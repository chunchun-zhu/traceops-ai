"""Isolated, data-driven API evaluation. Default fixtures never call a model."""
import hashlib
import json
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import Settings
from .main import create_app
from .models import ModelGateway, ModelUnavailable
from .schemas import DocumentInput, StartInput, TopologyInput
from .store import now


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalCase(Contract):
    id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    model: str | None = None
    version: str | None = None
    relevant_titles: list[str] = Field(default_factory=list, max_length=50)
    expect_empty: bool = False
    expected_route: str | None = None

    @model_validator(mode="after")
    def labels(self):
        if bool(self.relevant_titles) == self.expect_empty or len(set(self.relevant_titles)) != len(self.relevant_titles):
            raise ValueError("须填写不重复的相关标题，或明确 expect_empty=true")
        return self


class WorkflowStep(Contract):
    action: Literal["clarify","feedback","confirm","repair","verify","restart","export","clear_alarms","document_status","standard_status"]
    payload: dict[str, Any] = Field(default_factory=dict)
    target_model: str | None = Field(default=None, max_length=100)
    expected_http: int = Field(default=200, ge=200, le=499)
    expected: dict[str, Any] = Field(default_factory=dict, max_length=20)


class WorkflowCase(Contract):
    id: str = Field(min_length=1, max_length=100)
    start: StartInput
    expected: dict[str, Any] = Field(min_length=1, max_length=20)
    steps: list[WorkflowStep] = Field(default_factory=list, max_length=100)
    with_alarms: bool = False
    disabled_document_models: list[str] = Field(default_factory=list, max_length=50)
    model_fixture: Literal["valid","unknown_id","changed_quote","unbound","legacy","extra_field","timeout"] | None = None


class EvaluationDataset(Contract):
    name: str = Field(min_length=1, max_length=100)
    revision: str = Field(min_length=1, max_length=100)
    kind: Literal["synthetic","annotated"]
    provenance: str = Field(min_length=1, max_length=2000)
    seed_demo: bool = True
    topology: TopologyInput | None = None
    documents: list[DocumentInput] = Field(default_factory=list, max_length=100)
    retrieval: list[RetrievalCase] = Field(default_factory=list, max_length=200)
    workflows: list[WorkflowCase] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_cases(self):
        ids=[c.id for c in self.retrieval+self.workflows]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("评测案例须非空且 ID 全局唯一")
        if self.kind == "annotated" and self.seed_demo:
            raise ValueError("人工标注集须 seed_demo=false，显式提供资料与台账")
        return self


class FixtureGateway(ModelGateway):
    """Generate labelled protocol fixtures, not simulated recognition quality."""
    def __init__(self, settings, fixture):
        super().__init__(settings)
        self.fixture = fixture

    def post(self, url, payload, key=""):
        if self.fixture == "timeout":
            raise ModelUnavailable("模型服务不可用、超时或返回格式异常")
        context=json.loads(payload["messages"][-1]["content"])
        source=context["evidence"][0]
        quote=source["text"][:120]
        cite={"evidence_id":source["id"],"quote":quote,"start":0,"end":len(quote)}
        output={"claims":[{"text":"接口替身：资料列出的供气候选仍需现场人工核验。","citations":[cite]}]}
        if self.fixture=="unknown_id":cite["evidence_id"]="not-in-context"
        elif self.fixture=="changed_quote":cite["quote"]=quote[:-1]+("X" if quote[-1]!="X" else "Y")
        elif self.fixture=="unbound":output["claims"][0]["citations"]=[]
        elif self.fixture=="legacy":output={"summary":"旧协议替身","evidence_ids":[source["id"]]}
        elif self.fixture=="extra_field":cite["page"]=99
        return {"choices":[{"message":{"content":json.dumps(output,ensure_ascii=False)}}]}


def read_path(value, path):
    for key in path.split("."):
        value=value[int(key)] if isinstance(value,list) else value[key]
    return value


def check_expected(value, expected):
    checks=[]
    for path,wanted in expected.items():
        try:
            actual=read_path(value,path)
            passed=type(actual) is type(wanted) and actual==wanted
        except (KeyError,IndexError,ValueError,TypeError):
            actual="<missing>";passed=False
        checks.append({"path":path,"expected":wanted,"actual":actual,"passed":passed})
    return checks


def fixture_settings(path, dataset, live_models=False, fixture=None):
    if live_models:
        settings=Settings()
    else:
        # Explicit values override any model endpoints/keys inherited from the shell.
        settings=Settings(_env_file=None,llm_base_url="",embedding_base_url="",rerank_url="",ocr_url="",
                          llm_api_key="",embedding_api_key="",rerank_api_key="",ocr_api_key="")
    return settings.model_copy(update={"db_path":Path(path),"seed_demo":dataset.seed_demo,"auth_mode":"legacy",
                                       "api_token":"","llm_base_url":"http://fixture.invalid/v1" if fixture else settings.llm_base_url})


def open_fixture(settings, dataset, fixture=None, initialize=True):
    app=create_app(settings)
    if fixture:
        app.state.workflow.gateway=FixtureGateway(settings,fixture)
    client=TestClient(app)
    client.__enter__()
    try:
        if initialize:
            if dataset.topology:
                response=client.post("/api/topology/import",json=dataset.topology.model_dump())
                if response.status_code!=200:raise ValueError("评测台账初始化失败")
            for document in dataset.documents:
                if client.post("/api/documents",json=document.model_dump()).status_code!=200:
                    raise ValueError("评测文档初始化失败")
        return client
    except Exception:
        client.__exit__(None,None,None)
        raise


def lookup(client, kind, model):
    rows=client.get("/api/"+kind).json()
    matching=[r for r in rows if r["model"]==model and (kind!="documents" or r["kind"]=="manual")]
    if len(matching)!=1:
        raise ValueError("占位符须唯一匹配一个标准或手册")
    return matching[0]["id"]


def expand(value, client, state):
    if isinstance(value,list):return [expand(v,client,state) for v in value]
    if isinstance(value,dict):return {k:expand(v,client,state) for k,v in value.items()}
    if isinstance(value,str) and value.startswith("${") and value.endswith("}"):
        key=value[2:-1]
        if key=="revision":return state["revision"]
        if key=="check_id":return state["current_check"]["id"]
        if key=="now":return now()
        if key.startswith("standard:"):return lookup(client,"standards",key.split(":",1)[1])
        if key.startswith("document:"):return lookup(client,"documents",key.split(":",1)[1])
        raise ValueError("未知评测占位符")
    return value


def run_workflow(case, dataset, directory, live_models=False):
    settings=fixture_settings(Path(directory)/"case.db",dataset,live_models,case.model_fixture)
    client=open_fixture(settings,dataset,case.model_fixture)
    checks=[]
    mode="mock" if case.model_fixture else "live" if live_models else "rule"
    try:
        for document in client.get("/api/documents").json():
            if document["model"] in case.disabled_document_models:
                client.post(f'/api/documents/{document["id"]}/status',json={"active":False}).raise_for_status()
        request=case.start.model_dump()
        if case.with_alarms:
            alarm_ids=[]
            for i,device in enumerate(request["device_ids"]):
                raised=client.post("/api/alarms",json={"event_id":f"raised-{i}","device_id":device,"code":"E101",
                                   "status":"raised","occurred_at":now(),"source":"evaluation-synthetic","message":"评测事件"})
                raised.raise_for_status();alarm_ids.append(raised.json()["id"])
            request["alarm_ids"]=alarm_ids
        response=client.post("/api/sessions",json=request)
        response.raise_for_status();state=response.json();session_id=state["id"]
        checks+=check_expected(state,case.expected)
        for i,step in enumerate(case.steps,1):
            old_revision=state["revision"]
            payload=expand(step.payload,client,state)
            if step.action=="restart":
                client.__exit__(None,None,None)
                client=None
                client=open_fixture(settings,dataset,case.model_fixture,initialize=False)
                response=client.get(f"/api/sessions/{session_id}")
            elif step.action=="export":response=client.get(f"/api/sessions/{session_id}/export")
            elif step.action=="clear_alarms":
                if not state["alarm_events"]:raise ValueError("没有可消除的案例告警")
                for alarm in state["alarm_events"]:
                    response=client.post("/api/alarms",json={"event_id":alarm["event_id"]+"-clear","device_id":alarm["device_id"],
                        "code":alarm["code"],"status":"cleared","occurred_at":now(),"source":alarm["source"],"message":"评测消除事件"})
                    response.raise_for_status()
            elif step.action in {"document_status","standard_status"}:
                kind="documents" if step.action=="document_status" else "standards"
                target=lookup(client,kind,step.target_model)
                response=client.post(f"/api/{kind}/{target}/status",json=payload)
            else:
                payload.setdefault("expected_revision",old_revision)
                response=client.post(f"/api/sessions/{session_id}/{step.action}",json=payload)
            checks.append({"step":i,"action":step.action,"path":"http_status","expected":step.expected_http,
                           "actual":response.status_code,"passed":response.status_code==step.expected_http})
            actual=response.json()
            checks.extend({**c,"step":i,"action":step.action} for c in check_expected(actual,step.expected))
            current=client.get(f"/api/sessions/{session_id}")
            current.raise_for_status();state=current.json()
            if response.status_code>=400:
                checks.append({"step":i,"path":"rejected_revision_unchanged","expected":old_revision,
                               "actual":state["revision"],"passed":state["revision"]==old_revision})
        return {"id":case.id,"category":"workflow","model_mode":mode,"passed":all(c["passed"] for c in checks),
                "checks":checks,"final_status":state["status"],"model_validation":state.get("model_validation",{})}
    finally:
        if client is not None:client.__exit__(None,None,None)


def evaluate_dataset(dataset, k=5, live_models=False):
    if not 1<=k<=20:raise ValueError("k 须为 1–20")
    started=now();tick=time.perf_counter();results=[]
    with TemporaryDirectory(prefix="suzhang-evaluation-") as temporary:
        root=Path(temporary)
        if dataset.retrieval:
            client=open_fixture(fixture_settings(root/"retrieval.db",dataset,live_models),dataset)
            try:
                for case in dataset.retrieval:
                    t=time.perf_counter()
                    response=client.post("/api/search",json={"query":case.query,"model":case.model,"version":case.version,"limit":k})
                    response.raise_for_status();data=response.json()
                    titles=list(dict.fromkeys(c["title"] for c in data["items"]))
                    relevant=set(case.relevant_titles)
                    ranks=[i for i,title in enumerate(titles,1) if title in relevant]
                    hit=bool(ranks)
                    passed=not titles if case.expect_empty else hit
                    if case.expected_route is not None:passed=passed and data["route"]==case.expected_route
                    results.append({"id":case.id,"category":"retrieval","passed":passed,"expect_empty":case.expect_empty,
                         "hit":hit,"recall":len(relevant.intersection(titles))/len(relevant) if relevant else None,
                         "rr":1/min(ranks) if ranks else 0,"retrieved_titles":titles,"relevant_titles":case.relevant_titles,
                         "route":data["route"],"expected_route":case.expected_route,"warnings":data["warnings"],
                         "elapsed_ms":round((time.perf_counter()-t)*1000,2)})
            finally:client.__exit__(None,None,None)
        for i,case in enumerate(dataset.workflows):
            t=time.perf_counter();directory=root/f"workflow-{i}";directory.mkdir()
            try:result=run_workflow(case,dataset,directory,live_models)
            except Exception as exc:
                result={"id":case.id,"category":"workflow","passed":False,"error_type":type(exc).__name__}
            results.append({**result,"elapsed_ms":round((time.perf_counter()-t)*1000,2)})
    positive=[r for r in results if r["category"]=="retrieval" and not r["expect_empty"]]
    negative=[r for r in results if r["category"]=="retrieval" and r["expect_empty"]]
    passed=sum(r["passed"] for r in results)
    return {"schema_version":1,"application_version":"0.6.0","started_at":started,"finished_at":now(),
            "elapsed_ms":round((time.perf_counter()-tick)*1000,2),
            "dataset":{"name":dataset.name,"revision":dataset.revision,"kind":dataset.kind,"provenance":dataset.provenance,
                       "sha256":hashlib.sha256(json.dumps(dataset.model_dump(),sort_keys=True,ensure_ascii=False).encode()).hexdigest()},
            "models":"explicit_live" if live_models else "rules_and_labelled_fixtures",
            "note":"匹配预期的回归案例比例不是故障诊断准确率；引用定位不证明语义支持。模拟数据不代表真实工厂效果。",
            "summary":{"total":len(results),"passed":passed,"failed":len(results)-passed,"pass_rate":passed/len(results)},
            "retrieval_metrics":{"label_level":"document_title","cutoff":f"top {k} chunks deduplicated into documents",
                "positive_count":len(positive),"negative_count":len(negative),
                f"Hit@{k}":sum(r["hit"] for r in positive)/len(positive) if positive else None,
                f"Recall@{k}":sum(r["recall"] for r in positive)/len(positive) if positive else None,
                f"MRR@{k}":sum(r["rr"] for r in positive)/len(positive) if positive else None,
                "negative_pass_rate":sum(r["passed"] for r in negative)/len(negative) if negative else None},
            "results":results}


def markdown_report(report):
    def safe(value):return str(value).replace("|","\\|").replace("\n"," ").replace("\r"," ").replace("<","&lt;").replace(">","&gt;")
    summary=report["summary"];dataset=report["dataset"]
    lines=["# 端到端评测报告", "", f"数据集：{safe(dataset['name'])} / {safe(dataset['revision'])} / {dataset['kind']}",
           f"来源：{safe(dataset['provenance'])}",f"数据集 SHA256：`{dataset['sha256']}`", "",report["note"], "",
           f"案例 {summary['total']}；通过 {summary['passed']}；失败 {summary['failed']}；预期匹配率 {summary['pass_rate']:.1%}。", "",
           f"模型方式：{report['models']}。执行时间 {report['elapsed_ms']:.0f} ms，仅记录本轮耗时，不是性能基准。", "",
           "| 案例 | 分类 | 结果 | 模型/结束状态 |", "|---|---|---|---|"]
    for r in report["results"]:
        detail=r.get("model_mode",r.get("route",""))+" / "+r.get("final_status",r.get("error_type",""))
        lines.append(f"| {safe(r['id'])} | {r['category']} | {'通过' if r['passed'] else '失败'} | {safe(detail)} |")
    lines+= ["", "## 检索指标", "",f"口径：{report['retrieval_metrics']['cutoff']}；标题级标签，仅正样本计算 Hit/Recall/MRR，负样本单独计分。", ""]
    for key,value in report["retrieval_metrics"].items():lines.append(f"- {key}: {safe(value)}")
    lines += ["", "## 失败详情", ""]
    failed=[r for r in report["results"] if not r["passed"]]
    for r in failed:
        lines.append(f"- {safe(r['id'])}: {safe(json.dumps([c for c in r.get('checks',[]) if not c['passed']],ensure_ascii=False) if 'checks' in r else r.get('error_type',r.get('retrieved_titles',[])))}")
    if not failed:lines.append("本轮没有预期不匹配的案例。")
    return "\n".join(lines)+"\n"
