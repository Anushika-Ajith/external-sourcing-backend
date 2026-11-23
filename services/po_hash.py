"""Document and business hashing for PO upload duplicate / revision detection."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional


def sha256_document_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    s = re.sub(r"\s+", " ", str(value).strip().upper())
    return s


def _norm_decimal(value: Any) -> str:
    if value is None or value == "":
        return "0"
    try:
        d = Decimal(str(value))
        return format(d.normalize(), "f")
    except (InvalidOperation, ValueError):
        return "0"


def _norm_date(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return s[:10] if len(s) >= 10 else s


def build_business_payload(
    *,
    po_number: str,
    vendor: str,
    lines: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Normalized business-only representation for hashing."""
    normalized_lines: List[Dict[str, str]] = []
    for idx, line in enumerate(lines, start=1):
        normalized_lines.append(
            {
                "item_code": _norm_text(
                    line.get("item_no") or line.get("item_code") or line.get("erp_code")
                ),
                "item_name": _norm_text(
                    line.get("description") or line.get("item_name")
                ),
                "quantity": _norm_decimal(
                    line.get("qty_ordered") or line.get("quantity")
                ),
                "rate": _norm_decimal(line.get("unit_price") or line.get("rate")),
                "uom": _norm_text(line.get("uom")),
                "delivery_date": _norm_date(line.get("delivery_date")),
                "line_no": str(line.get("line_no") or idx),
            }
        )

    normalized_lines.sort(
        key=lambda row: (row["line_no"], row["item_code"], row["item_name"])
    )

    return {
        "po_number": _norm_text(po_number),
        "vendor": _norm_text(vendor),
        "lines": normalized_lines,
    }


def business_hash_from_payload(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def business_hash(
    *,
    po_number: str,
    vendor: str,
    lines: List[Dict[str, Any]],
) -> str:
    payload = build_business_payload(
        po_number=po_number, vendor=vendor, lines=lines
    )
    return business_hash_from_payload(payload)
