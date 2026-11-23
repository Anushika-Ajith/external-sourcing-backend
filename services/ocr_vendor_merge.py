"""
Merge structured OCR output (extract_vendor_data) into the Vendor ORM row.

Only fills empty Vendor columns from non-empty OCR values.
Never overwrites an already-set company_name / contact / address / tax id, etc.
Email/mobile are also guarded against unique-constraint conflicts with other vendors.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from models import Vendor

# Fields on models.Vendor that map from OCR VendorMaster (ocr/data_cleanup.py)
_STRING_FIELDS = (
    "company_name",
    "vendor_type",
    "gst_number",
    "pan_number",
    "contact_person",
    "email",
    "mobile",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "country",
    "postal_code",
)

_VENDOR_TYPES_CANON = {"Service", "Product", "Trading", "Mixed"}

# Form-label / instruction text OCR often mistakes for real values.
_PLACEHOLDER_RE = re.compile(
    r"(to be completed|details to be completed|enter (company|supplier|buyer|contact)|n/?a\b|not available|tbd\b)",
    re.IGNORECASE,
)


def _normalize_vendor_type(raw: Any) -> Optional[str]:
    cleaned = _clean_str(raw)
    if not cleaned:
        return None
    if cleaned in _VENDOR_TYPES_CANON:
        return cleaned
    lower = cleaned.lower()
    if any(token in lower for token in ("product", "manufacturer", "supplier", "food")):
        return "Product"
    if "trading" in lower:
        return "Trading"
    if "service" in lower:
        return "Service"
    if "mixed" in lower:
        return "Mixed"
    return "Product"


def _clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or _PLACEHOLDER_RE.search(s):
        return None
    return s


def _has_value(val: Any) -> bool:
    return bool(str(val or "").strip())


def _parse_parent_id(val: Any) -> Optional[int]:
    if val is None or val is False:
        return None
    if isinstance(val, int) and not isinstance(val, bool):
        return val if val > 0 else None
    try:
        n = int(str(val).strip())
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def merge_ocr_vendor_master(
    db: Session,
    vendor: Vendor,
    ocr_payload: Dict[str, Any],
) -> None:
    """
    Fill blank fields on `vendor` from `ocr_payload["vendor"]` when present.

    Existing non-empty values (especially company_name) are never overwritten.
    ocr_payload is the dict returned by extract_vendor_data() / extract_raw_text().
    """
    if not isinstance(ocr_payload, dict):
        return
    vm = ocr_payload.get("vendor")
    if not isinstance(vm, dict):
        return

    had_company_name = _has_value(vendor.company_name)

    for key in _STRING_FIELDS:
        raw = vm.get(key)
        cleaned = _clean_str(raw) if raw is not None else None
        if cleaned is None:
            continue
        if key == "vendor_type":
            cleaned = _normalize_vendor_type(cleaned) or cleaned
        if key == "email":
            if _has_value(vendor.email):
                continue
            taken = (
                db.query(Vendor.vendor_id)
                .filter(Vendor.email == cleaned, Vendor.vendor_id != vendor.vendor_id)
                .first()
            )
            if taken:
                continue
            vendor.email = cleaned
            continue
        if key == "mobile":
            if _has_value(vendor.mobile):
                continue
            taken = (
                db.query(Vendor.vendor_id)
                .filter(Vendor.mobile == cleaned, Vendor.vendor_id != vendor.vendor_id)
                .first()
            )
            if taken:
                continue
            vendor.mobile = cleaned
            continue
        if _has_value(getattr(vendor, key, None)):
            continue
        setattr(vendor, key, cleaned)

    # Branch flag / parent id: only apply while identity was blank before this merge.
    if not had_company_name and "is_branch" in vm and vm["is_branch"] is not None:
        vendor.is_branch = bool(vm["is_branch"])

    if not had_company_name and vendor.parent_vendor_id is None:
        pid = _parse_parent_id(vm.get("parent_vendor_id"))
        if pid is not None and pid != vendor.vendor_id:
            parent_exists = (
                db.query(Vendor.vendor_id).filter(Vendor.vendor_id == pid).first()
            )
            if parent_exists:
                vendor.parent_vendor_id = pid
