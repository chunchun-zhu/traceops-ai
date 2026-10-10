import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConflictError(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS alarms (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, event_id TEXT NOT NULL,
                    device_id TEXT NOT NULL REFERENCES nodes(id), code TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(source,event_id)
                );
                CREATE INDEX IF NOT EXISTS alarm_time ON alarms(device_id,occurred_at);
                CREATE TABLE IF NOT EXISTS standards (
                    id TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, dedupe_key TEXT NOT NULL,
                    status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, input TEXT NOT NULL, progress TEXT NOT NULL,
                    result TEXT, error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS job_dedupe ON jobs(dedupe_key)
                    WHERE status IN ('queued','running','committing');
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edges (
                    source TEXT NOT NULL REFERENCES nodes(id),
                    target TEXT NOT NULL REFERENCES nodes(id),
                    PRIMARY KEY(source,target)
                );
                CREATE TABLE IF NOT EXISTS measurements (
                    id INTEGER PRIMARY KEY, node_id TEXT NOT NULL REFERENCES nodes(id),
                    observed_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS measurement_time ON measurements(node_id,observed_at);
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
                    model TEXT NOT NULL, version TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(session_id,revision)
                );
                CREATE TABLE IF NOT EXISTS index_stats (
                    chunk_id TEXT PRIMARY KEY REFERENCES chunks(id), length INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_terms (
                    chunk_id TEXT NOT NULL REFERENCES chunks(id), term TEXT NOT NULL, frequency INTEGER NOT NULL,
                    PRIMARY KEY(chunk_id,term)
                );
                CREATE INDEX IF NOT EXISTS term_lookup ON index_terms(term);
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id TEXT NOT NULL REFERENCES chunks(id), model_key TEXT NOT NULL,
                    dimension INTEGER NOT NULL, vector TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(chunk_id,model_key)
                );
            """)
            if "active" not in {r[1] for r in db.execute("PRAGMA table_info(documents)")}:
                db.execute("ALTER TABLE documents ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def nodes(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM nodes ORDER BY id")]

    def edges(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT source,target FROM edges ORDER BY source,target")]

    def measurements(self, node_ids: list[str]):
        with self.connect() as db:
            result = []
            for node_id in node_ids:
                row = db.execute("SELECT payload FROM measurements WHERE node_id=? ORDER BY observed_at DESC,id DESC LIMIT 1", (node_id,)).fetchone()
                if row:
                    result.append(json.loads(row[0]))
            return result

    def add_measurement(self, item: dict):
        with self.connect() as db:
            if not db.execute("SELECT id FROM nodes WHERE id=?", (item["node_id"],)).fetchone():
                raise ValueError("测点所属节点不存在")
            db.execute("INSERT INTO measurements(node_id,observed_at,payload) VALUES(?,?,?)", (item["node_id"], item["observed_at"], json.dumps(item, ensure_ascii=False)))

    def documents(self):
        with self.connect() as db:
            return [{**json.loads(r[0]), "active": bool(r[1])} for r in db.execute("SELECT payload,active FROM documents ORDER BY rowid DESC")]

    def document(self, doc_id):
        with self.connect() as db:
            row = db.execute("SELECT payload,active FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not row:
                raise KeyError(doc_id)
            return {**json.loads(row[0]), "active": bool(row[1])}

    def document_status(self, doc_id, active):
        with self.connect() as db:
            if db.execute("UPDATE documents SET active=? WHERE id=?", (int(active), doc_id)).rowcount != 1:
                raise KeyError(doc_id)
        return self.document(doc_id)

    def chunks(self, models=None, version=None):
        sql, params = "SELECT chunks.payload FROM chunks JOIN documents ON documents.id=chunks.document_id WHERE documents.active=1", []
        if models:
            sql += " AND model IN (" + ",".join("?" for _ in models) + ")"
            params.extend(models)
        if version:
            sql += " AND version=?"
            params.append(version)
        sql += " ORDER BY chunks.id"
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute(sql, params)]

    def put_document(self, document, chunks):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM documents WHERE fingerprint=?", (document["fingerprint"],)).fetchone()
            if existing:
                return {**json.loads(existing[0]), "duplicate": True}
            db.execute("INSERT INTO documents(id,fingerprint,payload) VALUES(?,?,?)", (document["id"], document["fingerprint"], json.dumps(document, ensure_ascii=False)))
            for chunk in chunks:
                db.execute("INSERT INTO chunks VALUES(?,?,?,?,?)", (chunk["id"], document["id"], document["model"], document["version"], json.dumps(chunk, ensure_ascii=False)))
            from .indexing import index_chunks
            index_chunks(db, chunks)
        return {**document, "duplicate": False}

    def new_session(self, state):
        state = {**state, "id": uuid4().hex, "revision": 0, "created_at": now(), "updated_at": now()}
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (state["id"], 0, state["updated_at"], json.dumps(state, ensure_ascii=False)))
            db.execute("INSERT INTO events(session_id,revision,payload) VALUES(?,?,?)", (state["id"], 0, json.dumps({"type": "started", "at": now(), "state": state}, ensure_ascii=False)))
        return state

    def session(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise KeyError(session_id)
            return json.loads(row[0])

    def sessions(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM sessions ORDER BY updated_at DESC LIMIT 100")]

    def save_session(self, state, expected_revision, event):
        state = {**state, "revision": expected_revision + 1, "updated_at": now()}
        with self.connect() as db:
            cursor = db.execute("UPDATE sessions SET revision=?,updated_at=?,payload=? WHERE id=? AND revision=?", (state["revision"], state["updated_at"], json.dumps(state, ensure_ascii=False), state["id"], expected_revision))
            if cursor.rowcount != 1:
                raise ConflictError("会话已更新，请重新载入后提交；本次反馈未写入")
            db.execute("INSERT INTO events(session_id,revision,payload) VALUES(?,?,?)", (state["id"], state["revision"], json.dumps({**event, "at": now(), "state": state}, ensure_ascii=False)))
        return state

    def events(self, session_id):
        self.session(session_id)
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE session_id=? ORDER BY revision", (session_id,))]

    def project_session(self, state):
        """Materialize the checkpoint into the API projection and audit event atomically."""
        public = {k: v for k, v in state.items() if not k.startswith("_")}
        event = state.get("_event", {"type": "started", "at": now()})
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM sessions WHERE id=?", (public["id"],)).fetchone()
            if not row:
                raise KeyError(public["id"])
            if row[0] > public["revision"]:
                raise ConflictError("检查点版本落后于会话记录")
            db.execute("UPDATE sessions SET revision=?,updated_at=?,payload=? WHERE id=?", (public["revision"], public["updated_at"], json.dumps(public, ensure_ascii=False), public["id"]))
            db.execute("INSERT INTO events(session_id,revision,payload) VALUES(?,?,?) ON CONFLICT(session_id,revision) DO UPDATE SET payload=excluded.payload", (public["id"], public["revision"], json.dumps({**event, "state": public}, ensure_ascii=False)))
        return public
