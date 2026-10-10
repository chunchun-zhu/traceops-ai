import hashlib
import json
import re
from uuid import uuid4

import pymupdf

from .schemas import DocumentInput
from .store import Store, now


def parse_file(filename: str, content: bytes) -> list[str]:
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
        raise ValueError("支持 .md、.txt 和包含文本层的 .pdf")
    try:
        with pymupdf.open(stream=content, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise ValueError("请先解密 PDF")
            if len(pdf) > 500:
                raise ValueError("核心版最多解析 500 页 PDF")
            pages = [page.get_text(sort=True) for page in pdf]
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise ValueError("PDF 损坏或无法解析") from exc
    # Do not silently discard scanned pages in mixed PDFs.
    empty = [i + 1 for i, page in enumerate(pages) if not page.strip()]
    if empty:
        raise ValueError(f"第 {empty[:20]} 页没有文本层；请先完成 OCR 再导入")
    if not pages:
        raise ValueError("PDF 没有页面")
    return pages


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


def ingest(store: Store, meta: DocumentInput, pages: list[str] | None = None, filename: str | None = None):
    pages = pages or [meta.text]
    if not any(p.strip() for p in pages):
        raise ValueError("文档为空")
    identity = {**meta.model_dump(exclude={"text"}), "pages": pages}
    fingerprint = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    doc_id = uuid4().hex
    chunks = []
    for page_no, page in enumerate(pages, 1):
        for part in split_page(page):
            chunks.append({**part, "id": f"{doc_id}:{len(chunks) + 1}", "document_id": doc_id,
                           "title": meta.title, "model": meta.model, "version": meta.version,
                           "page": page_no, "kind": meta.kind,
                           "preliminary_cause": meta.preliminary_cause, "confirmed_cause": meta.confirmed_cause})
    document = {**meta.model_dump(exclude={"text"}), "id": doc_id, "fingerprint": fingerprint,
                "filename": filename, "pages": pages, "chunk_count": len(chunks), "created_at": now()}
    return store.put_document(document, chunks)
