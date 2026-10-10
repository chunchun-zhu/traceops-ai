"""Run bounded dataset scenarios in disposable SQLite databases, never the live DB."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ["LANGSMITH_TRACING"]="false"
os.environ["LANGCHAIN_TRACING_V2"]="false"

from app.config import ROOT
from app.evaluation import EvaluationDataset,evaluate_dataset,markdown_report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases",type=Path,default=ROOT/"examples/e2e-cases.json")
    parser.add_argument("--output",type=Path)
    parser.add_argument("--markdown",type=Path)
    parser.add_argument("--k",type=int,choices=range(1,21),default=5)
    parser.add_argument("--min-pass-rate",type=float,default=1.0)
    parser.add_argument("--live-models",action="store_true",help="Explicitly use configured model endpoints; requires matching dataset expectations")
    args=parser.parse_args()
    if not 0<=args.min_pass_rate<=1:parser.error("min-pass-rate 须为 0–1")
    try:
        if args.cases.stat().st_size>5*1024*1024:raise ValueError("评测集超过 5 MB")
        dataset=EvaluationDataset.model_validate_json(args.cases.read_bytes())
        report=evaluate_dataset(dataset,args.k,args.live_models)
    except (ValueError,OSError) as exc:
        parser.error(str(exc))
    output=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(output,encoding="utf-8")
    else:print(output)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True,exist_ok=True)
        args.markdown.write_text(markdown_report(report),encoding="utf-8")
    return 0 if report["summary"]["pass_rate"]>=args.min_pass_rate else 1


if __name__=="__main__":
    raise SystemExit(main())
