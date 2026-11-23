"""Vendor-scoped access helpers for purchase orders and profile APIs."""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import PurchaseOrderHeader, User, Vendor
from services.vendor_email_sync import normalize_vendor_email

logger = logging.getLogger(__name__)


def is_vendor_actor(user: User) -> bool:
    """Deprecated role gate — always False. Access is permissions-API driven."""
    _ = user
    return False


def find_vendor_by_email(db: Session, email: Optional[str]) -> Optional[Vendor]:
    """Case-insensitive lookup of a vendor by email."""
    normalized = normalize_vendor_email(email)
    if not normalized:
        return None
    return (
        db.query(Vendor)
        .filter(func.lower(func.trim(Vendor.email)) == normalized)
        .order_by(Vendor.vendor_id.asc())
        .first()
    )


def resolve_actor_vendor_id(db: Session, user: User) -> Optional[int]:
    """
    Resolve the vendor_id that scopes PO visibility for a vendor user.
    Prefers users.vendor_id; falls back to vendors.email match (case-insensitive).
    """
    if user.vendor_id is not None:
        return int(user.vendor_id)
    vendor = find_vendor_by_email(db, getattr(user, "email", None))
    return int(vendor.vendor_id) if vendor is not None else None


def resolve_vendor_for_user(
    db: Session,
    user: User,
    *,
    persist_link: bool = True,
) -> Optional[Vendor]:
    """
    Return the Vendor row for this user.

    Prefer ``users.vendor_id``. If missing (common after Foundation JWT login),
    resolve by email and optionally persist ``users.vendor_id`` so later
    document/bank/profile calls work.
    """
    if user.vendor_id is not None:
        vendor = (
            db.query(Vendor)
            .filter(Vendor.vendor_id == int(user.vendor_id))
            .first()
        )
        if vendor is not None:
            return vendor

    vendor = find_vendor_by_email(db, getattr(user, "email", None))
    if vendor is None:
        return None

    if persist_link and getattr(user, "user_id", None) is not None:
        try:
            user.vendor_id = int(vendor.vendor_id)
            if (user.role or "").strip().lower() != "vendor":
                # Keep Foundation role names when not already vendor; profile still works.
                pass
            db.add(user)
            db.commit()
            db.refresh(user)
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(
                "[vendor-scope] could not persist users.vendor_id=%s for user_id=%s: %s",
                vendor.vendor_id,
                getattr(user, "user_id", None),
                exc,
            )
            # Still return vendor for this request even if persist failed.
            user.vendor_id = int(vendor.vendor_id)

    return vendor


def vendor_can_access_po(
    header: PurchaseOrderHeader,
    scoped_vendor_id: Optional[int],
) -> bool:
    """Vendor users may view a PO only when it belongs to their vendor_id."""
    if scoped_vendor_id is None:
        return False
    return int(header.vendor_id) == int(scoped_vendor_id)
