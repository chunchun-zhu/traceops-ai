import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ingest import parse_file
from app.maintenance import backup,restore
from app.main import create_app
from app.ocr import OCRGateway,OCRResponse,parse_table_html
from app.ocr_service import create_ocr_app,convert_paddle_result
from test_core import client,settings
from test_operations import wait_job
from test_hardening import secured,login


def table_pdf():
    with pymupdf.open() as doc:
        p=doc.new_page(width=400,height=230)
        p.insert_text((40,30),"Pressure limits",fontsize=14)
        xs,ys=[40,140,260,360],[60,90,120,150]
        for x in xs:
            p.draw_line((x,ys[0]),(x,ys[-1]))
        for y in ys:
            p.draw_line((xs[0],y),(xs[-1],y))
        rows=[["Code","Pressure","Model"],["E303","0.60-0.80 MPa","P-200"],["E304","0.50-0.70 MPa","A-100"]]
        for r,row in enumerate(rows):
            for c,text in enumerate(row):
                p.insert_text((xs[c]+4,ys[r]+20),text,fontsize=10)
        return doc.tobytes()


def scanned_pdf(pages=1,mixed=False,footer=False):
    with pymupdf.open() as source:
        page=source.new_page(width=400,height=230)
        page.insert_text((40,80),"E505 Pressure 0.60-0.80 MPa",fontsize=16)
        png=page.get_pixmap().tobytes("png")
    with pymupdf.open() as pdf:
        if mixed:
            p=pdf.new_page(width=400,height=230)
            p.insert_text((40,50),"E504 selectable native text")
        for _ in range(pages):
            p=pdf.new_page(width=400,height=230)
            p.insert_image(p.rect,stream=png)
            if footer:
                p.insert_text((40,210),"Page footer")
        return pdf.tobytes()


def ocr_result(width,height,table=False):
    blocks=[{"kind":"heading","text":"E505 入口压力","bbox":[10,10,width-10,35],"confidence":0.98},
            {"kind":"text","text":"压力范围 0.60–0.80 MPa，实际标准须独立审核。","bbox":[10,40,width-10,80],"confidence":0.91}]
    if table:
        blocks.append({"kind":"table","rows":[["型号","范围"],["P-200","0.60–0.80 MPa"],[None,"不可继承上一行"]],
                       "bbox":[10,90,width-10,height-10],"confidence":0.84})
    return {"engine":"mock-only","model_version":"test-1","width":width,"height":height,"blocks":blocks}


class FakeOCR:
    def __init__(self,table=False):
        self.settings=Settings(_env_file=None,ocr_url="http://mock-only/v1/ocr")
        self.calls=[]
        self.table=table

    def recognize(self,png,width,height,page):
        assert png.startswith(b"\x89PNG")
        self.calls.append(page)
        return OCRResponse.model_validate(ocr_result(width,height,self.table))


def test_native_pdf_table_geometry_and_exact_ranges(client):
    raw=table_pdf()
    result=client.post("/api/documents/upload",files={"file":("table.pdf",raw)},data={"model":"P-200"})
    assert result.status_code==200,result.text
    info=result.json()
    assert info["parsing_summary"]["tables"]==1 and not info["parsing_summary"]["ocr_pages"]
    doc=client.get("/api/documents/"+info["id"]).json()
    table=next(b for b in doc["parsing"]["pages"][0]["blocks"] if b["kind"]=="table")
    assert table["rows"][1]==["E303","0.60-0.80 MPa","P-200"]
    assert table["coordinates"]=="pdf_points" and table["cell_boxes"]
    assert doc["pages"][0].count("E303")==1
    chunks=client.post("/api/search",json={"query":"E303","model":"P-200"}).json()["items"]
    chunk=next(c for c in chunks if "E303" in c["text"])
    assert chunk["provenance"]["kind"]=="table"
    assert doc["pages"][0][chunk["start"]:chunk["end"]]==chunk["text"]
    assert "E304" in chunk["text"] and "Code" in chunk["text"]
    image=client.get(f'/api/documents/{doc["id"]}/pages/1/image')
    assert image.status_code==200 and image.headers["content-type"]=="image/png"
    assert image.content.startswith(b"\x89PNG")
    assert client.get(f'/api/documents/{doc["id"]}/pages/2/image').status_code==404
    assert client.app.state.store.document_source(doc["id"])==raw
    duplicate=client.post("/api/documents/upload",files={"file":("table.pdf",raw)},data={"model":"P-200"}).json()
    assert duplicate["duplicate"] and duplicate["id"]==doc["id"]


def test_mixed_scan_auto_footer_detection_and_provenance():
    gateway=FakeOCR(table=True)
    pages=parse_file("mixed.pdf",scanned_pdf(mixed=True,footer=True),gateway)
    assert gateway.calls==[2] and "E504" in pages[0] and "E505" in pages[1]
    assert pages.metadata["ocr_pages"]==1 and pages.metadata["tables"]==1
    info=pages.metadata["pages"][1]
    assert info["confidence_min"]==0.84 and info["method"]=="ocr" and info["warnings"]
    for b in info["blocks"]:
        assert pages[1][b["start"]:b["end"]]==b["text"]
        assert b["coordinates"]=="rendered_pixels"
    assert next(b for b in info["blocks"] if b["kind"]=="table")["rows"][2][0] is None


def test_explicit_ocr_and_native_modes():
    gateway=FakeOCR()
    pages=parse_file("table.pdf",table_pdf(),gateway,"ocr")
    assert gateway.calls==[1] and pages.metadata["ocr_pages"]==1
    with pytest.raises(ValueError,match="OCR"):
        parse_file("scan.pdf",scanned_pdf(),gateway,"native")
    with pytest.raises(ValueError,match="OCR"):
        parse_file("mixed.pdf",scanned_pdf(mixed=True,footer=True))
    native=parse_file("footer.pdf",scanned_pdf(footer=True),gateway,"native")
    assert "Page footer" in native[0] and native.metadata["pages"][0]["warnings"]


def test_ocr_http_contract_limits_and_credentials():
    settings=Settings(_env_file=None,ocr_url="http://ocr/v1/ocr",ocr_api_key="test-secret")
    def handler(request):
        payload=json.loads(request.content)
        assert request.headers["authorization"]=="Bearer test-secret"
        assert base64.b64decode(payload["image_base64"])==b"synthetic png"
        return httpx.Response(200,json=ocr_result(payload["width"],payload["height"]))
    gateway=OCRGateway(settings,httpx.MockTransport(handler))
    assert gateway.recognize(b"synthetic png",200,200,1).engine=="mock-only"
    bad=OCRGateway(settings,httpx.MockTransport(lambda r:httpx.Response(200,json={"leaked":"test-secret"})))
    with pytest.raises(ValueError) as exc:
        bad.recognize(b"png",200,200,2)
    assert "test-secret" not in str(exc.value) and "第 2 页" in str(exc.value)
    huge=OCRGateway(settings,httpx.MockTransport(lambda r:httpx.Response(200,content=b"x"*(4*1024*1024+1))))
    with pytest.raises(ValueError,match="OCR"):
        huge.recognize(b"png",200,200,1)


@pytest.mark.parametrize("change",["outside","nan","dimensions","ragged","empty","boolean_coordinate","string_confidence","boolean_span"])
def test_invalid_ocr_response_rejected(change):
    response=ocr_result(200,200,table=True)
    if change=="outside":response["blocks"][0]["bbox"][2]=201
    elif change=="nan":response["blocks"][0]["confidence"]=float("nan")
    elif change=="dimensions":response["width"]=False
    elif change=="ragged":response["blocks"][-1]["rows"]=[["x"],["y","z"]]
    elif change=="boolean_coordinate":response["blocks"][0]["bbox"][0]=True
    elif change=="string_confidence":response["blocks"][0]["confidence"]="0.8"
    elif change=="boolean_span":response["blocks"][-1]["spans"]=[{"row":False,"column":0,"rowspan":1,"colspan":1}]
    else:response["blocks"]=[]
    with pytest.raises(ValueError):OCRResponse.model_validate(response)


def test_html_merged_table_preserves_null_and_strips_active_content():
    rows,spans=parse_table_html('<table><tr><th rowspan="2">A</th><th>B</th></tr><tr><td>C<script>do not include</script><br>D</td></tr></table>')
    assert rows==[["A","B"],[None,"C\nD"]]
    assert spans==[{"row":0,"column":0,"rowspan":2,"colspan":1}]
    with pytest.raises(ValueError):parse_table_html('<table><tr><td colspan="41">x</td></tr></table>')
    with pytest.raises(ValueError):parse_table_html('<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>')


def test_background_ocr_page_progress_cancel_and_retry(client):
    q=client.app.state.tasks
    gateway=FakeOCR()
    q.ocr_gateway=gateway
    original=gateway.recognize
    progress=[]
    checkpoint=q.checkpoint
    def capture(job,done=0,total=0,stage="running"):
        progress.append((done,total,stage))
        return checkpoint(job,done,total,stage)
    q.checkpoint=capture
    result=client.post("/api/tasks/documents",files={"file":("scan.pdf",scanned_pdf(pages=2))},data={"model":"OCR","pdf_mode":"auto"})
    job=wait_job(client,result.json()["id"])
    assert job["status"]=="succeeded" and gateway.calls==[1,2]
    assert (1,2,"ocr") in progress and (2,2,"parsed") in progress
    assert job["result"]["parsing_summary"]["ocr_pages"]==2
    assert "parsing" not in job["result"]
    q.stop()
    q.stop_event.clear()
    cancelled=q.enqueue("document",{"filename":"cancel-scan.pdf","model":"CANCEL-OCR","version":"1","content":base64.b64encode(scanned_pdf(pages=2)).decode()})
    def cancel_after_page(*args):
        parsed=original(*args)
        q.cancel(cancelled["id"])
        return parsed
    gateway.recognize=cancel_after_page
    q.execute(q.claim())
    assert q.get(cancelled["id"])["status"]=="cancelled"
    assert not any(d["model"]=="CANCEL-OCR" for d in q.store.documents())
    gateway.recognize=original
    q.retry(cancelled["id"])
    q.execute(q.claim())
    assert q.get(cancelled["id"])["status"]=="succeeded"


def test_partial_failure_never_ingests_then_retry_succeeds(client):
    q=client.app.state.tasks
    gateway=FakeOCR()
    original=gateway.recognize
    def fail_second(*args):
        if args[-1]==2:raise ValueError("第 2 页 OCR 服务失败")
        return original(*args)
    gateway.recognize=fail_second;q.ocr_gateway=gateway
    raw=scanned_pdf(pages=2)
    result=client.post("/api/tasks/documents",files={"file":("fail.pdf",raw)},data={"model":"FAIL-OCR"})
    job=wait_job(client,result.json()["id"])
    assert job["status"]=="failed" and not any(d["model"]=="FAIL-OCR" for d in q.store.documents())
    gateway.recognize=original
    client.post(f'/api/tasks/{job["id"]}/retry')
    retried=wait_job(client,job["id"])
    assert retried["status"]=="succeeded" and retried["attempts"]==2
    assert q.store.document_source(retried["result"]["id"])==raw


def test_table_standard_requires_visual_attestation(secured):
    client,app,_=secured
    login(client,"admin")
    response=client.post("/api/documents/upload",files={"file":("pressure.pdf",table_pdf())},data={"model":"P-200"})
    doc=client.get("/api/documents/"+response.json()["id"]).json()
    quote=next(b for b in doc["parsing"]["pages"][0]["blocks"] if b["kind"]=="table")["text"]
    login(client,"operator")
    draft=client.post("/api/standards",json={"title":"原生表格范围","model":"P-200","minimum":0.6,"maximum":0.8,
                     "reference_document_id":doc["id"],"page":1,"quote":quote,"approved_by":"forged"}).json()
    assert draft["source_origin"]["requires_visual_review"]
    login(client,"reviewer")
    url=f'/api/standards/{draft["id"]}/review'
    assert client.post(url,json={"decision":"approved","note":"已读文本"}).status_code==400
    result=client.post(url,json={"decision":"approved","note":"已核对原始页面图像中的 P-200 范围","visual_checked":True})
    assert result.status_code==200 and result.json()["visual_checked"]
    assert client.get(f'/api/documents/{doc["id"]}/pages/1/image').status_code==200


def test_optional_worker_adapter_without_loading_weights():
    class FakeEngine:
        def predict(self,path):
            assert Path(path).read_bytes()[:8]==b"\x89PNG\r\n\x1a\n"
            return [SimpleNamespace(json={"res":{"parsing_res_list":[
                {"block_label":"text","block_bbox":[1,1,50,30],"block_content":"E505"},
                {"block_label":"table","block_bbox":[1,40,100,90],"block_content":"<table><tr><td>型号</td><td>压力</td></tr><tr><td>P-200</td><td>0.60–0.80</td></tr></table>"}],
                "overall_ocr_res":{"rec_boxes":[[1,1,40,20]],"rec_scores":[0.95]}}})]
    settings=Settings(_env_file=None,ocr_api_key="test-worker-key",ocr_model_version="mock-not-real")
    with pymupdf.open() as pdf:
        p=pdf.new_page(width=200,height=100);pix=p.get_pixmap();raw=pix.tobytes("png")
    payload={"image_base64":base64.b64encode(raw).decode(),"width":pix.width,"height":pix.height,"page":1}
    with TestClient(create_ocr_app(settings,FakeEngine)) as client:
        assert client.post("/v1/ocr",json=payload).status_code==401
        response=client.post("/v1/ocr",json=payload,headers={"Authorization":"Bearer test-worker-key"})
        assert response.status_code==200,response.text
        result=OCRResponse.model_validate(response.json())
        assert result.blocks[0].confidence==0.95 and result.blocks[1].rows[1]==["P-200","0.60–0.80"]
        assert client.post("/v1/ocr",json={**payload,"width":201},headers={"Authorization":"Bearer test-worker-key"}).status_code==400


def test_original_pdf_survives_backup_restore(tmp_path):
    settings=Settings(_env_file=None,db_path=tmp_path/"live.db")
    raw=table_pdf()
    with TestClient(create_app(settings)) as client:
        doc=client.post("/api/documents/upload",files={"file":("table.pdf",raw)},data={"model":"BACKUP"}).json()
    backup(settings.db_path,tmp_path/"bundle")
    recovered=restore(tmp_path/"bundle",tmp_path/"restored")
    with TestClient(create_app(settings.model_copy(update={"db_path":recovered}))) as client:
        assert client.app.state.store.document_source(doc["id"])==raw
        assert client.get(f'/api/documents/{doc["id"]}/pages/1/image').status_code==200
        assert client.get("/api/documents/"+doc["id"]).json()["parsing_summary"]["tables"]==1
