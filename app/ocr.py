"""Bounded OCR HTTP contract. No model installation or weights in the core app."""
import base64
import json
import math
from html.parser import HTMLParser
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator


class OCRBlock(BaseModel):
    kind: Literal["text", "heading", "table"]
    text: str = Field(default="", max_length=100000)
    bbox: list[float] = Field(min_length=4, max_length=4)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    rows: list[list[str | None]] | None = None
    spans: list[dict[str, int]] = Field(default_factory=list, max_length=20000)

    @model_validator(mode="before")
    @classmethod
    def numeric_types(cls, data):
        if isinstance(data, dict):
            bbox = data.get("bbox")
            if not isinstance(bbox, (list, tuple)):
                raise ValueError("bbox must be coordinates")
            values = list(bbox)
            if data.get("confidence") is not None:
                values.append(data["confidence"])
            if any(type(v) not in (int, float) for v in values):
                raise ValueError("coordinates and confidence must be numbers")
            for span in data.get("spans") or []:
                if not isinstance(span, dict) or any(type(v) is not int for v in span.values()):
                    raise ValueError("span values must be integers")
        return data

    @model_validator(mode="after")
    def bounded(self):
        if not all(math.isfinite(v) and v >= 0 for v in self.bbox) or self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]:
            raise ValueError("invalid bbox")
        if self.kind == "table":
            validate_rows(self.rows)
            for span in self.spans:
                if set(span) != {"row", "column", "rowspan", "colspan"}:
                    raise ValueError("invalid span")
                r,c,rs,cs = (span[k] for k in ("row","column","rowspan","colspan"))
                if r < 0 or c < 0 or rs < 1 or cs < 1 or r+rs > len(self.rows) or c+cs > len(self.rows[0]):
                    raise ValueError("invalid span bounds")
        elif self.rows is not None or self.spans or not self.text.strip():
            raise ValueError("invalid text block")
        return self


class OCRResponse(BaseModel):
    engine: str = Field(min_length=1, max_length=100)
    model_version: str = Field(default="unspecified", min_length=1, max_length=100)
    width: int = Field(ge=1, le=4096, strict=True)
    height: int = Field(ge=1, le=4096, strict=True)
    blocks: list[OCRBlock] = Field(min_length=1, max_length=2000)
    warnings: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def in_image(self):
        total = 0
        for block in self.blocks:
            if block.bbox[2] > self.width or block.bbox[3] > self.height:
                raise ValueError("bbox outside image")
            total += len(block.text) + sum(len(cell or "") for row in block.rows or [] for cell in row)
        if total > 500000 or any(len(w) > 500 for w in self.warnings):
            raise ValueError("response too large")
        return self


def validate_rows(rows):
    if not rows or len(rows) > 500 or not rows[0] or len(rows[0]) > 40:
        raise ValueError("table dimensions unsupported")
    if any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("table must be rectangular")
    if any(cell is not None and (not isinstance(cell,str) or len(cell)>10000) for row in rows for cell in row):
        raise ValueError("table cell invalid")
    if sum(len(cell or "") for row in rows for cell in row) > 100000:
        raise ValueError("table too large")


def table_text(rows):
    # Do not infer a header, propagate merged values or discard empty cells.
    validate_rows(rows)
    return "表格（逐行保留；null 表示空或合并位置，不能自行继承数值）\n" + "\n".join(json.dumps(row,ensure_ascii=False) for row in rows)


class TableHTML(HTMLParser):
    """Convert one engine-generated HTML table to bounded cells without rendering HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.current, self.cell, self.parts = [], None, None, []
        self.depth, self.hidden = 0, 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "table":
            self.depth += 1
            if self.depth != 1 or self.rows:
                raise ValueError("nested/multiple tables unsupported")
        elif tag == "tr":
            if self.current is not None:
                raise ValueError("invalid table rows")
            self.current = []
        elif tag in {"td","th"}:
            if self.current is None or self.cell is not None:
                raise ValueError("invalid table cell")
            attrs = dict(attrs)
            rs,cs = int(attrs.get("rowspan",1)),int(attrs.get("colspan",1))
            if not 1 <= rs <= 500 or not 1 <= cs <= 40:
                raise ValueError("span exceeds limit")
            self.cell, self.parts = (rs,cs), []
        elif tag == "br" and self.cell:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.cell and not self.hidden:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag in {"script","style"}:
            self.hidden = max(0,self.hidden-1)
        if tag in {"td","th"}:
            if self.cell is None:
                raise ValueError("unmatched cell")
            self.current.append(("".join(self.parts).strip(),*self.cell))
            self.cell = None
        elif tag == "tr":
            if self.current is None or self.cell is not None:
                raise ValueError("unclosed cell")
            self.rows.append(self.current)
            self.current = None
            if len(self.rows)>500:
                raise ValueError("too many rows")
        elif tag == "table":
            self.depth -= 1

    def result(self):
        if self.depth or self.cell or self.current is not None or not self.rows:
            raise ValueError("unclosed/empty table")
        grid, occupied, spans = [], set(), []
        for r, cells in enumerate(self.rows):
            c = 0
            for text,rs,cs in cells:
                while (r,c) in occupied:
                    c += 1
                if c+cs>40 or r+rs>len(self.rows):
                    raise ValueError("span outside table")
                for rr in range(r,r+rs):
                    for cc in range(c,c+cs):
                        if (rr,cc) in occupied:
                            raise ValueError("overlapping spans")
                        occupied.add((rr,cc))
                if rs>1 or cs>1:
                    spans.append({"row":r,"column":c,"rowspan":rs,"colspan":cs})
                grid.append((r,c,text)); c+=cs
        width=max(c for _,c in occupied)+1
        rows=[[None]*width for _ in self.rows]
        for r,c,text in grid:
            rows[r][c]=text
        validate_rows(rows)
        return rows,spans


def parse_table_html(html):
    if len(html)>500000:
        raise ValueError("table HTML too large")
    parser=TableHTML()
    parser.feed(html)
    parser.close()
    return parser.result()


class OCRGateway:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    def recognize(self, png, width, height, page_no):
        if not self.settings.ocr_url:
            raise ValueError("OCR 服务尚未配置；扫描页须接入识别服务后再导入")
        headers={"Authorization":f"Bearer {self.settings.ocr_api_key}"} if self.settings.ocr_api_key else {}
        payload={"image_base64":base64.b64encode(png).decode(),"width":width,"height":height,"page":page_no}
        try:
            with httpx.Client(timeout=self.settings.ocr_timeout,trust_env=False,transport=self.transport) as client:
                with client.stream("POST",self.settings.ocr_url,json=payload,headers=headers) as response:
                    response.raise_for_status()
                    chunks,total=[],0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > 4*1024*1024:
                            raise ValueError("response too large")
                        chunks.append(chunk)
            result=OCRResponse.model_validate_json(b"".join(chunks))
            if (result.width,result.height)!=(width,height):
                raise ValueError("image dimensions mismatch")
            return result
        except (httpx.HTTPError,ValidationError,ValueError,TypeError) as exc:
            raise ValueError(f"第 {page_no} 页 OCR 不可用、超时或返回格式异常；未入库，请检查识别服务后重试") from exc
