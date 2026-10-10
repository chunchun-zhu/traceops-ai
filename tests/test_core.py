import json
import sys
from pathlib import Path

import anyio
import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import ROOT, Settings
from app.ingest import parse_file
from app.main import create_app
from app.models import ModelGateway, ModelUnavailable
from app.retrieval import Retriever


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, db_path=tmp_path / "test.db", seed_demo=True)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def start(client, **kwargs):
    response = client.post("/api/sessions", json={"device_ids": ["PRESS-01", "ASSY-01"], "symptom": "多设备气压不足", **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def feedback(client, state, result, note="现场核验"):
    response = client.post(f"/api/sessions/{state['id']}/feedback", json={"check_id": state["current_check"]["id"], "result": result, "note": note, "expected_revision": state["revision"]})
    assert response.status_code == 200, response.text
    return response.json()


def test_diagnosis_persists_after_restart(client, settings):
    state = start(client)
    assert state["topology"][0]["node"]["id"] == "BR-A"
    assert state["evidence"] and state["mode"] == "rule_demo"
    state = feedback(client, state, "normal", "总管读数 0.72 MPa")
    assert state["current_check"]["id"] == "branch_pressure"
    sid = state["id"]
    with TestClient(create_app(settings)) as restarted:
        state = restarted.get(f"/api/sessions/{sid}").json()
        assert len(state["checks"]) == 1
        state = feedback(restarted, state, "abnormal", "支路独立核验 0.35 MPa")
        assert state["status"] == "awaiting_confirmation"
        assert state["candidates"][0]["state"] == "not_supported"
        assert state["candidates"][1]["state"] == "supported"
        response = restarted.post(f"/api/sessions/{sid}/confirm", json={"decision": "confirmed", "operator": "测试班组", "note": "独立核验后确认支路异常，记录待维修事项", "expected_revision": state["revision"]})
        assert response.json()["status"] == "resolved"
        exported = restarted.get(f"/api/sessions/{sid}/export").json()
        assert len(exported["events"]) == 4
        assert exported["session"]["checks"][1]["note"].endswith("0.35 MPa")
        assert exported["session"]["evidence"] == state["evidence"]


def test_clarification_then_continue(client):
    state = start(client, device_ids=[])
    assert state["status"] == "needs_info" and not state["evidence"]
    response = client.post(f"/api/sessions/{state['id']}/clarify", json={"device_ids": ["PRESS-01"], "expected_revision": 0})
    assert response.json()["status"] == "checking"


def test_unknown_stops_and_handoff_record(client):
    state = feedback(client, start(client), "unknown")
    assert state["status"] == "handoff" and state["current_check"] is None
    response = client.post(f"/api/sessions/{state['id']}/confirm", json={"decision": "handoff", "operator": "B 班", "note": "仪表无法核验，交接专业人员", "expected_revision": state["revision"]})
    assert response.json()["status"] == "handed_off"


def test_all_normal_cannot_invent_root_cause(client):
    state = start(client)
    for _ in range(3):
        state = feedback(client, state, "normal")
    assert state["status"] == "handoff"
    assert state["current_check"] is None


def test_cross_branch_checks_do_not_invent_shared_branch(client):
    state = start(client, device_ids=["PRESS-01", "CNC-01"])
    assert state["topology"][0]["node"]["id"] == "COMP-01"
    state = feedback(client, state, "normal")
    assert set(state["current_check"]["target_ids"]) == {"BR-A", "BR-B"}
    assert "各设备供气支路" in state["current_check"]["title"]
    state = feedback(client, state, "abnormal")
    assert "至少一条" in state["summary"]
    assert "共同供气支路" not in state["candidates"][1]["name"]


def test_unsupported_fault_and_unknown_device(client):
    assert start(client, symptom="主轴温度过高")["status"] == "handoff"
    assert client.post("/api/sessions", json={"device_ids": ["BAD"], "symptom": "气压不足"}).status_code == 400


def test_revision_conflict_does_not_overwrite(client):
    state = start(client)
    feedback(client, state, "normal")
    response = client.post(f"/api/sessions/{state['id']}/feedback", json={"check_id": "source_pressure", "result": "abnormal", "expected_revision": 0})
    assert response.status_code == 409
    current = client.get(f"/api/sessions/{state['id']}/export").json()
    assert len(current["events"]) == 2
    assert current["session"]["checks"][0]["result"] == "normal"


def test_completed_check_and_premature_confirmation_rejected(client):
    state = feedback(client, start(client), "normal")
    response = client.post(f"/api/sessions/{state['id']}/feedback", json={"check_id": "source_pressure", "result": "normal", "expected_revision": state["revision"]})
    assert response.status_code == 400
    response = client.post(f"/api/sessions/{state['id']}/confirm", json={"decision": "confirmed", "operator": "test", "note": "不能提前结束", "expected_revision": state["revision"]})
    assert response.status_code == 400


def test_documents_idempotent_and_version_filtered(client):
    meta = {"title": "真实测试文档", "model": "TEST-01", "version": "v2", "text": "# E101\n\n气压不足，需要检查供气支路。", "kind": "work_order", "preliminary_cause": "供气不足", "confirmed_cause": "压力表故障"}
    first = client.post("/api/documents", json=meta).json()
    again = client.post("/api/documents", json=meta).json()
    assert first["id"] == again["id"] and again["duplicate"]
    result = client.post("/api/search", json={"query": "E101", "model": "TEST-01", "version": "v2"}).json()
    assert result["route"] == "exact_code" and len(result["items"]) == 1
    item = result["items"][0]
    assert item["page"] == 1 and item["confirmed_cause"] == "压力表故障"
    assert item["preliminary_cause"] == "供气不足"
    assert client.post("/api/search", json={"query": "E101", "model": "TEST-01", "version": "v1"}).json()["items"] == []
    assert client.post("/api/search", json={"query": "E101", "model": "WRONG"}).json()["items"] == []


def test_fault_code_boundary(client):
    client.post("/api/documents", json={"title": "边界", "model": "BOUNDARY", "text": "故障 E1010 的检查说明"})
    result = client.post("/api/search", json={"query": "E101", "model": "BOUNDARY"}).json()
    assert result["items"] == []


def test_upload_and_pdf_provenance(client):
    with pymupdf.open() as pdf:
        for text in ["E901 supply manual page one", "E902 supply manual page two"]:
            page = pdf.new_page()
            page.insert_text((72,72), text)
        content = pdf.tobytes()
    response = client.post("/api/documents/upload", data={"model": "PDF-MODEL", "version": "2026"}, files={"file": ("manual.pdf", content, "application/pdf")})
    assert response.status_code == 200, response.text
    result = client.post("/api/search", json={"query": "E902", "model": "PDF-MODEL"}).json()
    chunk = result["items"][0]
    original = client.get("/api/documents/" + chunk["document_id"]).json()
    assert chunk["page"] == 2
    assert "E902" in original["pages"][chunk["page"]-1][chunk["start"]:chunk["end"]]


def test_scan_and_bad_upload_rejected(client):
    with pymupdf.open() as pdf:
        pdf.new_page()
        content = pdf.tobytes()
    with pytest.raises(ValueError, match="OCR"):
        parse_file("scan.pdf", content)
    response = client.post("/api/documents/upload", data={"model": "X"}, files={"file": ("bad.exe", b"bad")})
    assert response.status_code == 400
    response = client.post("/api/documents/upload", data={"model": "X"}, files={"file": ("empty.txt", b" ")})
    assert response.status_code == 400


def test_topology_import_atomic_validation(client):
    nodes = [{"id": "CUSTOM-S", "name": "真实供气源", "kind": "source", "model": "AIR-SYSTEM"}, {"id": "CUSTOM-D", "name": "真实设备", "kind": "device", "model": "CUSTOM"}]
    data = {"nodes": nodes, "edges": [{"source": "CUSTOM-S", "target": "CUSTOM-D"}]}
    assert client.post("/api/topology/import", json=data).status_code == 200
    assert client.post("/api/topology/common", json={"device_ids": ["CUSTOM-D"], "expected_revision": 0}).json()[0]["node"]["id"] == "CUSTOM-S"
    bad = {"nodes": [{"id": "X", "name": "X", "kind": "branch", "model": "X"}], "edges": [{"source": "X", "target": "NONEXISTENT"}]}
    assert client.post("/api/topology/import", json=bad).status_code == 400
    assert "X" not in [n["id"] for n in client.get("/api/devices").json()]
    bad = {"nodes": [{"id": "X", "name": "X", "kind": "branch", "model": "X"}, {"id": "Y", "name": "Y", "kind": "branch", "model": "Y"}], "edges": [{"source": "X", "target": "Y"}, {"source": "Y", "target": "X"}]}
    assert client.post("/api/topology/import", json=bad).status_code == 400


def test_live_measurement_mapping(client):
    response = client.post("/api/measurements", json={"node_id": "BR-A", "value": 0.61, "observed_at": "2026-10-10T10:00:00+08:00", "source": "接口联调"})
    assert response.status_code == 200
    assert response.json()["is_demo"] is False
    assert response.json()["observed_at"] == "2026-10-10T02:00:00+00:00"
    assert client.post("/api/measurements", json={"node_id": "BR-A", "value": 0.5, "observed_at": "2026-10-10T10:00:00", "source": "test"}).status_code == 400


def test_auth_protects_reads_and_writes(settings):
    secured = settings.model_copy(update={"api_token": "secret-test"})
    with TestClient(create_app(secured)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/devices").status_code == 401
        assert client.post("/api/sessions", json={"symptom": "低气压"}).status_code == 401
        assert client.get("/api/devices", headers={"Authorization": "Bearer secret-test"}).status_code == 200


def http_gateway(settings, monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler)))
    return ModelGateway(settings)


@pytest.mark.parametrize("response_format", ["scores", "results"])
def test_hybrid_rrf_and_rerank_http_contract(client, settings, monkeypatch, response_format):
    configured = settings.model_copy(update={"embedding_base_url": "http://model/v1", "rerank_url": "http://model/rerank"})
    def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/v1/embeddings":
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0,0.1+i]} for i in range(len(body["input"]))]})
        scores = list(range(len(body["documents"])))
        response = {"scores": scores} if response_format == "scores" else {"results": [{"index": i, "relevance_score": score} for i, score in reversed(list(enumerate(scores)))]}
        return httpx.Response(200, json=response)
    gateway = http_gateway(configured, monkeypatch, handler)
    result = Retriever(client.app.state.store, gateway).search("多设备气压不足")
    assert result["route"] == "hybrid_rrf+rerank"
    assert not result["warnings"] and "dense_score" in result["items"][0]
    assert "rerank_score" in result["items"][0]


def test_model_outage_keeps_bm25(client, settings, monkeypatch):
    configured = settings.model_copy(update={"embedding_base_url": "http://offline/v1"})
    gateway = http_gateway(configured, monkeypatch, lambda _: httpx.Response(503))
    result = Retriever(client.app.state.store, gateway).search("气压不足")
    assert result["route"] == "bm25" and result["items"] and result["warnings"]


def test_invalid_model_citation_rejected(settings, monkeypatch):
    configured = settings.model_copy(update={"llm_base_url": "http://model/v1"})
    gateway = http_gateway(configured, monkeypatch, lambda _: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"summary": "虚构引用", "evidence_ids": ["invented"]})}}]}))
    with pytest.raises(ModelUnavailable, match="引用"):
        gateway.explain({"evidence": [{"id": "real"}]})


def test_model_valid_output(settings, monkeypatch):
    configured = settings.model_copy(update={"llm_base_url": "http://model/v1"})
    gateway = http_gateway(configured, monkeypatch, lambda _: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"summary": "共同供气需现场核验", "evidence_ids": ["real"]})}}]}))
    assert gateway.explain({"evidence": [{"id": "real"}]})["evidence_ids"] == ["real"]


def test_mcp_protocol_lists_and_calls_real_tools(client, settings):
    async def run():
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(ROOT), env={"SUZHANG_DB_PATH": str(settings.resolved_db_path)})
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert {t.name for t in listed.tools} == {"get_device_registry", "find_common_supply", "get_pressure_snapshots", "search_maintenance_evidence"}
                result = await session.call_tool("find_common_supply", {"device_ids": ["PRESS-01", "ASSY-01"]})
                assert not result.isError
                parsed = json.loads(result.content[0].text)
                assert parsed["items"][0]["node"]["id"] == "BR-A"
                result = await session.call_tool("search_maintenance_evidence", {"query": "E101", "model": "P-200"})
                assert json.loads(result.content[0].text)["items"][0]["page"] == 1
    anyio.run(run)
