"""Durable local queue with a single elected worker per database."""
import base64
import hashlib
import json
import threading
from uuid import uuid4

from filelock import FileLock, Timeout

from .ingest import ingest, parse_file
from .schemas import DocumentInput
from .store import ConflictError, now


class TaskCancelled(Exception):
    pass


class TaskQueue:
    def __init__(self, store, index):
        self.store, self.index = store, index
        self.stop_event = threading.Event()
        self.thread = None
        self.worker_lock = FileLock(str(store.path)+".tasks.lock",timeout=0)

    @staticmethod
    def public(row):
        item = dict(row)
        item.pop("input",None)
        item.pop("dedupe_key",None)
        for key in ("progress","result"):
            item[key] = json.loads(item[key]) if item[key] else None
        item["cancel_requested"] = bool(item["cancel_requested"])
        return item

    def list(self):
        with self.store.connect() as db:
            return [self.public(r) for r in db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100")]

    def get(self, job_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            return self.public(row)

    def enqueue(self, kind, payload):
        if kind not in {"document","embedding"}:
            raise ValueError("不支持的任务类型")
        if kind == "embedding":
            if not self.index.gateway.settings.embedding_base_url:
                raise ValueError("请先配置 Embedding 服务；此操作不会部署模型")
            payload = {"model_key":self.index.model_key}
        encoded = json.dumps(payload,ensure_ascii=False,sort_keys=True)
        key = hashlib.sha256((kind+encoded).encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE dedupe_key=? AND status IN ('queued','running','committing')",(key,)).fetchone()
            if row:
                return {**self.public(row),"duplicate":True}
            stamp, job_id = now(), uuid4().hex
            db.execute("INSERT INTO jobs(id,kind,dedupe_key,status,created_at,updated_at,input,progress) VALUES(?,?,?,'queued',?,?,?,?)",(job_id,kind,key,stamp,stamp,encoded,'{"stage":"queued","completed":0,"total":0}'))
        return {**self.get(job_id),"duplicate":False}

    def cancel(self, job_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row[0] not in {"queued","running"}:
                raise ConflictError("仅排队或执行中的任务可取消；已提交批次保留")
            db.execute("UPDATE jobs SET cancel_requested=1,status=?,updated_at=? WHERE id=?",("cancelled" if row[0]=="queued" else "running",now(),job_id))
        return self.get(job_id)

    def retry(self, job_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row["status"] not in {"failed","cancelled"}:
                raise ConflictError("仅失败或取消的任务可重试")
            if row["kind"]=="embedding" and json.loads(row["input"])["model_key"] != self.index.model_key:
                raise ConflictError("模型配置已变化，请创建新构建任务")
            if db.execute("SELECT id FROM jobs WHERE dedupe_key=? AND status IN ('queued','running','committing')",(row["dedupe_key"],)).fetchone():
                raise ConflictError("已有相同任务正在执行")
            db.execute("UPDATE jobs SET status='queued',cancel_requested=0,error=NULL,result=NULL,updated_at=? WHERE id=?",(now(),job_id))
        return self.get(job_id)

    def recover(self):
        # Called only after obtaining the worker lock; another live worker is never reset.
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'queued' END,updated_at=? WHERE status='running'",(now(),))
            # A committing document is replayed idempotently; cancellation is closed in this phase.

    def claim(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status IN ('queued','committing') ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status=?,attempts=attempts+1,updated_at=? WHERE id=?",("committing" if row["status"]=="committing" else "running",now(),row["id"]))
            return dict(row)

    def checkpoint(self, job_id, completed=0, total=0, stage="running"):
        if self.stop_event.is_set():
            raise InterruptedError("服务停止，等待下次启动恢复")
        with self.store.connect() as db:
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?",(job_id,)).fetchone()
            if row[0]:
                raise TaskCancelled()
            db.execute("UPDATE jobs SET progress=?,updated_at=? WHERE id=?",(json.dumps({"stage":stage,"completed":completed,"total":total}),now(),job_id))

    def commit_phase(self, job_id):
        self.checkpoint(job_id,stage="ready_to_commit")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT cancel_requested FROM jobs WHERE id=?",(job_id,)).fetchone()
            if row[0]:
                raise TaskCancelled()
            db.execute("UPDATE jobs SET status='committing',updated_at=? WHERE id=?",(now(),job_id))

    def finish(self, job_id, status, result=None, error=None):
        with self.store.connect() as db:
            if status == "succeeded":
                total = result.get("total",result.get("chunk_count",0))
                db.execute("UPDATE jobs SET progress=? WHERE id=?",(json.dumps({"stage":"done","completed":total,"total":total}),job_id))
            db.execute("UPDATE jobs SET status=?,result=?,error=?,updated_at=? WHERE id=?",(status,json.dumps(result,ensure_ascii=False) if result is not None else None,error,now(),job_id))

    def execute(self, job):
        job_id, payload = job["id"], json.loads(job["input"])
        try:
            if job["kind"]=="document":
                self.checkpoint(job_id,stage="parsing")
                raw=base64.b64decode(payload["content"])
                pages=parse_file(payload["filename"],raw)
                meta=DocumentInput(title=payload["filename"],model=payload["model"],version=payload["version"],text="\n".join(pages))
                self.commit_phase(job_id)
                result=ingest(self.store,meta,pages=pages,filename=payload["filename"])
                result={k:v for k,v in result.items() if k!="pages"}
            else:
                if payload["model_key"]!=self.index.model_key:
                    raise ValueError("模型配置已变化，请创建新构建任务")
                result=self.index.build(progress=lambda done,total:self.checkpoint(job_id,done,total,"embedding"))
            self.finish(job_id,"succeeded",result)
        except TaskCancelled:
            self.finish(job_id,"cancelled")
        except InterruptedError:
            with self.store.connect() as db:
                db.execute("UPDATE jobs SET status='queued',updated_at=? WHERE id=? AND status='running'",(now(),job_id))
        except Exception as exc:
            # Exception text may contain server URLs or credentials; return a bounded category only.
            error=str(exc)[:1000] if isinstance(exc,ValueError) else "任务失败："+type(exc).__name__+"；检查服务配置后重试"
            self.finish(job_id,"failed",error=error)

    def run(self):
        while not self.stop_event.is_set():
            try:
                with self.worker_lock:
                    self.recover()
                    while not self.stop_event.is_set():
                        job=self.claim()
                        if job:
                            self.execute(job)
                        else:
                            self.stop_event.wait(0.25)
            except Timeout:
                self.stop_event.wait(0.25)

    def start(self):
        self.thread=threading.Thread(target=self.run,name="suzhang-task-worker",daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
