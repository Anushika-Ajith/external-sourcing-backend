"""
Merge structured OCR / Gemini vendor extraction into the `vendors` ORM row.

See `ocr.data_cleanup.VendorMaster` for field shapes.
Only fills blank fields — never overwrites existing supplier identity values.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from models import Vendor

_VENDOR_STRING_FIELDS = (
    "company_name",
    "vendor_type",
    "gst_number",
    "pan_number",
    "contact_person",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "country",
    "postal_code",
)

_VENDOR_TYPES_CANON = {"Service", "Product", "Trading", "Mixed"}

_PLACEHOLDER_RE = re.compile(
    r"(to be completed|details to be completed|enter (company|supplier|buyer|contact)|n/?a\b|not available|tbd\b)",
    re.IGNORECASE,
)


def _norm_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if not s or _PLACEHOLDER_RE.search(s):
        return None
    return s


def _has_value(val: Any) -> bool:
    return bool(str(val or "").strip())


def merge_vendor_master_from_dict(
    db: Session,
    vendor: Vendor,
    master: Optional[Dict[str, Any]],
) -> bool:
    """
    Fill blank fields on `vendor` from extracted master.

    Existing non-empty values are preserved. email / mobile respect UNIQUE —
    only set if blank and no other vendor owns them.

    Returns True if any attribute was changed.
    """
    if not master:
        return False

    changed = False
    had_company_name = _has_value(vendor.company_name)

    for field in _VENDOR_STRING_FIELDS:
        if _has_value(getattr(vendor, field, None)):
            continue
        raw = master.get(field)
        nv = _norm_str(raw)
        if nv is None:
            continue
        if field == "vendor_type":
            vt = nv if nv in _VENDOR_TYPES_CANON else nv.title()
            if vt not in _VENDOR_TYPES_CANON:
                vt = "Service"
            nv = vt
        setattr(vendor, field, nv)
        changed = True

    # Only apply branch/parent from OCR while identity was blank before this merge.
    if not had_company_name:
        ib = master.get("is_branch")
        if ib is not None:
            nb = bool(ib)
            if vendor.is_branch != nb:
                vendor.is_branch = nb
                changed = True

        if vendor.parent_vendor_id is None:
            pv = master.get("parent_vendor_id")
            if pv not in (None, ""):
                try:
                    pid = int(pv)
                    vendor.parent_vendor_id = pid
                    changed = True
                except (TypeError, ValueError):
                    pass

    em = _norm_str(master.get("email"))
    if em and not _has_value(vendor.email):
        conflict = (
            db.query(Vendor.vendor_id)
            .filter(Vendor.email == em, Vendor.vendor_id != vendor.vendor_id)
            .first()
        )
        if not conflict:
            vendor.email = em
            changed = True

    mob = _norm_str(master.get("mobile"))
    if mob and not _has_value(vendor.mobile):
        conflict = (
            db.query(Vendor.vendor_id)
            .filter(Vendor.mobile == mob, Vendor.vendor_id != vendor.vendor_id)
            .first()
        )
        if not conflict:
            vendor.mobile = mob
            changed = True

    return changed


def apply_structured_vendor_payload_to_db(
    db: Session,
    vendor_id: int,
    payload: Any,
) -> bool:
    """
    `payload` is the dict returned by `extract_vendor_data` / OCR pipeline:
    { "vendor": {...}, "banks": [...], ... }

    Updates only the vendors row for ``vendor_id`` from ``payload["vendor"]``.

    Returns True if the vendor row was modified.
    """
    if not isinstance(payload, dict):
        return False
    master = payload.get("vendor")
    if not isinstance(master, dict):
        return False

    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        return False

    return merge_vendor_master_from_dict(db, vendor, master)
