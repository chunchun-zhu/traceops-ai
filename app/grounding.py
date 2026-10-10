"""Exact citation anchoring; this does not establish semantic entailment."""
from copy import deepcopy

from .schemas import ModelExplanation


class GroundingError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_explanation(raw, evidence, document_getter=None):
    try:
        output = ModelExplanation.model_validate(raw)
    except ValueError as exc:
        raise GroundingError("invalid_structure") from exc
    by_id = {e["id"]: e for e in evidence}
    if len(by_id) != len(evidence):
        raise GroundingError("ambiguous_evidence")
    claims, ids, documents = [], [], {}
    for number, claim in enumerate(output.claims, 1):
        citations, seen = [], set()
        for cite in claim.citations:
            if cite.evidence_id not in by_id:
                raise GroundingError("unknown_evidence")
            key = (cite.evidence_id, cite.start, cite.end)
            if key in seen:
                raise GroundingError("duplicate_citation")
            seen.add(key)
            source = by_id[cite.evidence_id]
            if cite.end > len(source["text"]) or source["text"][cite.start:cite.end] != cite.quote:
                raise GroundingError("quote_mismatch")
            page, base, end = (source[k] for k in ("page", "start", "end"))
            if any(type(v) is not int for v in (page, base, end)) or page < 1 or base < 0 or end < base+cite.end:
                raise GroundingError("invalid_source_span")
            absolute_start, absolute_end = base+cite.start, base+cite.end
            if document_getter:
                doc_id = source["document_id"]
                if doc_id not in documents:
                    documents[doc_id] = document_getter(doc_id)
                doc = documents[doc_id]
                if not doc.get("active") or any(doc[k] != source[k] for k in ("title","model","version")) or page > len(doc["pages"]):
                    raise GroundingError("source_unavailable")
                if doc["pages"][page-1][absolute_start:absolute_end] != cite.quote:
                    raise GroundingError("page_quote_mismatch")
            origin = deepcopy(source.get("provenance"))
            citations.append({**cite.model_dump(), "document_id": source["document_id"],
                              "title": source["title"], "model": source["model"], "version": source["version"],
                              "page": page, "source_start": absolute_start, "source_end": absolute_end,
                              "provenance": origin,
                              "requires_visual_review":bool(origin and (origin.get("method")=="ocr" or origin.get("kind")=="table"))})
            if cite.evidence_id not in ids:
                ids.append(cite.evidence_id)
        claims.append({"id":f"claim-{number}", "text":claim.text, "citations":citations})
    return {"claims":claims, "summary":"\n".join(c["text"] for c in claims), "evidence_ids":ids,
            "validation":{"scheme":"exact-quotes-v1", "status":"anchored", "semantic_support":"human_review_required",
                          "page_checked":document_getter is not None}}
