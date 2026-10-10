import base64
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, Query
from fastapi.responses import FileResponse, JSONResponse
from filelock import FileLock
from fastapi.staticfiles import StaticFiles

from .config import ROOT, Settings
from .ingest import ingest, parse_file
from .models import ModelGateway, ModelUnavailable
from .retrieval import Retriever
from .schemas import ClarifyInput, ConfirmInput, DocumentInput, FeedbackInput, MeasurementInput, SearchInput, StartInput, TopologyInput, RepairInput, VerifyInput, DocumentStatusInput, AlarmInput, StandardInput
from .seed import seed, seed_standards
from .store import ConflictError, Store
from .topology import Topology
from .workflow import Workflow
from .operations import Operations
from .tasks import TaskQueue
from .security import Security, LoginInput, UserInput, UserUpdate, PasswordInput, ReviewInput

SERVICE_LOCKS = {}


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    settings.resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = str(settings.resolved_db_path.resolve()) + ".service.lock"
    service_lock = SERVICE_LOCKS.setdefault(lock_path, FileLock(lock_path, timeout=0, thread_local=False))
    # Initialization also honors offline maintenance; constructors perform migrations.
    with service_lock:
        store = Store(settings.resolved_db_path)
        gateway = ModelGateway(settings)
        retriever = Retriever(store, gateway)
        workflow = Workflow(store, retriever, gateway)
        operations = Operations(store)
        tasks = TaskQueue(store, retriever.index)
        security = Security(store)
        workflow.operations.require_trusted = settings.auth_mode == "accounts"

    @asynccontextmanager
    async def lifespan(app):
        service_lock.acquire()
        try:
            if settings.seed_demo:
                with workflow.lock:
                    seed(store)
                    seed_standards(operations)
            tasks.start()
            yield
        finally:
            try:
                tasks.stop()
                workflow.close()
            finally:
                service_lock.release()

    app = FastAPI(title="溯障 · 设备诊断与协同排障", version="0.4.0", lifespan=lifespan)
    app.state.store, app.state.workflow = store, workflow
    app.state.tasks, app.state.operations = tasks, operations
    app.state.security = security

    def authorize(request: Request, authorization: str | None = Header(default=None)):
        if settings.auth_mode == "accounts":
            bearer_token = None
            if authorization is not None:
                if not authorization.startswith("Bearer ") or not authorization[7:].strip():
                    raise HTTPException(401, "Bearer 令牌格式无效")
                bearer_token = authorization[7:]
            token = bearer_token or request.cookies.get("suzhang-session")
            user = security.authenticate(token)
            request.state.user = user
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if bearer_token is None:
                    check_origin(request)
                path = request.url.path
                readonly = path in {"/api/search", "/api/topology/common", "/api/auth/logout"}
                if path.startswith("/api/admin/"):
                    allowed = {"admin"}
                elif path.endswith("/review") or (path.startswith("/api/standards/") and path.endswith("/status")):
                    allowed = {"reviewer", "admin"}
                elif path.startswith(("/api/documents", "/api/indexes", "/api/tasks")) or path == "/api/topology/import":
                    allowed = {"admin"}
                else:
                    allowed = {"operator", "reviewer", "admin"}
                if not readonly and user["role"] not in allowed:
                    raise HTTPException(403, "当前角色无权执行此操作")
            elif request.url.path.startswith("/api/admin/") and user["role"] != "admin":
                raise HTTPException(403, "仅管理员可访问")
            return user
        if settings.api_token:
            provided = (authorization or "").removeprefix("Bearer ")
            if not secrets.compare_digest(provided, settings.api_token):
                raise HTTPException(401, "访问令牌无效或未提供")

    def check_origin(request):
        # Cookies are same-site strict; exact origin is additionally required for writes.
        origin = request.headers.get("origin")
        if origin != f"{request.base_url.scheme}://{request.base_url.netloc}":
            raise HTTPException(403, "请求来源不匹配，请从本站页面操作")

    def accounts_only():
        if settings.auth_mode != "accounts":
            raise HTTPException(404, "账号模式未启用")

    def identified(payload, request):
        if settings.auth_mode == "accounts":
            return payload.model_copy(update={"operator": security.actor(request.state.user)})
        return payload

    def actor(request):
        return request.state.user if settings.auth_mode == "accounts" else None

    @app.middleware("http")
    async def trace(request: Request, call_next):
        started = time.monotonic()
        request_id = uuid4().hex
        response = await call_next(request)
        elapsed = round((time.monotonic() - started) * 1000, 2)
        response.headers["X-Request-Id"] = request_id
        response.headers["Server-Timing"] = f"total;dur={elapsed}"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        logging.getLogger("suzhang").info("request=%s method=%s path=%s status=%d duration_ms=%s", request_id, request.method, request.url.path, response.status_code, elapsed)
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"detail": "记录不存在"})

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ModelUnavailable)
    async def model_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    auth = [Depends(authorize)]

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.4.0", "llm_configured": bool(settings.llm_base_url),
                "embedding_configured": bool(settings.embedding_base_url), "rerank_configured": bool(settings.rerank_url),
                "authentication": settings.auth_mode == "accounts" or bool(settings.api_token), "auth_mode": settings.auth_mode, "demo_data": settings.seed_demo,
                "task_worker_alive": bool(tasks.thread and tasks.thread.is_alive())}

    @app.post("/api/auth/login")
    def login(payload: LoginInput, request: Request):
        accounts_only()
        check_origin(request)
        token, user = security.login(payload, request.client.host if request.client else "unknown")
        response = JSONResponse(user)
        response.set_cookie("suzhang-session", token, httponly=True, secure=settings.cookie_secure, samesite="strict", max_age=43200, path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/auth/me", dependencies=auth)
    def me(request: Request):
        accounts_only()
        return request.state.user

    @app.post("/api/auth/logout", dependencies=auth)
    def logout(request: Request):
        accounts_only()
        security.logout(request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get("suzhang-session"))
        response = JSONResponse({"ok": True})
        response.delete_cookie("suzhang-session", path="/")
        return response

    @app.get("/api/admin/users", dependencies=auth)
    def users():
        accounts_only()
        return security.users()

    @app.post("/api/admin/users", dependencies=auth)
    def create_user(payload: UserInput, request: Request):
        accounts_only()
        return security.create_user(payload, request.state.user)

    @app.post("/api/admin/users/{user_id}/status", dependencies=auth)
    def user_status(user_id: str, payload: UserUpdate, request: Request):
        accounts_only()
        return security.update_user(user_id, payload, request.state.user)

    @app.post("/api/admin/users/{user_id}/password", dependencies=auth)
    def reset_password(user_id: str, payload: PasswordInput, request: Request):
        accounts_only()
        return security.reset_password(user_id, payload.password, request.state.user)

    @app.get("/api/admin/audit", dependencies=auth)
    def audit(limit: int = Query(default=100, ge=1, le=1000)):
        accounts_only()
        with store.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]

    @app.get("/api/devices", dependencies=auth)
    def devices():
        return store.nodes()

    @app.get("/api/topology", dependencies=auth)
    def topology():
        return {"nodes": store.nodes(), "edges": store.edges()}

    @app.post("/api/topology/common", dependencies=auth)
    def common(request: ClarifyInput):
        return Topology(store).common(request.device_ids)

    @app.post("/api/topology/import", dependencies=auth)
    def import_topology(request: TopologyInput):
        return Topology(store).import_graph(request)

    @app.get("/api/measurements", dependencies=auth)
    def measurements():
        return store.measurements([n["id"] for n in store.nodes()])

    @app.post("/api/measurements", dependencies=auth)
    def add_measurement(request: MeasurementInput):
        try:
            timestamp = datetime.fromisoformat(request.observed_at)
            if timestamp.tzinfo is None:
                raise ValueError("timezone required")
        except ValueError as exc:
            raise ValueError("observed_at 须为包含时区的 ISO 8601 时间") from exc
        item = {**request.model_dump(), "unit": "MPa", "is_demo": False,
                "observed_at": timestamp.astimezone(timezone.utc).isoformat()}
        store.add_measurement(item)
        return item

    @app.get("/api/documents", dependencies=auth)
    def documents():
        return [{k: v for k, v in d.items() if k != "pages"} for d in store.documents()]

    @app.get("/api/documents/{doc_id}", dependencies=auth)
    def document(doc_id: str):
        return store.document(doc_id)

    @app.post("/api/documents/{doc_id}/status", dependencies=auth)
    def document_status(doc_id: str, request: DocumentStatusInput):
        with workflow.lock:
            return store.document_status(doc_id, request.active)

    @app.get("/api/indexes", dependencies=auth)
    def index_status():
        return retriever.index.status()

    @app.post("/api/indexes/embeddings/build", dependencies=auth)
    def build_embeddings():
        return retriever.index.build()

    @app.post("/api/documents", dependencies=auth)
    def add_document(request: DocumentInput):
        return ingest(store, request)

    @app.post("/api/documents/upload", dependencies=auth)
    def upload(file: UploadFile = File(...), model: str = Form(...), version: str = Form("1.0")):
        content = file.file.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "文件上限为 10 MB")
        pages = parse_file(file.filename or "upload", content)
        meta = DocumentInput(title=file.filename or "导入资料", model=model, version=version, text="\n".join(pages))
        return ingest(store, meta, pages=pages, filename=file.filename)

    @app.get("/api/tasks", dependencies=auth)
    def task_list():
        return tasks.list()

    @app.get("/api/tasks/{job_id}", dependencies=auth)
    def task_detail(job_id: str):
        return tasks.get(job_id)

    @app.post("/api/tasks/{job_id}/cancel", dependencies=auth)
    def task_cancel(job_id: str):
        return tasks.cancel(job_id)

    @app.post("/api/tasks/{job_id}/retry", dependencies=auth)
    def task_retry(job_id: str):
        return tasks.retry(job_id)

    @app.post("/api/tasks/embeddings", status_code=202, dependencies=auth)
    def task_embeddings():
        return tasks.enqueue("embedding", {})

    @app.post("/api/tasks/documents", status_code=202, dependencies=auth)
    def task_document(file: UploadFile = File(...), model: str = Form(...), version: str = Form("1.0")):
        content = file.file.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "文件上限为 10 MB")
        filename = file.filename or "upload"
        DocumentInput(title=filename, model=model, version=version, text="待后台解析")
        return tasks.enqueue("document", {"filename":filename,"model":model,"version":version,"content":base64.b64encode(content).decode()})

    @app.post("/api/alarms", dependencies=auth)
    def add_alarm(request: AlarmInput):
        with workflow.lock:
            return operations.add_alarm(request)

    @app.get("/api/alarms", dependencies=auth)
    def alarm_events(device_id: str | None = None, since: str | None = None, until: str | None = None,
                     active: bool = False, limit: int = Query(default=200,ge=1,le=1000)):
        return operations.alarms(device_id,since,until,active,limit)

    @app.get("/api/standards", dependencies=auth)
    def standards():
        return operations.standards()

    @app.post("/api/standards", dependencies=auth)
    def add_standard(request: StandardInput, http: Request):
        with workflow.lock:
            return operations.add_standard(request, submitted_by=actor(http), audit=security.audit)

    @app.post("/api/standards/{standard_id}/review", dependencies=auth)
    def review_standard(standard_id: str, payload: ReviewInput, request: Request):
        accounts_only()
        with workflow.lock:
            return operations.review_standard(standard_id, payload, request.state.user, audit=security.audit)

    @app.post("/api/standards/{standard_id}/status", dependencies=auth)
    def standard_status(standard_id: str, request: DocumentStatusInput, http: Request):
        with workflow.lock:
            return operations.standard_status(standard_id,request.active,actor(http),security.audit)

    @app.post("/api/search", dependencies=auth)
    def search(request: SearchInput):
        return retriever.search(request.query, [request.model] if request.model else None, request.version, request.limit)

    @app.post("/api/sessions", dependencies=auth)
    def start(request: StartInput, http: Request):
        return workflow.start(request, actor(http))

    @app.get("/api/sessions", dependencies=auth)
    def sessions():
        return workflow.sessions()

    @app.get("/api/sessions/{session_id}", dependencies=auth)
    def session(session_id: str):
        return workflow.session(session_id)

    @app.get("/api/sessions/{session_id}/export", dependencies=auth)
    def export(session_id: str):
        return {"session": workflow.session(session_id), "events": store.events(session_id)}

    @app.post("/api/sessions/{session_id}/clarify", dependencies=auth)
    def clarify(session_id: str, request: ClarifyInput, http: Request):
        return workflow.clarify(session_id, request, actor(http))

    @app.post("/api/sessions/{session_id}/feedback", dependencies=auth)
    def feedback(session_id: str, request: FeedbackInput, http: Request):
        return workflow.feedback(session_id, request, actor(http))

    @app.post("/api/sessions/{session_id}/confirm", dependencies=auth)
    def confirm(session_id: str, request: ConfirmInput, http: Request):
        return workflow.confirm(session_id, identified(request, http), actor(http))

    @app.post("/api/sessions/{session_id}/repair", dependencies=auth)
    def repair(session_id: str, request: RepairInput, http: Request):
        return workflow.repair(session_id, identified(request, http), actor(http))

    @app.post("/api/sessions/{session_id}/verify", dependencies=auth)
    def verify(session_id: str, request: VerifyInput, http: Request):
        return workflow.verify(session_id, identified(request, http), actor(http))

    app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "app" / "static" / "index.html")

    return app
