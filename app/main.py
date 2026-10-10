import base64
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, Query
from fastapi.responses import FileResponse, JSONResponse
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


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = Store(settings.resolved_db_path)
    gateway = ModelGateway(settings)
    retriever = Retriever(store, gateway)
    workflow = Workflow(store, retriever, gateway)
    operations = Operations(store)
    tasks = TaskQueue(store, retriever.index)

    @asynccontextmanager
    async def lifespan(app):
        if settings.seed_demo:
            with workflow.lock:
                seed(store)
                seed_standards(operations)
        tasks.start()
        try:
            yield
        finally:
            tasks.stop()
            workflow.close()

    app = FastAPI(title="溯障 · 设备诊断与协同排障", version="0.3.0", lifespan=lifespan)
    app.state.store, app.state.workflow = store, workflow
    app.state.tasks, app.state.operations = tasks, operations

    def authorize(authorization: str | None = Header(default=None)):
        if settings.api_token:
            provided = (authorization or "").removeprefix("Bearer ")
            if not secrets.compare_digest(provided, settings.api_token):
                raise HTTPException(401, "访问令牌无效或未提供")

    @app.middleware("http")
    async def trace(request: Request, call_next):
        started = time.monotonic()
        request_id = uuid4().hex
        response = await call_next(request)
        elapsed = round((time.monotonic() - started) * 1000, 2)
        response.headers["X-Request-Id"] = request_id
        response.headers["Server-Timing"] = f"total;dur={elapsed}"
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
        return {"status": "ok", "version": "0.3.0", "llm_configured": bool(settings.llm_base_url),
                "embedding_configured": bool(settings.embedding_base_url), "rerank_configured": bool(settings.rerank_url),
                "authentication": bool(settings.api_token), "demo_data": settings.seed_demo,
                "task_worker_alive": bool(tasks.thread and tasks.thread.is_alive())}

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
    def add_standard(request: StandardInput):
        with workflow.lock:
            return operations.add_standard(request)

    @app.post("/api/standards/{standard_id}/status", dependencies=auth)
    def standard_status(standard_id: str, request: DocumentStatusInput):
        with workflow.lock:
            return operations.standard_status(standard_id,request.active)

    @app.post("/api/search", dependencies=auth)
    def search(request: SearchInput):
        return retriever.search(request.query, [request.model] if request.model else None, request.version, request.limit)

    @app.post("/api/sessions", dependencies=auth)
    def start(request: StartInput):
        return workflow.start(request)

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
    def clarify(session_id: str, request: ClarifyInput):
        return workflow.clarify(session_id, request)

    @app.post("/api/sessions/{session_id}/feedback", dependencies=auth)
    def feedback(session_id: str, request: FeedbackInput):
        return workflow.feedback(session_id, request)

    @app.post("/api/sessions/{session_id}/confirm", dependencies=auth)
    def confirm(session_id: str, request: ConfirmInput):
        return workflow.confirm(session_id, request)

    @app.post("/api/sessions/{session_id}/repair", dependencies=auth)
    def repair(session_id: str, request: RepairInput):
        return workflow.repair(session_id, request)

    @app.post("/api/sessions/{session_id}/verify", dependencies=auth)
    def verify(session_id: str, request: VerifyInput):
        return workflow.verify(session_id, request)

    app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "app" / "static" / "index.html")

    return app
