import json
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from app.evaluation import FixtureGateway
from app.grounding import GroundingError,validate_explanation
from app.ingest import split_page
from app.main import create_app
from app.models import ModelGateway,ModelUnavailable
from test_core import client,settings,http_gateway,start


def contract():
    text="前缀🙂。供气压力范围为 0.60–0.80 MPa，须核对型号。"
    quote="供气压力范围为 0.60–0.80 MPa，须核对型号。"
    offset=text.index(quote)
    raw={"claims":[{"text":"资料范围需要人工核验。","citations":[{"evidence_id":"e1","quote":quote,"start":offset,"end":len(text)}]}]}
    source={"id":"e1","document_id":"d1","text":text,"title":"压力手册","model":"P-200","version":"v2","page":2,
            "start":17,"end":17+len(text),"provenance":{"kind":"table","method":"native","bbox":[0,0,10,10]}}
    doc={"id":"d1","title":"压力手册","model":"P-200","version":"v2","active":True,"pages":["第一页","x"*17+text]}
    return raw,source,doc


def test_exact_quote_full_page_unicode_and_server_metadata():
    raw,source,doc=contract()
    result=validate_explanation(raw,[source],lambda id:doc)
    cite=result["claims"][0]["citations"][0]
    assert cite["source_start"]==17+4 and cite["page"]==2
    assert doc["pages"][1][cite["source_start"]:cite["source_end"]]==cite["quote"]
    assert cite["requires_visual_review"] and result["validation"]["page_checked"]
    assert result["validation"]["semantic_support"]=="human_review_required"
    source["provenance"]["bbox"][0]=999
    assert cite["provenance"]["bbox"][0]==0
    assert result["summary"]==raw["claims"][0]["text"] and result["evidence_ids"]==["e1"]


@pytest.mark.parametrize("change,code",[
    ("id","unknown_evidence"),("quote","quote_mismatch"),("position","quote_mismatch"),
    ("span","invalid_structure"),("boolean","invalid_structure"),("empty","invalid_structure"),
    ("legacy","invalid_structure"),("extra","invalid_structure"),("duplicate","duplicate_citation"),
    ("page","page_quote_mismatch"),("version","source_unavailable"),("inactive","source_unavailable"),
    ("outside","invalid_source_span"),("unsupported_summary","invalid_structure")])
def test_invalid_grounding_rejected(change,code):
    raw,source,doc=contract();cite=raw["claims"][0]["citations"][0]
    if change=="id":cite["evidence_id"]="invented"
    elif change=="quote":cite["quote"]=cite["quote"].replace("0.60","0.90")
    elif change=="position":cite["start"]+=1;cite["end"]+=1
    elif change=="span":cite["end"]-=1
    elif change=="boolean":cite["start"]=False
    elif change=="empty":raw["claims"][0]["citations"]=[]
    elif change=="legacy":raw={"summary":"旧摘要","evidence_ids":["e1"]}
    elif change=="extra":cite["document_id"]="spoofed"
    elif change=="duplicate":raw["claims"][0]["citations"].append(deepcopy(cite))
    elif change=="page":doc["pages"][1]=doc["pages"][1].replace("0.60","0.90")
    elif change=="version":doc["version"]="v3"
    elif change=="inactive":doc["active"]=False
    elif change=="outside":source["end"]=17
    elif change=="unsupported_summary":raw["summary"]="没有引用的额外结论"
    with pytest.raises(GroundingError) as exc:validate_explanation(raw,[source],lambda id:doc)
    assert exc.value.code==code


def test_mixed_valid_invalid_claims_never_return_partial_output():
    raw,source,doc=contract()
    second=deepcopy(raw["claims"][0]);second["citations"][0]["evidence_id"]="invented"
    raw["claims"].append(second)
    with pytest.raises(GroundingError):validate_explanation(raw,[source],lambda id:doc)


def test_text_chunks_preserve_blank_lines_and_unicode():
    page="# 示例\n\n第一段🙂：供气压力需核验。  \n\n\n第二段：型号与版本需核验。\n"
    chunks=list(split_page(page))
    assert len(chunks)==1
    assert chunks[0]["text"]==page[chunks[0]["start"]:chunks[0]["end"]]
    raw={"claims":[{"text":"条款需核验。","citations":[{"evidence_id":"e","quote":"第二段：型号与版本需核验。","start":page.index("第二段"),"end":len(page)-1}]}]}
    source={**chunks[0],"id":"e","document_id":"d","title":"示例","model":"AIR-SYSTEM","version":"1","page":1}
    doc={"title":"示例","model":"AIR-SYSTEM","version":"1","active":True,"pages":[page]}
    assert validate_explanation(raw,[source],lambda id:doc)["validation"]["page_checked"]


def test_gateway_failure_does_not_leak_model_body(settings,monkeypatch):
    configured=settings.model_copy(update={"llm_base_url":"http://fixture/v1"})
    gateway=http_gateway(configured,monkeypatch,lambda r:httpx.Response(200,json={"choices":[{"message":{"content":"private-test-body"}}]}))
    with pytest.raises(ModelUnavailable) as exc:gateway.explain({"evidence":[]})
    assert exc.value.code=="invalid_structure" and "private-test-body" not in str(exc.value)


@pytest.mark.parametrize("fixture,status,mode",[("valid","anchored","model_assisted"),("changed_quote","rejected","rule_demo"),("timeout","unavailable","rule_demo")])
def test_workflow_grounding_persists_and_does_not_change_rule_branch(settings,fixture,status,mode):
    configured=settings.model_copy(update={"llm_base_url":"http://fixture/v1"})
    app=create_app(configured);app.state.workflow.gateway=FixtureGateway(configured,fixture)
    with TestClient(app) as client:
        state=start(client)
        assert state["status"]=="checking" and state["current_check"]["id"]=="source_pressure"
        assert state["mode"]==mode and state["model_validation"]["status"]==status
        if fixture=="valid":assert state["model_explanation"]["validation"]["page_checked"]
        else:assert state["model_explanation"] is None
    with TestClient(create_app(settings)) as restarted:
        saved=restarted.get(f'/api/sessions/{state["id"]}/export').json()["session"]
        assert saved["model_validation"]==state["model_validation"]
        assert saved["model_explanation"]==state["model_explanation"]


def test_valid_quote_is_not_a_semantic_approval():
    raw,source,doc=contract()
    raw["claims"][0]["text"]="故意错误的语义：这条引文证明设备已经维修完毕。"
    result=validate_explanation(raw,[source],lambda id:doc)
    assert result["validation"]["semantic_support"]=="human_review_required"
