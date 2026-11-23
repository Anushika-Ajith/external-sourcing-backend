"""Shared PO number extraction from OCR structured payload and raw text."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


def _pick_str(*values: Any) -> Optional[str]:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _header_po_number(block: Any) -> Optional[str]:
    if not isinstance(block, dict):
        return None
    header = block.get("header")
    if not isinstance(header, dict):
        return None
    return _pick_str(header.get("po_number"))


def _first_line_po_number(block: Any) -> Optional[str]:
    if not isinstance(block, dict):
        return None
    lines = block.get("lines")
    if not isinstance(lines, list):
        return None
    for line in lines:
        if isinstance(line, dict):
            val = _pick_str(line.get("po_number"))
            if val:
                return val
    return None


_PO_NUMBER_PATTERNS = (
    r"(?:PO|P\.O\.|Purchase\s+Order)\s*(?:No\.?|Number|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,40})",
    r"\b(PO[-\s/]?\d{3,}[-\s/]?\d*)\b",
    r"(?:Order\s+No\.?|Order\s+Number|Order\s*#)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,40})",
    r"(?:Ref(?:erence)?\s*No\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/]{2,40})",
)


def _regex_po_from_text(text: str) -> Optional[str]:
    blob = (text or "").strip()
    if not blob:
        return None
    for pattern in _PO_NUMBER_PATTERNS:
        m = re.search(pattern, blob, re.I)
        if m:
            return m.group(1).strip()
    return None


def extract_po_number_from_ocr_raw(
    raw: Dict[str, Any],
    *,
    ocr_text: Optional[str] = None,
    document_classification: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Best-effort PO number from Gemini OCR payload and optional raw document text."""
    candidates: List[Optional[str]] = [
        _pick_str(raw.get("po_number"), raw.get("purchase_order_number")),
    ]

    vendor = raw.get("vendor")
    if isinstance(vendor, dict):
        for key in (
            "po_number",
            "purchase_order_number",
            "order_number",
            "document_number",
        ):
            candidates.append(_pick_str(vendor.get(key)))

    for block_key in ("grn", "invoice", "dispatch_note"):
        block = raw.get(block_key)
        candidates.append(_header_po_number(block))
        candidates.append(_first_line_po_number(block))

    docs = raw.get("documents")
    if isinstance(docs, list):
        classified_po = False
        if isinstance(document_classification, dict):
            dtype = str(document_classification.get("document_type") or "").lower()
            classified_po = "purchase" in dtype and "order" in dtype
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            dtype = str(doc.get("document_type") or "").lower()
            num = _pick_str(doc.get("document_number"))
            if not num:
                continue
            if classified_po or "purchase" in dtype or dtype == "po":
                candidates.append(num)
            elif re.match(r"^PO[-\s/]", num, re.I):
                candidates.append(num)

    items = raw.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                candidates.append(_pick_str(item.get("po_number")))

    for c in candidates:
        if c:
            return c

    for text_blob in (ocr_text, str(raw)):
        found = _regex_po_from_text(text_blob or "")
        if found:
            return found

    return None
