import hashlib
import json
import math
import re
from collections import Counter

from .models import ModelUnavailable
from .store import now


def tokens(text):
    text = text.lower()
    words = re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(run)
        words.extend(run[i:i + 2] for i in range(len(run) - 1))
    return words


def index_chunks(db, chunks):
    for chunk in chunks:
        counts = Counter(tokens(chunk["text"] + " " + chunk["title"]))
        db.execute("INSERT OR IGNORE INTO index_stats VALUES(?,?)", (chunk["id"], sum(counts.values())))
        db.executemany("INSERT OR IGNORE INTO index_terms VALUES(?,?,?)", [(chunk["id"], term, f) for term, f in counts.items()])


class PersistentIndex:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway
        with store.connect() as db:
            missing = [json.loads(r[0]) for r in db.execute("SELECT payload FROM chunks WHERE id NOT IN (SELECT chunk_id FROM index_stats)")]
            index_chunks(db, missing)

    @property
    def model_key(self):
        s = self.gateway.settings
        return hashlib.sha256(json.dumps([s.embedding_base_url.rstrip("/"), s.embedding_model, "embedding-v1"]).encode()).hexdigest()

    def lexical(self, query, chunks):
        by_id = {c["id"]: c for c in chunks}
        query_terms = set(tokens(query))
        if not by_id or not query_terms:
            return []
        with self.store.connect() as db:
            lengths = {r[0]: r[1] for r in db.execute("SELECT chunk_id,length FROM index_stats") if r[0] in by_id}
            rows = [r for r in db.execute("SELECT chunk_id,term,frequency FROM index_terms WHERE term IN (" + ",".join("?" for _ in query_terms) + ")", list(query_terms)) if r[0] in by_id]
        df = Counter(r[1] for r in rows)
        avg = sum(lengths.values()) / len(by_id) or 1
        scores = Counter()
        for cid, term, frequency in rows:
            idf = math.log(1 + (len(by_id) - df[term] + 0.5) / (df[term] + 0.5))
            scores[cid] += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * lengths[cid] / avg))
        return [{**by_id[cid], "bm25_score": score} for cid, score in sorted(scores.items(), key=lambda r: (-r[1], r[0]))]

    def vectors(self, chunks):
        wanted = {c["id"] for c in chunks}
        with self.store.connect() as db:
            return {r[0]: json.loads(r[1]) for r in db.execute("SELECT chunk_id,vector FROM embeddings WHERE model_key=?", (self.model_key,)) if r[0] in wanted}

    def build(self, batch_size=32):
        if not self.gateway.settings.embedding_base_url:
            raise ValueError("请先配置 Embedding 服务；此操作不会部署模型")
        chunks = self.store.chunks()
        cached = self.vectors(chunks)
        missing = [c for c in chunks if c["id"] not in cached]
        dimensions = {len(v) for v in cached.values()}
        added = 0
        for offset in range(0, len(missing), batch_size):
            batch = missing[offset:offset + batch_size]
            vectors = self.gateway.embeddings([c["text"] for c in batch])
            dimensions.add(len(vectors[0]))
            if len(dimensions) != 1:
                raise ModelUnavailable("同一向量配置返回不同维数，请修改模型标识后重新构建")
            with self.store.connect() as db:
                db.executemany("INSERT OR IGNORE INTO embeddings VALUES(?,?,?,?,?)", [(c["id"], self.model_key, len(v), json.dumps(v), now()) for c, v in zip(batch, vectors)])
            added += len(batch)
        return {"total": len(chunks), "added": added, "reused": len(cached), "model_key": self.model_key}

    def status(self):
        chunks = self.store.chunks()
        vectors = self.vectors(chunks)
        return {"active_chunks": len(chunks), "vectors_ready": len(vectors), "vectors_missing": len(chunks)-len(vectors),
                "model_key": self.model_key, "embedding_configured": bool(self.gateway.settings.embedding_base_url)}
