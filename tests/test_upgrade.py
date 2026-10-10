from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.ingest import ingest
from app.main import create_app
from app.models import ModelGateway, ModelUnavailable
from app.retrieval import Retriever, bm25
from app.schemas import DocumentInput
from app.seed import seed
from app.store import Store
from app.workflow import Workflow
from test_core import settings, client, start, feedback


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def confirm_cause(client):
    state = feedback(client, start(client), "abnormal")
    response = client.post(f"/api/sessions/{state['id']}/confirm", json={"decision": "confirmed", "operator": "A班", "note": "独立核验确认供气异常", "expected_revision": state["revision"]})
    assert response.status_code == 200, response.text
    return response.json()


def repair(client, state):
    response = client.post(f"/api/sessions/{state['id']}/repair", json={"operator": "维修班", "action": "记录授权维修人员已完成的处理", "completed_at": timestamp(), "expected_revision": state["revision"]})
    assert response.status_code == 200, response.text
    return response.json()


def retest_payload(client, state, result="passed"):
    documents = client.get("/api/documents").json()
    reference = next(d["id"] for d in documents if d["model"] == "AIR-SYSTEM" and d["kind"] == "manual")
    return {"result": result, "operator": "验收班", "note": "维修后现场读数与告警已核对", "alarms_cleared": True,
            "readings": [{"device_id": d, "value": 0.68, "minimum": 0.6, "maximum": 0.8, "observed_at": timestamp(), "reference_document_id": reference} for d in state["device_ids"]], "expected_revision": state["revision"]}


def verify(client, state, payload):
    return client.post(f"/api/sessions/{state['id']}/verify", json=payload)


def test_only_repair_and_retest_can_close_after_restart(client, settings):
    state = confirm_cause(client)
    assert state["status"] == "repairing"
    assert verify(client, state, retest_payload(client, state)).status_code == 400
    state = repair(client, state)
    assert state["status"] == "awaiting_verification"
    with TestClient(create_app(settings)) as restarted:
        state = restarted.get(f"/api/sessions/{state['id']}").json()
        wf = restarted.app.state.workflow
        snapshot = wf.graph.get_state(wf.config(state["id"]))
        assert snapshot.next == ("await_input",)
        assert snapshot.tasks[0].interrupts[0].value["phase"] == "awaiting_verification"
        response = verify(restarted, state, retest_payload(restarted, state))
        assert response.status_code == 200, response.text
        state = response.json()
        assert state["status"] == "resolved" and len(state["repairs"]) == len(state["verifications"]) == 1
        assert not wf.graph.get_state(wf.config(state["id"])).next
        exported = restarted.get(f"/api/sessions/{state['id']}/export").json()
        assert [e["type"] for e in exported["events"]] == ["started", "feedback", "confirmation", "repair", "verify"]


@pytest.mark.parametrize("violation", ["missing_device", "alarm_active", "outside_range", "before_repair", "wrong_reference", "duplicate_device"])
def test_bad_passed_retest_rejected_without_consuming_interrupt(client, violation):
    state = repair(client, confirm_cause(client))
    payload = retest_payload(client, state)
    if violation == "missing_device":
        payload["readings"].pop()
    elif violation == "alarm_active":
        payload["alarms_cleared"] = False
    elif violation == "outside_range":
        payload["readings"][0]["value"] = 0.2
    elif violation == "before_repair":
        payload["readings"][0]["observed_at"] = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    elif violation == "wrong_reference":
        unrelated = client.post("/api/documents", json={"title": "其他型号手册", "model": "OTHER", "text": "其他设备"}).json()
        payload["readings"][0]["reference_document_id"] = unrelated["id"]
    elif violation == "duplicate_device":
        payload["readings"][1]["device_id"] = payload["readings"][0]["device_id"]
    assert verify(client, state, payload).status_code == 400
    current = client.get(f"/api/sessions/{state['id']}").json()
    assert current["revision"] == state["revision"] and current["status"] == "awaiting_verification"
    assert verify(client, current, retest_payload(client, current)).json()["status"] == "resolved"


def test_retest_failure_requires_new_repair_and_unknown_handoff(client):
    state = repair(client, confirm_cause(client))
    payload = retest_payload(client, state, "failed")
    payload["alarms_cleared"] = False
    state = verify(client, state, payload).json()
    assert state["status"] == "repairing"
    state = repair(client, state)
    payload = retest_payload(client, state, "unknown")
    payload["readings"] = []
    state = verify(client, state, payload).json()
    assert state["status"] == "handoff" and len(state["verifications"]) == 2
    assert state["verifications"][1]["repair_number"] == 2


def test_graph_checkpoint_recovers_projection_failure(client, monkeypatch):
    state = start(client)
    store = client.app.state.store
    original = store.project_session
    def fail_after_feedback(checkpoint):
        if checkpoint["revision"] == 1:
            raise RuntimeError("simulated projection crash")
        return original(checkpoint)
    monkeypatch.setattr(store, "project_session", fail_after_feedback)
    with pytest.raises(RuntimeError, match="projection crash"):
        feedback(client, state, "normal")
    assert store.session(state["id"])["revision"] == 0
    monkeypatch.setattr(store, "project_session", original)
    recovered = client.get(f"/api/sessions/{state['id']}").json()
    assert recovered["revision"] == 1 and len(recovered["checks"]) == 1
    assert recovered["current_check"]["id"] == "branch_pressure"
    assert len(store.events(state["id"])) == 2


def test_node_failure_restarts_without_repeating_completed_collect(settings, monkeypatch):
    original = Workflow.plan
    def fail_once(self, state):
        if not getattr(self, "failed_plan", False):
            self.failed_plan = True
            raise RuntimeError("simulated node crash")
        return original(self, state)
    monkeypatch.setattr(Workflow, "plan", fail_once)
    with TestClient(create_app(settings)) as client:
        with pytest.raises(RuntimeError, match="node crash"):
            start(client)
        sid = client.app.state.store.sessions()[0]["id"]
    monkeypatch.setattr(Workflow, "plan", original)
    with TestClient(create_app(settings)) as restarted:
        state = restarted.get(f"/api/sessions/{sid}").json()
        assert state["status"] == "checking"
        assert [t["node"] for t in state["trace"]].count("retrieval") == 1


def test_two_writers_only_accept_one_feedback(client, settings):
    state = start(client)
    body = {"check_id": "source_pressure", "result": "normal", "expected_revision": state["revision"]}
    with TestClient(create_app(settings)) as other:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c: c.post(f"/api/sessions/{state['id']}/feedback", json=body).status_code, [client, other]))
    assert sorted(results) == [200,409]
    assert len(client.get(f"/api/sessions/{state['id']}").json()["checks"]) == 1


def test_old_manual_confirmation_migrates_without_erasing_history(client):
    store = client.app.state.store
    legacy = store.new_session({"device_ids": ["PRESS-01"], "symptom": "气压不足", "status": "resolved", "question": None,
        "candidates": [], "evidence": [], "topology": [], "observations": [], "checks": [], "current_check": None,
        "summary": "旧版人工确认", "warnings": [], "trace": [], "skill": {"id": "air-supply", "version": "1.0.0"},
        "retrieval_route": "bm25", "model_explanation": None, "confirmation": {"operator": "旧班组", "note": "原始结论", "at": timestamp()}, "mode": "rule_demo"})
    migrated = client.get(f"/api/sessions/{legacy['id']}").json()
    assert migrated["status"] == "repairing" and migrated["confirmation"]["note"] == "原始结论"
    assert migrated["workflow_version"] == 2


def test_persisted_bm25_matches_baseline_and_disabled_docs_excluded(client):
    store = client.app.state.store
    retriever = Retriever(store, ModelGateway(client.app.state.workflow.gateway.settings))
    chunks = store.chunks()
    baseline = bm25("气压不足支路", chunks)
    indexed = retriever.index.lexical("气压不足支路", chunks)
    assert [c["id"] for c in baseline] == [c["id"] for c in indexed]
    for a,b in zip(baseline,indexed):
        assert a["bm25_score"] == pytest.approx(b["bm25_score"])
    doc = client.post("/api/documents", json={"title": "退役说明", "model": "RETIRED", "text": "E808 气压不足"}).json()
    assert client.post("/api/documents/"+doc["id"]+"/status", json={"active":False}).status_code == 200
    assert client.post("/api/search", json={"query":"E808", "model":"RETIRED"}).json()["items"] == []
    assert store.document(doc["id"])["pages"]
    client.post("/api/documents/"+doc["id"]+"/status", json={"active":True})
    assert client.post("/api/search", json={"query":"E808", "model":"RETIRED"}).json()["items"]


class CountingGateway(ModelGateway):
    def __init__(self, settings):
        super().__init__(settings)
        self.calls = []
        self.fail = False

    def embeddings(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise ModelUnavailable("模拟服务异常")
        return [[1.0,0.5] for _ in texts]


def test_embedding_incremental_reuse_restart_model_switch(client, settings):
    store = client.app.state.store
    configured = settings.model_copy(update={"embedding_base_url":"http://model/v1"})
    gateway = CountingGateway(configured)
    retriever = Retriever(store, gateway)
    initial = retriever.index.build()
    assert initial["added"] == len(store.chunks())
    calls = len(gateway.calls)
    assert retriever.index.build()["added"] == 0 and len(gateway.calls) == calls
    restarted = Retriever(Store(settings.resolved_db_path), gateway)
    assert restarted.search("供气异常")["route"] == "hybrid_rrf"
    assert gateway.calls[-1] == ["供气异常"]
    new = ingest(store, DocumentInput(title="新增手册", model="NEW", text="E909 供气检查"))
    assert retriever.index.status()["vectors_missing"] == new["chunk_count"]
    added = retriever.index.build()
    assert added["added"] == new["chunk_count"] and added["reused"] == initial["added"]
    other = CountingGateway(configured.model_copy(update={"embedding_model":"BGE-M3-v2"}))
    changed = Retriever(store, other)
    assert changed.index.status()["vectors_ready"] == 0
    changed.index.build()
    assert other.calls


def test_embedding_outage_after_build_falls_back_without_losing_vectors(client, settings):
    gateway = CountingGateway(settings.model_copy(update={"embedding_base_url":"http://model/v1"}))
    retriever = Retriever(client.app.state.store, gateway)
    retriever.index.build()
    ready = retriever.index.status()["vectors_ready"]
    gateway.fail = True
    result = retriever.search("气压不足")
    assert result["route"] == "bm25" and result["warnings"] and result["items"]
    assert retriever.index.status()["vectors_ready"] == ready


def test_checkpoint_not_exposed_and_index_build_without_model_rejected(client):
    state = start(client)
    assert all(not k.startswith("_") for k in state)
    assert client.post("/api/indexes/embeddings/build").status_code == 400
    assert client.get("/api/indexes").json()["vectors_missing"] > 0


def test_embedding_partial_failure_keeps_completed_batches(client, settings):
    gateway = CountingGateway(settings.model_copy(update={"embedding_base_url":"http://model/v1"}))
    retriever = Retriever(client.app.state.store, gateway)
    original = gateway.embeddings
    def interrupted(texts):
        if gateway.calls:
            raise ModelUnavailable("第二批模拟失败")
        return original(texts)
    gateway.embeddings = interrupted
    with pytest.raises(ModelUnavailable):
        retriever.index.build(batch_size=2)
    assert retriever.index.status()["vectors_ready"] == 2
    gateway.embeddings = original
    result = retriever.index.build(batch_size=2)
    assert result["reused"] == 2
    assert result["added"] == len(client.app.state.store.chunks()) - 2
    assert retriever.index.status()["vectors_missing"] == 0
