import hashlib
import json
import re
from uuid import uuid4

from .document_parser import parse_pdf
from .schemas import DocumentInput
from .store import Store, now


def parse_file(filename: str, content: bytes, gateway=None, mode="auto", progress=None) -> list[str]:
    suffix = filename.rsplit(".", 1)[-1].lower()
    if suffix in {"txt", "md"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文本文件须使用 UTF-8 编码") from exc
        if not text.strip():
            raise ValueError("文档为空")
        return [text]
    if suffix != "pdf":
        raise ValueError("支持 .md、.txt 和 .pdf")
    return parse_pdf(content,gateway,mode,progress)


def split_page(text: str, max_chars: int = 1000):
    """Keep paragraph/table groups and heading context; retain original char ranges."""
    blocks = list(re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", text))
    current, start, end, heading = [], 0, 0, ""
    for match in blocks:
        block = match.group().strip()
        is_heading = bool(re.match(r"^#{1,6}\s|^第[一二三四五六七八九十\d]+[章节]", block))
        if current and (is_heading or sum(len(x) for x in current) + len(block) > max_chars):
            yield {"text": "\n\n".join(current), "start": start, "end": end, "section": heading}
            current = []
        if is_heading:
            heading = block.splitlines()[0].lstrip("# ")
        if not current:
            start = match.start()
        current.append(block)
        end = match.end()
    if current:
        yield {"text": "\n\n".join(current), "start": start, "end": end, "section": heading}


def ingest(store: Store, meta: DocumentInput, pages: list[str] | None = None, filename: str | None = None, source_bytes=None):
    pages = pages or [meta.text]
    if not any(p.strip() for p in pages):
        raise ValueError("文档为空")
    identity = {**meta.model_dump(exclude={"text"}), "pages": pages}
    parsing=getattr(pages,"metadata",None)
    if parsing:
        # Reuse the first immutable parse for the same source and engine version.
        identity={**meta.model_dump(exclude={"text"}),"parser":{k:parsing[k] for k in ("version","mode","source_sha256")},
                  "engines":[(p["engine"],p["model_version"]) for p in parsing["pages"]]}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    doc_id = uuid4().hex
    chunks = []
    for page_no, page in enumerate(pages, 1):
        parts=split_page(page)
        if parsing:
            # Keep each table whole; canonical source ranges remain exact.
            parts=({"text":page[b["start"]:b["end"]],"start":b["start"],"end":b["end"],"section":"表格" if b["kind"]=="table" else "PDF 原文",
                    "provenance":{"block_id":b["id"],"kind":b["kind"],"bbox":b["bbox"],"coordinates":b["coordinates"],
                                  "method":parsing["pages"][page_no-1]["method"],"confidence":b.get("confidence")}}
                   for b in parsing["pages"][page_no-1]["blocks"] if "start" in b)
        for part in parts:
            chunks.append({**part, "id": f"{doc_id}:{len(chunks) + 1}", "document_id": doc_id,
                           "title": meta.title, "model": meta.model, "version": meta.version,
                           "page": page_no, "kind": meta.kind,
                           "preliminary_cause": meta.preliminary_cause, "confirmed_cause": meta.confirmed_cause})
    document = {**meta.model_dump(exclude={"text"}), "id": doc_id, "fingerprint": fingerprint,
                "filename": filename, "pages": list(pages), "chunk_count": len(chunks), "created_at": now()}
    if parsing:
        document["parsing"]=parsing
        document["parsing_summary"]={"version":parsing["version"],"mode":parsing["mode"],"ocr_pages":parsing["ocr_pages"],
                                      "tables":parsing["tables"],"pages":len(pages),
                                      "requires_visual_review":bool(parsing["ocr_pages"] or parsing["tables"])}
    document["has_source_pdf"]=source_bytes is not None
    return store.put_document(document, chunks, source_bytes)
