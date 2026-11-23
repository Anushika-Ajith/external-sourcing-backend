"""Look up vendors by phone / mobile number."""

from __future__ import annotations

import re
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import User, Vendor


def mobile_lookup_variants(mobile: str) -> List[str]:
    """Build common stored forms for the same phone (with/without country code)."""
    raw = (mobile or "").strip()
    digits = re.sub(r"\D", "", raw)
    variants: set[str] = set()
    if raw:
        variants.add(raw)
    if digits:
        variants.add(digits)
        if len(digits) == 10:
            variants.add(f"91{digits}")
            variants.add(f"+91{digits}")
        if digits.startswith("91") and len(digits) == 12:
            variants.add(digits[2:])
            variants.add(f"+{digits}")
        if digits.startswith("0") and len(digits) == 11:
            variants.add(digits[1:])
    return [v for v in variants if v]


def find_vendor_by_phone(db: Session, mobile: str) -> Optional[Vendor]:
    variants = mobile_lookup_variants(mobile)
    if not variants:
        return None
    return db.query(Vendor).filter(Vendor.mobile.in_(variants)).first()


def find_other_vendor_by_phone(
    db: Session, mobile: str, *, exclude_vendor_id: int
) -> Optional[Vendor]:
    """
    Another vendor already using ``mobile``. Compares digits only so stored
    forms like ``+91 98765 43210`` still match. Skips the vendor being saved
    and DRAFT vendors, whose unvalidated numbers must not block others.
    """
    digit_variants = {re.sub(r"\D", "", v) for v in mobile_lookup_variants(mobile)}
    digit_variants.discard("")
    if not digit_variants:
        return None
    return (
        db.query(Vendor)
        .filter(
            func.regexp_replace(Vendor.mobile, "[^0-9]", "", "g").in_(digit_variants),
            Vendor.vendor_id != exclude_vendor_id,
            func.upper(func.coalesce(Vendor.status, "")) != "DRAFT",
        )
        .first()
    )


def user_password_is_set(user: Optional[User]) -> bool:
    if user is None:
        return False
    return bool((user.hashed_password or "").strip())


def vendor_has_login(db: Session, vendor: Vendor) -> bool:
    """
    True when the vendor already has a login account.

    Prefer local ``users`` (password hash). When the DB is Foundation-shaped
    (no ``users.email`` / ``hashed_password``), fall back to Foundation
    user-by-email so ``/vendor/check-email`` does not 500.
    """
    from services.auth_utils import _is_users_schema_mismatch

    try:
        user = None
        if vendor.email:
            user = db.query(User).filter(User.email == vendor.email).first()
        if user is None and getattr(vendor, "vendor_id", None) is not None:
            user = (
                db.query(User)
                .filter(User.vendor_id == vendor.vendor_id)
                .first()
            )
        if user_password_is_set(user):
            return True
        # Local row missing/empty password — still check Foundation below.
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        if not _is_users_schema_mismatch(exc):
            raise

    email = (getattr(vendor, "email", None) or "").strip()
    if not email:
        return False
    try:
        from services.foundation_user import _lookup_foundation_user_id_by_email

        return _lookup_foundation_user_id_by_email(email) is not None
    except Exception:
        return False
