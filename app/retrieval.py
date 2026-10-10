import math
import re
from collections import Counter

from .models import ModelGateway, ModelUnavailable
from .store import Store


def tokens(text):
    text = text.lower()
    words = re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)*", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(run)
        words.extend(run[i:i + 2] for i in range(len(run) - 1))
    return words


def bm25(query, chunks):
    if not chunks:
        return []
    docs = [Counter(tokens(c["text"] + " " + c["title"])) for c in chunks]
    df = Counter(t for doc in docs for t in doc)
    avg = sum(sum(doc.values()) for doc in docs) / len(docs) or 1
    output = []
    for chunk, doc in zip(chunks, docs):
        score = 0
        for t in set(tokens(query)):
            f = doc[t]
            idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * f * 2.5 / (f + 1.5 * (0.25 + 0.75 * sum(doc.values()) / avg))
        if score > 0:
            output.append({**chunk, "bm25_score": score})
    return sorted(output, key=lambda c: (-c["bm25_score"], c["id"]))


def rrf(rankings, k=60):
    scores, by_id = Counter(), {}
    for ranking in rankings:
        for rank, c in enumerate(ranking, 1):
            scores[c["id"]] += 1 / (k + rank)
            by_id[c["id"]] = {**by_id.get(c["id"], {}), **c}
    return [{**by_id[cid], "score": score} for cid, score in sorted(scores.items(), key=lambda row: (-row[1], row[0]))]


def cosine(a, b):
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / norm if norm else 0


class Retriever:
    def __init__(self, store: Store, gateway: ModelGateway):
        self.store, self.gateway = store, gateway

    def search(self, query, models=None, version=None, limit=5):
        chunks = self.store.chunks(models, version)
        warnings = []
        code = re.search(r"(?<![A-Za-z0-9])(?:[A-Za-z]{1,5}[-_]?)\d{2,6}(?![A-Za-z0-9])", query)
        exact = [c for c in chunks if code and re.search(r"(?<![A-Za-z0-9])" + re.escape(code.group()) + r"(?![A-Za-z0-9])", c["text"], re.I)]
        route = "exact_code" if exact else "bm25"
        lexical = bm25(query, exact or chunks)[:30]
        rankings = [lexical]
        if self.gateway.settings.embedding_base_url and chunks and not exact:
            # Small corpus only: cache-free batching avoids stale document/model vectors.
            # A production adapter should replace this with a persistent vector index.
            if len(chunks) > 500:
                warnings.append("本地向量适配器限 500 个切片，当前降级为 BM25；请接入向量索引")
            else:
                try:
                    vectors = self.gateway.embeddings([query] + [c["text"] for c in chunks])
                    dense = sorted([{**c, "dense_score": cosine(vectors[0], v)} for c, v in zip(chunks, vectors[1:])], key=lambda c: c["dense_score"], reverse=True)[:30]
                    rankings.append(dense)
                    route = "hybrid_rrf"
                except ModelUnavailable as exc:
                    warnings.append(str(exc) + "，已降级为 BM25")
        merged = rrf(rankings)
        if self.gateway.settings.rerank_url and merged:
            try:
                merged = self.gateway.rerank(query, merged[:20])
                route += "+rerank"
            except ModelUnavailable as exc:
                warnings.append(str(exc) + "，保留原排序")
        return {"route": route, "warnings": warnings, "total_candidates": len(chunks), "items": merged[:limit]}
