"""Vendor business code helpers.

``vendors.vendor_code`` is the FK target for ``vendor_items.vendor_code``.
It is assigned from Nest next-numbering **on approval** only (never on Save
Draft or Submit).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from models import Vendor, VendorItem
from services.next_numbering import get_next_vendor_registration_number_from_nest

# Returned when OCR is attempted before Nest next-numbering assigned a vendor_code.
VENDOR_NOT_APPROVED_OCR_DETAIL = "Vendor not approved yet"


def clip_vendor_code(value: Optional[str], max_len: int = 20) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None
    return text[:max_len]


def ensure_vendor_code_from_registration(vendor: Vendor) -> Optional[str]:
    """
    Return an existing vendor_code / registration_no if already assigned.

    Does **not** mint a next-numbering value — that happens on approval only.
    """
    existing = clip_vendor_code(getattr(vendor, "vendor_code", None))
    if existing:
        if vendor.vendor_code != existing:
            vendor.vendor_code = existing
        return existing

    from_reg = clip_vendor_code(getattr(vendor, "vendor_registration_no", None))
    if from_reg:
        vendor.vendor_code = from_reg
        return from_reg

    return None


def assign_vendor_code_on_approval(
    db: Session,
    vendor: Vendor,
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
) -> str:
    """
    Mint next-numbering code on approve and stamp vendor + draft items.

    Idempotent when ``vendor.vendor_code`` is already set.
    """
    existing = clip_vendor_code(getattr(vendor, "vendor_code", None))
    if existing:
        _backfill_item_vendor_codes(db, vendor.vendor_id, existing)
        return existing

    # Prefer an already-issued registration no (do not mint a second number).
    from_reg = clip_vendor_code(getattr(vendor, "vendor_registration_no", None))
    if from_reg:
        vendor.vendor_code = from_reg
        _backfill_item_vendor_codes(db, vendor.vendor_id, from_reg)
        return from_reg

    tid = int(tenant_id if tenant_id is not None else (vendor.tenant_id or 1))
    cid = int(company_id if company_id is not None else (vendor.company_id or 3))

    reg_no = get_next_vendor_registration_number_from_nest(
        tenant_id=tid,
        company_id=cid,
        department_id=1,
    )
    code = clip_vendor_code(reg_no)
    if not code:
        raise ValueError("Next numbering returned an empty vendor code")

    vendor.vendor_registration_no = reg_no
    vendor.vendor_code = code
    _backfill_item_vendor_codes(db, vendor.vendor_id, code)
    return code


def _backfill_item_vendor_codes(db: Session, vendor_id: int, code: str) -> None:
    db.query(VendorItem).filter(
        VendorItem.vendor_id == int(vendor_id),
        (VendorItem.vendor_code.is_(None)) | (VendorItem.vendor_code == ""),
    ).update({VendorItem.vendor_code: code}, synchronize_session=False)
