"""Optional PP-StructureV3 worker. Launch separately after the owner deploys models."""
import base64
import json
import math
import secrets
import struct
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import Settings
from .ocr import OCRResponse, parse_table_html


class OCRRequest(BaseModel):
    image_base64: str = Field(min_length=1,max_length=14*1024*1024)
    width: int = Field(ge=1,le=4096,strict=True)
    height: int = Field(ge=1,le=4096,strict=True)
    page: int = Field(ge=1,le=500,strict=True)


def block_confidence(result, bbox):
    ocr=result.get("overall_ocr_res",{})
    boxes=ocr.get("rec_boxes",[])
    scores=ocr.get("rec_scores",[])
    if len(boxes)!=len(scores):
        return None
    selected=[]
    for box,score in zip(boxes,scores):
        if len(box)!=4:
            continue
        x,y=(box[0]+box[2])/2,(box[1]+box[3])/2
        if bbox[0]<=x<=bbox[2] and bbox[1]<=y<=bbox[3] and 0<=score<=1:
            selected.append(float(score))
    return min(selected) if selected else None


def convert_paddle_result(data,width,height,model_version):
    if isinstance(data,str):
        data=json.loads(data)
    result=data.get("res",data)
    blocks,warnings=[],[]
    for item in result["parsing_res_list"]:
        label=item["block_label"]
        if label in {"image","chart","seal"}:
            warnings.append(f"{label} 区域未转为文字，请核对页面图像")
            continue
        text=item["block_content"]
        if not isinstance(text,str) or not text.strip():
            continue
        original=[float(v) for v in item["block_bbox"]]
        if len(original) != 4 or not all(math.isfinite(v) for v in original):
            raise ValueError("invalid recognition coordinates")
        bbox=[max(0,min(width,original[0])),max(0,min(height,original[1])),
              max(0,min(width,original[2])),max(0,min(height,original[3]))]
        if bbox!=original:
            warnings.append("识别边界框已裁剪至原图范围")
        if label=="table":
            rows,spans=parse_table_html(text)
            block={"kind":"table","rows":rows,"spans":spans,"text":"","bbox":bbox}
        else:
            block={"kind":"heading" if label in {"doc_title","paragraph_title","table_title"} else "text","text":text,"bbox":bbox}
        block["confidence"]=block_confidence(result,bbox)
        blocks.append(block)
    return OCRResponse(engine="PP-StructureV3",model_version=model_version,width=width,height=height,
                       blocks=blocks,warnings=list(dict.fromkeys(warnings))[:30])


def create_ocr_app(settings=None,engine_factory=None):
    settings=settings or Settings()
    if not settings.ocr_api_key or not settings.ocr_model_version.strip():
        raise ValueError("识别服务须配置 SUZHANG_OCR_API_KEY 与 SUZHANG_OCR_MODEL_VERSION")
    lock=threading.Lock()

    def default_engine():
        # Heavy dependencies and automatic model initialization are opt-in at worker startup.
        from paddleocr import PPStructureV3
        options={"device":settings.ocr_device,"use_doc_orientation_classify":False,
                 "use_doc_unwarping":False,"use_textline_orientation":False,"use_formula_recognition":False}
        if settings.ocr_paddlex_config:
            options["paddlex_config"]=settings.ocr_paddlex_config
        return PPStructureV3(**options)

    @asynccontextmanager
    async def lifespan(app):
        app.state.engine=(engine_factory or default_engine)()
        yield
        close=getattr(app.state.engine,"close",None)
        if close:
            close()

    app=FastAPI(title="溯障 · OCR 识别服务",version="0.6.0",lifespan=lifespan)

    def authorize(authorization: str | None = Header(default=None)):
        if not secrets.compare_digest(authorization or "","Bearer "+settings.ocr_api_key):
            raise HTTPException(401,"识别服务令牌无效")

    @app.get("/health")
    def health():
        return {"status":"ok","engine":"PP-StructureV3","model_version":settings.ocr_model_version}

    @app.post("/v1/ocr",dependencies=[Depends(authorize)])
    def recognize(request: OCRRequest):
        try:
            raw=base64.b64decode(request.image_base64,validate=True)
            if len(raw)>10*1024*1024 or raw[:8]!=b"\x89PNG\r\n\x1a\n" or len(raw)<24:
                raise ValueError("invalid PNG")
            dimensions=struct.unpack(">II",raw[16:24])
            if dimensions!=(request.width,request.height) or request.width*request.height>12000000:
                raise ValueError("invalid dimensions")
        except (ValueError,struct.error) as exc:
            raise HTTPException(400,"须提供尺寸一致且受限的 PNG 图像") from exc
        try:
            with lock, tempfile.TemporaryDirectory(prefix="suzhang-ocr-") as directory:
                image=Path(directory)/"page.png"
                image.write_bytes(raw)
                outputs=list(app.state.engine.predict(str(image)))
                if len(outputs)!=1:
                    raise ValueError("expected one page")
                data=outputs[0].json
                result=convert_paddle_result(data,request.width,request.height,settings.ocr_model_version)
        except Exception as exc:
            raise HTTPException(502,"识别或版面转换失败，请检查模型与识别服务日志") from exc
        return result.model_dump()

    return app
