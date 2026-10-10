"""PDF layout parsing with page-local provenance and optional OCR."""
import hashlib
import json
import math

import pymupdf

from .ocr import table_text


PARSER_VERSION = "pdf-layout-v1"


class ParsedPages(list):
    def __init__(self, pages, metadata=None):
        super().__init__(pages)
        self.metadata = metadata


def render_page(page, dpi=150):
    width,height=page.rect.width,page.rect.height
    if not all(math.isfinite(v) and v>0 for v in (width,height)):
        raise ValueError("PDF 页面尺寸异常")
    scale=min(dpi/72,4095/width,4095/height,math.sqrt(11000000/(width*height)))
    pix=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),colorspace=pymupdf.csRGB,alpha=False)
    raw=pix.tobytes("png")
    if len(raw)>10*1024*1024:
        raise ValueError("页面图像超过 10 MB，请降低扫描分辨率")
    return raw,pix.width,pix.height


def assemble(blocks):
    parts, offset = [], 0
    for block in blocks:
        text=block["text"].strip()
        if not text:
            continue
        if parts:
            parts.append("\n\n"); offset+=2
        block["start"],block["end"]=offset,offset+len(text)
        block["text"]=text
        parts.append(text);offset+=len(text)
    return "".join(parts)


def native_blocks(page):
    tables = list(page.find_tables().tables)
    blocks, exclusions = [], []
    for index, table in enumerate(tables):
        rows=table.extract()
        text=table_text(rows)
        bbox=list(table.bbox)
        header=table.header
        headers=[str(v) if v is not None else None for v in header.names]
        if header.external:
            text="解析器识别的外部表头（须核对）："+json.dumps(headers,ensure_ascii=False)+"\n"+text
            bbox=list(pymupdf.Rect(bbox)|pymupdf.Rect(header.bbox))
        exclusions.append(pymupdf.Rect(bbox))
        blocks.append({"id":f"table-{index+1}","kind":"table","text":text,"bbox":bbox,
                       "rows":rows,"header_names":headers,"header_external":header.external,
                       "cell_boxes":[list(c) if c is not None else None for c in table.cells],
                       "confidence":None,"coordinates":"pdf_points"})
    for index, block in enumerate(page.get_text("dict",sort=True)["blocks"]):
        if block["type"] != 0:
            continue
        lines, boxes = [], []
        for line in block["lines"]:
            box=pymupdf.Rect(line["bbox"])
            center=pymupdf.Point((box.x0+box.x1)/2,(box.y0+box.y1)/2)
            if any(center in table for table in exclusions):
                continue
            lines.append("".join(s["text"] for s in line["spans"]));boxes.append(box)
        if lines:
            bbox=boxes[0]
            for box in boxes[1:]:
                bbox|=box
            blocks.append({"id":f"text-{index+1}","kind":"text","text":"\n".join(lines),
                           "bbox":list(bbox),"confidence":None,"coordinates":"pdf_points"})
    return sorted(blocks,key=lambda b:(b["bbox"][1],b["bbox"][0]))


def image_coverage(page):
    area=page.rect.get_area()
    # Largest image is enough to detect scan pages with a small selectable footer.
    boxes=[pymupdf.Rect(i["bbox"]) & page.rect for i in page.get_image_info()]
    return max((b.get_area()/area for b in boxes),default=0)


def parse_pdf(content, gateway=None, mode="auto", progress=None):
    if mode not in {"auto","native","ocr"}:
        raise ValueError("PDF 解析模式须为 auto、native 或 ocr")
    try:
        with pymupdf.open(stream=content,filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise ValueError("请先解密 PDF")
            if not 1<=len(pdf)<=500:
                raise ValueError("PDF 须包含 1–500 页")
            pages,metadata,total_chars=[],[],0
            for index,page in enumerate(pdf):
                native=page.get_text(sort=True)
                coverage=image_coverage(page)
                needs_ocr=mode=="ocr" or (mode=="auto" and (not native.strip() or (coverage>=0.6 and len(native.strip())<80)))
                warnings=[]
                stage="ocr" if needs_ocr else "pdf_layout"
                if progress:
                    progress(index,len(pdf),stage)
                if needs_ocr:
                    if gateway is None or not gateway.settings.ocr_url:
                        raise ValueError(f"第 {index+1} 页需要 OCR；请配置识别服务或先完成 OCR 再导入")
                    raw,width,height=render_page(page,gateway.settings.ocr_dpi)
                    result=gateway.recognize(raw,width,height,index+1)
                    blocks=[]
                    for number,b in enumerate(result.blocks,1):
                        item=b.model_dump(exclude_none=True)
                        item.update(id=f"ocr-{number}",coordinates="rendered_pixels")
                        if b.kind=="table":
                            item["text"]=table_text(b.rows)
                        blocks.append(item)
                    warnings=list(result.warnings)+["OCR 提取可能存在错字或数值误识别，审批须核对原始页面图像"]
                    engine,version=result.engine,result.model_version
                else:
                    if not native.strip():
                        raise ValueError(f"第 {index+1} 页没有文本层；请选择 OCR 模式或先完成 OCR 再导入")
                    blocks=native_blocks(page)
                    width,height=page.rect.width,page.rect.height
                    engine,version="pymupdf",pymupdf.VersionBind
                    if coverage>0.1:
                        warnings.append("本页包含图像，文字层提取不能覆盖图中文字；可选择强制 OCR")
                text=assemble(blocks)
                if not text.strip():
                    raise ValueError(f"第 {index+1} 页未提取到有效文字，未入库")
                total_chars+=len(text)
                if total_chars>500000:
                    raise ValueError("解析后文档超过 500000 字符，请拆分资料")
                confidence=[b["confidence"] for b in blocks if b.get("confidence") is not None]
                pages.append(text)
                metadata.append({"page":index+1,"method":"ocr" if needs_ocr else "native",
                                 "engine":engine,"model_version":version,"width":width,"height":height,
                                 "confidence_min":min(confidence) if confidence else None,
                                 "warnings":warnings,"blocks":blocks})
                if progress:
                    progress(index+1,len(pdf),"parsed")
    except (pymupdf.FileDataError,RuntimeError) as exc:
        raise ValueError("PDF 损坏或无法解析") from exc
    return ParsedPages(pages,{"version":PARSER_VERSION,"mode":mode,"source_sha256":hashlib.sha256(content).hexdigest(),
                              "pages":metadata,"ocr_pages":sum(p["method"]=="ocr" for p in metadata),
                              "tables":sum(b["kind"]=="table" for p in metadata for b in p["blocks"])})
