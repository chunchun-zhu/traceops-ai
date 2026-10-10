import json
import os
import subprocess
import sys

import pytest

from app.config import ROOT
from app.evaluation import EvaluationDataset,evaluate_dataset,markdown_report


def small_dataset():
    return {"name":"模拟评测","revision":"v1","kind":"synthetic","provenance":"测试自编模拟数据",
            "retrieval":[{"id":"positive","query":"E101","model":"P-200","relevant_titles":["冲压机供气检查说明（模拟）"]},
                         {"id":"negative","query":"E99999","expect_empty":True}],
            "workflows":[{"id":"wrong-expectation","start":{"device_ids":["PRESS-01"],"symptom":"气压不足"},"expected":{"status":"resolved"}}]}


def test_metrics_negative_samples_and_failure_details_in_isolation(tmp_path,monkeypatch):
    live=tmp_path/"do-not-touch.db";live.write_bytes(b"untouched")
    monkeypatch.setenv("SUZHANG_DB_PATH",str(live))
    monkeypatch.setenv("SUZHANG_LLM_BASE_URL","http://must-not-be-called/v1")
    report=evaluate_dataset(EvaluationDataset.model_validate(small_dataset()))
    assert live.read_bytes()==b"untouched"
    assert report["summary"]=={"total":3,"passed":2,"failed":1,"pass_rate":2/3}
    assert report["retrieval_metrics"]["Hit@5"]==1 and report["retrieval_metrics"]["negative_pass_rate"]==1
    assert report["retrieval_metrics"]["positive_count"]==1
    assert report["results"][-1]["checks"][0]["actual"]=="checking"
    assert "失败详情" in markdown_report(report) and report["dataset"]["sha256"]


@pytest.mark.parametrize("change",["empty","duplicate","unlabelled","annotated_seed","unknown_action"])
def test_dataset_contract_rejects_ambiguous_cases(change):
    data=small_dataset()
    if change=="empty":data["retrieval"]=[];data["workflows"]=[]
    elif change=="duplicate":data["workflows"][0]["id"]="positive"
    elif change=="unlabelled":data["retrieval"][0]["relevant_titles"]=[]
    elif change=="annotated_seed":data["kind"]="annotated"
    else:data["workflows"][0]["steps"]=[{"action":"execute_arbitrary_code"}]
    with pytest.raises(ValueError):EvaluationDataset.model_validate(data)


def test_cli_failed_expectation_returns_nonzero_and_writes_both_reports(tmp_path):
    cases=tmp_path/"cases.json";cases.write_text(json.dumps(small_dataset()),encoding="utf-8")
    output=tmp_path/"report.json";md=tmp_path/"report.md"
    command=[sys.executable,str(ROOT/"scripts/evaluate_e2e.py"),"--cases",str(cases),"--output",str(output),"--markdown",str(md)]
    result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=60,
                          env={**os.environ,"LANGSMITH_TRACING":"false","LANGCHAIN_TRACING_V2":"false"})
    assert result.returncode==1,result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["failed"]==1
    assert md.exists()
