import base64
import json
import threading
import time
from datetime import datetime,timedelta,timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas import AlarmInput, StandardInput
from app.tasks import TaskQueue
from app.store import Store, ConflictError
from app.retrieval import Retriever
from app.models import ModelGateway
from app.operations import Operations
from test_core import client, settings, start, feedback
from test_upgrade import CountingGateway, timestamp, confirm_cause, repair, retest_payload, verify


def wait_job(client, job_id, terminal=True):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        job=client.get("/api/tasks/"+job_id).json()
        if job["status"] in ({"succeeded","failed","cancelled"} if terminal else {"running"}):
            return job
        time.sleep(0.02)
    raise AssertionError(job)


def queue(settings):
    store=Store(settings.resolved_db_path)
    gateway=CountingGateway(settings.model_copy(update={"embedding_base_url":"http://model/v1"}))
    return TaskQueue(store,Retriever(store,gateway).index)


def document_payload(text="后台解析测试"):
    return {"filename":"background.md","model":"TEST","version":"1","content":base64.b64encode(text.encode()).decode()}


def test_task_upload_real_worker_success_restart(client,settings):
    response=client.post("/api/tasks/documents",files={"file":("worker.md","E919 后台入库".encode(),"text/markdown")},data={"model":"TASK","version":"1"})
    assert response.status_code==202
    job=wait_job(client,response.json()["id"])
    assert job["status"]=="succeeded" and job["progress"]["stage"]=="done"
    assert "input" not in job
    assert client.post("/api/search",json={"query":"E919","model":"TASK"}).json()["items"]
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/api/tasks/"+job["id"]).json()["status"]=="succeeded"


def test_task_failed_parse_then_retry(client):
    response=client.post("/api/tasks/documents",files={"file":("bad.bin",b"bad")},data={"model":"TASK"})
    job=wait_job(client,response.json()["id"])
    assert job["status"]=="failed" and job["error"]
    assert client.post("/api/tasks/"+job["id"]+"/retry").status_code==200
    retried=wait_job(client,job["id"])
    assert retried["status"]=="failed" and retried["attempts"]==2
    assert client.post("/api/tasks/embeddings").status_code==400


def test_queue_dedupe_cancel_retry_and_recovery(settings):
    q=queue(settings)
    job=q.enqueue("document",document_payload())
    assert q.enqueue("document",document_payload())["id"]==job["id"]
    assert q.cancel(job["id"])["status"]=="cancelled"
    q.retry(job["id"])
    claimed=q.claim()
    assert q.get(job["id"])["status"]=="running"
    restarted=queue(settings)
    restarted.recover()
    assert restarted.get(job["id"])["status"]=="queued"
    restarted.execute(restarted.claim())
    done=restarted.get(job["id"])
    assert done["status"]=="succeeded" and done["attempts"]==2
    with pytest.raises(ConflictError):
        restarted.retry(job["id"])


def test_running_cancel_during_parse_does_not_ingest(client,monkeypatch):
    import app.tasks as module
    entered,release=threading.Event(),threading.Event()
    original=module.parse_file
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(module,"parse_file",blocked)
    try:
        job=client.post("/api/tasks/documents",files={"file":("cancel.md",b"cancel me")},data={"model":"CANCEL"}).json()
        assert entered.wait(5)
        response=client.post("/api/tasks/"+job["id"]+"/cancel")
        assert response.status_code==200 and response.json()["cancel_requested"]
    finally:
        release.set()
    assert wait_job(client,job["id"])["status"]=="cancelled"
    assert not any(d["model"]=="CANCEL" for d in client.get("/api/documents").json())


def test_committing_document_replay_is_idempotent(client,settings):
    q=queue(settings)
    # Stop the API worker before preparing a crash scenario.
    client.app.state.tasks.stop()
    job=q.enqueue("document",document_payload())
    claimed=q.claim();q.commit_phase(job["id"])
    with pytest.raises(ConflictError):q.cancel(job["id"])
    # Simulate ingest committed but the final job result was not saved.
    from app.ingest import ingest
    from app.schemas import DocumentInput
    ingest(q.store,DocumentInput(title="background.md",model="TEST",version="1",text="后台解析测试"),filename="background.md")
    q.recover();q.execute(q.claim())
    assert q.get(job["id"])["status"]=="succeeded"
    assert q.get(job["id"])["result"]["duplicate"]
    assert len([d for d in q.store.documents() if d["model"]=="TEST"])==1


def test_embedding_job_partial_cancel_retry_reuses(client,settings):
    client.app.state.tasks.stop()
    q=queue(settings)
    job=q.enqueue("embedding",{})
    original=q.index.gateway.embeddings
    def cancel_after_batch(texts):
        result=original(texts)
        q.cancel(job["id"])
        return result
    q.index.gateway.embeddings=cancel_after_batch
    q.execute(q.claim())
    assert q.get(job["id"])["status"]=="cancelled"
    ready=q.index.status()["vectors_ready"]
    assert ready>0
    q.index.gateway.embeddings=original
    q.retry(job["id"]);q.execute(q.claim())
    assert q.get(job["id"])["status"]=="succeeded"
    assert q.get(job["id"])["result"]["reused"]==ready


def alarm_payload(**updates):
    return {"event_id":"source-1","device_id":"PRESS-01","code":"E101","status":"raised","source":"test-iot","occurred_at":timestamp(),"message":"压力不足",**updates}


def test_alarm_idempotency_and_conflicting_duplicate(client):
    item=alarm_payload()
    first=client.post("/api/alarms",json=item).json()
    repeated=client.post("/api/alarms",json=item).json()
    assert repeated["duplicate"] and repeated["id"]==first["id"]
    assert client.post("/api/alarms",json={**item,"status":"cleared"}).status_code==409
    assert client.post("/api/alarms",json=alarm_payload(device_id="NOT-FOUND",event_id="unknown-device")).status_code==400
    assert client.post("/api/alarms",json=alarm_payload(occurred_at="2026-10-10T10:00:00")).status_code==422


def test_alarm_time_window_late_arrival_and_sources(client):
    now=datetime.now(timezone.utc)
    clear=alarm_payload(event_id="clear",status="cleared",occurred_at=(now-timedelta(minutes=1)).isoformat())
    client.post("/api/alarms",json=clear)
    client.post("/api/alarms",json=alarm_payload(occurred_at=(now-timedelta(minutes=5)).isoformat()))
    assert not client.get("/api/alarms",params={"active":True}).json()
    client.post("/api/alarms",json=alarm_payload(source="other",event_id="other"))
    assert len(client.get("/api/alarms",params={"active":True}).json())==1
    rows=client.get("/api/alarms",params={"since":(now-timedelta(minutes=2)).isoformat()}).json()
    assert len(rows)==2
    assert client.get("/api/alarms",params={"since":now.isoformat(),"until":(now-timedelta(hours=1)).isoformat()}).status_code==400
    assert client.get("/api/alarms",params={"limit":1001}).status_code==422


def test_alarm_start_snapshot_and_clear_required_for_closure(client):
    item=client.post("/api/alarms",json=alarm_payload()).json()
    state=start(client,device_ids=[],alarm_ids=[item["id"]])
    assert state["device_ids"]==["PRESS-01"] and state["alarm_events"][0]["id"]==item["id"]
    state=feedback(client,state,"abnormal")
    state=client.post(f"/api/sessions/{state['id']}/confirm",json={"decision":"confirmed","operator":"test","note":"confirmed","expected_revision":state["revision"]}).json()
    state=repair(client,state)
    payload=retest_payload(client,state)
    assert verify(client,state,payload).status_code==400
    client.post("/api/alarms",json=alarm_payload(event_id="clear",status="cleared"))
    closed=verify(client,state,payload)
    assert closed.status_code==200,closed.text
    assert closed.json()["verifications"][-1]["alarm_resolution"][0]["status"]=="cleared"
    assert client.post("/api/sessions",json={"device_ids":[],"alarm_ids":[item["id"]],"symptom":"压力不足"}).status_code==400


def test_standard_exact_quote_and_model_validation(client):
    document=client.post("/api/documents",json={"title":"标准手册","model":"P-200","version":"2","text":"入口压力范围 0.65–0.75 MPa。"}).json()
    payload={"title":"核验标准","model":"P-200","minimum":0.65,"maximum":0.75,"reference_document_id":document["id"],"page":1,"quote":"入口压力范围 0.65–0.75 MPa。","approved_by":"A班"}
    for changed in ({"quote":"伪造条款"},{"page":2},{"model":"OTHER"},{"minimum":0.8},{"source_start":1}):
        assert client.post("/api/standards",json={**payload,**changed}).status_code in {400,422}
    standard=client.post("/api/standards",json=payload).json()
    assert standard["document_version"]=="2" and standard["source_start"]==0
    assert not standard["is_demo"]


@pytest.mark.parametrize("violation",["no_standard","tampered_range","inactive_standard","inactive_document","wrong_model"])
def test_standard_enforced_and_snapshot_kept(client,violation):
    state=repair(client,confirm_cause(client))
    payload=retest_payload(client,state)
    reading=payload["readings"][0]
    if violation=="no_standard":reading.pop("standard_id")
    elif violation=="tampered_range":reading["maximum"]=100
    elif violation=="inactive_standard":client.post("/api/standards/"+reading["standard_id"]+"/status",json={"active":False})
    elif violation=="inactive_document":client.post("/api/documents/"+reading["reference_document_id"]+"/status",json={"active":False})
    else:
        std=next(s for s in client.get("/api/standards").json() if s["model"]=="A-100")
        reading.update(standard_id=std["id"],reference_document_id=std["reference_document_id"])
    assert verify(client,state,payload).status_code==400
    assert client.get(f"/api/sessions/{state['id']}").json()["revision"]==state["revision"]


def test_standard_audit_snapshot_survives_deactivation(client):
    state=repair(client,confirm_cause(client))
    payload=retest_payload(client,state)
    result=verify(client,state,payload).json()
    standard=result["verifications"][0]["readings"][0]["standard_snapshot"]
    client.post("/api/standards/"+standard["id"]+"/status",json={"active":False})
    saved=client.get(f"/api/sessions/{state['id']}/export").json()
    assert saved["session"]["verifications"][0]["readings"][0]["standard_snapshot"]["active"] is True


def test_only_one_worker_executes_shared_queue(client,settings,monkeypatch):
    import app.tasks as module
    client.app.state.tasks.stop()
    first,second=queue(settings),queue(settings)
    entered,release=threading.Event(),threading.Event()
    original=module.parse_file
    calls=[]
    def blocked(*args):
        calls.append(1);entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(module,"parse_file",blocked)
    job=first.enqueue("document",document_payload())
    first.start();second.start()
    try:
        assert entered.wait(5)
        assert second.get(job["id"])["status"]=="running"
        release.set()
        done=wait_job(client,job["id"])
        assert done["status"]=="succeeded" and done["attempts"]==1 and len(calls)==1
    finally:
        release.set();first.stop();second.stop()


@pytest.mark.parametrize("path",["/api/tasks","/api/alarms","/api/standards"])
def test_new_routes_require_configured_token(settings,path):
    with TestClient(create_app(settings.model_copy(update={"api_token":"test-token"}))) as secured:
        assert secured.get(path).status_code==401
        assert secured.get(path,headers={"Authorization":"Bearer test-token"}).status_code==200
