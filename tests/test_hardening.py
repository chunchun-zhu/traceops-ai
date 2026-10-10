import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from filelock import Timeout

from app.config import Settings
from app.main import create_app
from app.maintenance import backup, restore, validate
from app.security import UserInput, LoginInput
from test_core import start, feedback
from test_upgrade import confirm_cause, repair, retest_payload, verify

PASSWORD = "Test-only-password-42"
ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def secured(tmp_path):
    settings = Settings(_env_file=None, db_path=tmp_path / "test.db", auth_mode="accounts", cookie_secure=False, api_token="legacy-token")
    app = create_app(settings)
    security = app.state.security
    for username, role in (("admin", "admin"), ("operator", "operator"), ("reviewer", "reviewer"), ("viewer", "viewer")):
        security.create_user(UserInput(username=username, password=PASSWORD, display_name=username, role=role), bootstrap=username=="admin")
    with TestClient(app, headers=ORIGIN) as client:
        yield client, app, settings


def login(client, username):
    result=client.post("/api/auth/login",json={"username":username,"password":PASSWORD})
    assert result.status_code==200,result.text
    return result.json()


def standard_payload(client):
    standard = client.get("/api/standards").json()[0]
    return {k:standard[k] for k in ("title","model","minimum","maximum","reference_document_id","page","quote","source_start")}|{"approved_by":"forged reviewer"}


def test_login_cookie_legacy_bypass_and_csrf(secured):
    client,app,_=secured
    assert client.get("/api/devices",headers={"Authorization":"Bearer legacy-token"}).status_code==401
    assert client.post("/api/auth/login",json={"username":"admin","password":PASSWORD},headers={"Origin":"http://evil.test"}).status_code==403
    user=login(client,"admin")
    assert user["role"]=="admin" and "password_hash" not in user
    cookie=client.cookies.get("suzhang-session")
    with app.state.store.connect() as db:
        row=db.execute("SELECT token_hash FROM auth_sessions").fetchone()
        assert row[0]!=cookie and len(row[0])==64
        assert db.execute("SELECT password_hash FROM users LIMIT 1").fetchone()[0].startswith("scrypt$")
    assert client.get("/api/auth/me").json()["username"]=="admin"
    assert client.post("/api/sessions",json={"symptom":"test"},headers={"Origin":"http://evil.test"}).status_code==403
    assert client.post("/api/sessions",json={"symptom":"test"},headers={"Origin":"http://evil.test","Authorization":"Bearer "}).status_code==401
    assert client.post("/api/auth/logout").status_code==200
    assert client.get("/api/devices").status_code==401


def test_roles_readonly_and_admin_boundaries(secured):
    client,_,_=secured
    login(client,"viewer")
    assert client.get("/api/devices").status_code==200
    assert client.post("/api/search",json={"query":"E101"}).status_code==200
    assert client.post("/api/sessions",json={"symptom":"test"}).status_code==403
    assert client.get("/api/admin/users").status_code==403
    assert client.post("/api/auth/logout").status_code==200
    login(client,"operator")
    assert client.post("/api/tasks/embeddings").status_code==403
    assert client.post("/api/documents",json={"title":"x","text":"x"}).status_code==403
    assert client.post("/api/topology/import",json={"nodes":[]}).status_code==403
    assert client.post("/api/sessions",json={"symptom":"test"}).status_code==200


def test_independent_approval_and_source_recheck(secured):
    client,_,_=secured
    login(client,"operator")
    payload=standard_payload(client)
    draft=client.post("/api/standards",json=payload).json()
    assert draft["review_status"]=="pending" and not draft["active"] and not draft["approved_by"]
    assert draft["submitted_by"]["username"]=="operator"
    review_url=f'/api/standards/{draft["id"]}/review'
    assert client.post(review_url,json={"decision":"approved","note":"checked"}).status_code==403
    login(client,"admin")
    self_draft=client.post("/api/standards",json=payload).json()
    assert client.post(f'/api/standards/{self_draft["id"]}/review',json={"decision":"approved","note":"checked"}).status_code==400
    login(client,"reviewer")
    assert client.post(f'/api/standards/{draft["id"]}/status',json={"active":True}).status_code==400
    approved=client.post(review_url,json={"decision":"approved","note":"核验单位、范围和型号"})
    assert approved.status_code==200,approved.text
    assert approved.json()["active"] and approved.json()["reviewed_by"]["username"]=="reviewer"
    assert "forged" not in approved.json()["approved_by"]
    assert client.post(review_url,json={"decision":"rejected","note":"again"}).status_code==409
    login(client,"admin")
    assert any(a["action"]=="standard.approved" for a in client.get("/api/admin/audit").json())
    client.post(f'/api/documents/{payload["reference_document_id"]}/status',json={"active":False})
    login(client,"reviewer")
    assert client.post(f'/api/standards/{self_draft["id"]}/review',json={"decision":"approved","note":"checked"}).status_code==400


def test_authenticated_closure_requires_trusted_standard(secured):
    client,_,_=secured
    login(client,"operator")
    state=confirm_cause(client)
    assert state["confirmation"]["operator"]=="operator (operator)"
    state=repair(client,state)
    assert state["repairs"][-1]["operator"]=="operator (operator)"
    payload=retest_payload(client,state)
    assert verify(client,state,payload).status_code==400
    # Independently approve standards for every selected device.
    available=client.get("/api/standards").json()
    drafts={}
    for model in set(state["device_models"].values()):
        legacy=next(t for t in available if t["model"]==model)
        body={k:legacy[k] for k in ("title","model","minimum","maximum","reference_document_id","page","quote","source_start","approved_by")}
        drafts[model]=client.post("/api/standards",json=body).json()
    login(client,"reviewer")
    for draft in drafts.values():
        assert client.post(f'/api/standards/{draft["id"]}/review',json={"decision":"approved","note":"已核对"}).status_code==200
    login(client,"operator")
    for reading in payload["readings"]:
        draft=drafts[state["device_models"][reading["device_id"]]]
        reading.update(standard_id=draft["id"],minimum=draft["minimum"],maximum=draft["maximum"],reference_document_id=draft["reference_document_id"])
    result=verify(client,state,payload)
    assert result.status_code==200,result.text
    assert result.json()["status"]=="resolved"
    assert result.json()["verifications"][-1]["operator"]=="operator (operator)"
    exported=client.get(f'/api/sessions/{state["id"]}/export').json()
    assert exported["session"]["created_by"]["username"]=="operator"
    assert exported["events"][-1]["actor"]["username"]=="operator"


def test_user_revocation_last_admin_and_password_reset(secured):
    client,_,_=secured
    admin=login(client,"admin")
    assert client.post(f'/api/admin/users/{admin["id"]}/status',json={"role":"viewer","active":True}).status_code==409
    viewer=next(u for u in client.get("/api/admin/users").json() if u["username"]=="viewer")
    login(client,"viewer")
    old=client.cookies.get("suzhang-session")
    login(client,"admin")
    assert client.post(f'/api/admin/users/{viewer["id"]}/status',json={"role":"operator","active":True}).status_code==200
    assert client.get("/api/devices",headers={"Authorization":"Bearer "+old}).status_code==401
    assert client.post(f'/api/admin/users/{viewer["id"]}/password',json={"password":"new-password-test-2026"}).status_code==200
    assert client.post("/api/auth/login",json={"username":"viewer","password":PASSWORD}).status_code==401
    assert client.post("/api/auth/login",json={"username":"viewer","password":"new-password-test-2026"}).status_code==200


def test_login_limit_expiry_and_no_bootstrap_twice(secured):
    client,app,_=secured
    for _ in range(5):
        assert client.post("/api/auth/login",json={"username":"unknown","password":"incorrect"}).status_code==401
    assert client.post("/api/auth/login",json={"username":"unknown","password":"incorrect"}).status_code==429
    with pytest.raises(Exception,match="不能重复初始化"):
        app.state.security.create_user(UserInput(username="second",display_name="second",password=PASSWORD),bootstrap=True)
    login(client,"admin")
    with app.state.store.connect() as db:
        db.execute("UPDATE auth_sessions SET expires=?",(time.time()-1,))
    assert client.get("/api/devices").status_code==401


def test_paired_backup_restore_resume_and_corruption(tmp_path):
    settings=Settings(_env_file=None,db_path=tmp_path/"live.db")
    with TestClient(create_app(settings)) as client:
        state=start(client)
        state=feedback(client,state,"abnormal")
        # Active server must block maintenance, including in the same process.
        with pytest.raises(Timeout):
            backup(settings.db_path,tmp_path/"blocked")
    bundle=tmp_path/"backup"
    manifest=backup(settings.db_path,bundle)
    assert validate(bundle)==manifest
    recovered=restore(bundle,tmp_path/"recovered")
    with TestClient(create_app(settings.model_copy(update={"db_path":recovered,"seed_demo":False}))) as client:
        restored=client.get(f'/api/sessions/{state["id"]}').json()
        assert restored["revision"]==state["revision"] and restored["status"]==state["status"]
        confirmed=client.post(f'/api/sessions/{restored["id"]}/confirm',json={"decision":"confirmed","operator":"recovered","note":"恢复后确认","expected_revision":restored["revision"]}).json()
        repaired=repair(client,confirmed)
        assert verify(client,repaired,retest_payload(client,repaired)).json()["status"]=="resolved"
        assert client.get(f'/api/sessions/{state["id"]}/export').json()["events"]
    with pytest.raises(FileExistsError):
        restore(bundle,tmp_path/"recovered")
    with (bundle/"suzhang.db").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError,match="校验和"):
        restore(bundle,tmp_path/"bad-restore")
    assert not (tmp_path/"bad-restore").exists()


def test_account_backup_roundtrip(tmp_path):
    settings=Settings(_env_file=None,db_path=tmp_path/"live.db",auth_mode="accounts",cookie_secure=False)
    app=create_app(settings)
    app.state.security.create_user(UserInput(username="admin",display_name="admin",password=PASSWORD),bootstrap=True)
    with TestClient(app,headers=ORIGIN) as client:
        login(client,"admin")
        old=client.cookies.get("suzhang-session")
        client.post("/api/tasks/documents",files={"file":("test.md",b"backup task")},data={"model":"TEST"})
    backup(settings.db_path,tmp_path/"bundle")
    recovered=restore(tmp_path/"bundle",tmp_path/"restored")
    with TestClient(create_app(settings.model_copy(update={"db_path":recovered})),headers=ORIGIN) as client:
        assert client.get("/api/devices",headers={"Authorization":"Bearer "+old}).status_code==401
        login(client,"admin")
        assert client.get("/api/tasks").json()
        assert client.get("/api/admin/audit").json()
