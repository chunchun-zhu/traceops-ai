"""Evaluate retrieval on caller-supplied labels; defaults are synthetic smoke cases."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ROOT, Settings
from app.models import ModelGateway
from app.retrieval import Retriever
from app.seed import seed
from app.store import Store


def evaluate(retriever, cases, k):
    results = []
    for case in cases:
        response = retriever.search(case["query"], [case["model"]] if case.get("model") else None, case.get("version"), limit=k)
        relevant = set(case["relevant_titles"])
        # Document-level labels: deduplicate chunks from the same source document.
        titles = list(dict.fromkeys(c["title"] for c in response["items"]))
        ranks = [i for i, title in enumerate(titles, 1) if title in relevant]
        hit = bool(ranks)
        results.append({"query": case["query"], "hit": hit, "recall": len(relevant.intersection(titles)) / len(relevant) if relevant else 0,
                        "rr": 1 / min(ranks) if ranks else 0, "retrieved_titles": titles,
                        "route": response["route"], "warnings": response["warnings"]})
    count = len(results)
    if not count:
        raise ValueError("评测集为空")
    return {"label_level": "document_title", "cutoff": f"top {k} chunks deduplicated into documents", "sample_count": count,
            f"Hit@{k}": sum(r["hit"] for r in results) / count,
            f"Recall@{k}": sum(r["recall"] for r in results) / count,
            f"MRR@{k}": sum(r["rr"] for r in results) / count, "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "examples" / "retrieval-cases.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--k", type=int, default=5, choices=range(1,21))
    args = parser.parse_args()
    settings = Settings()
    store = Store(settings.resolved_db_path)
    if settings.seed_demo:
        seed(store)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    report = {"dataset": str(args.cases), "note": "默认样本为模拟冒烟评测，不代表真实工厂效果或简历历史指标。", **evaluate(Retriever(store, ModelGateway(settings)), cases, args.k)}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output)
