"""Keep vendor, local users, and Foundation user emails in sync."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import User, Vendor
from services.auth_utils import _is_users_schema_mismatch
from services.foundation_user import update_foundation_user_email

logger = logging.getLogger(__name__)


def normalize_vendor_email(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def sync_vendor_email_change(
    db: Session,
    *,
    vendor: Vendor,
    old_email: Optional[str],
    new_email: str,
    current_user_id: Optional[int] = None,
) -> None:
    """
    When ``vendors.email`` changes, mirror the new email on:
    - local ``users`` rows linked to this vendor (or matching the old email)
    - the corresponding Foundation user (PATCH /users/{id})
    """
    normalized_new = normalize_vendor_email(new_email)
    normalized_old = normalize_vendor_email(old_email)

    if not normalized_new:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email cannot be empty",
        )
    if normalized_new == normalized_old:
        return

    conflict_vendor = (
        db.query(Vendor.vendor_id)
        .filter(
            func.lower(Vendor.email) == normalized_new,
            Vendor.vendor_id != vendor.vendor_id,
        )
        .first()
    )
    if conflict_vendor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already used by another vendor",
        )

    lookup_old = normalized_old or normalized_new
    try:
        users_to_update = (
            db.query(User).filter(User.vendor_id == vendor.vendor_id).all()
        )
        if not users_to_update and normalized_old:
            users_to_update = (
                db.query(User)
                .filter(func.lower(User.email) == normalized_old)
                .all()
            )
        if not users_to_update and normalized_new:
            users_to_update = (
                db.query(User)
                .filter(
                    User.vendor_id == vendor.vendor_id,
                    func.lower(User.email) == normalized_new,
                )
                .all()
            )

        linked_ids = {u.user_id for u in users_to_update}
        if current_user_id is not None:
            linked_ids.add(current_user_id)
        conflict_query = db.query(User.user_id).filter(
            func.lower(User.email) == normalized_new
        )
        if linked_ids:
            conflict_query = conflict_query.filter(User.user_id.notin_(linked_ids))
        conflict_user = conflict_query.first()
        if conflict_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered to another user account",
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Deployed `users` is often the Foundation shape and has no email column.
        # Registration submit must still save; login email lives in Foundation.
        if not _is_users_schema_mismatch(exc):
            raise
        db.rollback()
        logger.warning(
            "[vendor-email] Local users schema has no email column; "
            "skipping local login sync for vendor_id=%s",
            vendor.vendor_id,
        )
        update_foundation_user_email(old_email=lookup_old, new_email=normalized_new)
        return

    update_foundation_user_email(old_email=lookup_old, new_email=normalized_new)

    updated_count = 0
    for user in users_to_update:
        user_norm = normalize_vendor_email(user.email)
        if user_norm == normalized_new:
            continue
        # Only mirror vendor email onto login accounts that previously used it.
        if normalized_old and user_norm != normalized_old:
            logger.info(
                "[vendor-email] Skipping user_id=%s — login email %s differs from previous vendor email",
                user.user_id,
                user.email,
            )
            continue
        taken_by_other = (
            db.query(User.user_id)
            .filter(
                func.lower(User.email) == normalized_new,
                User.user_id != user.user_id,
            )
            .first()
        )
        if taken_by_other:
            logger.info(
                "[vendor-email] Skipping user_id=%s — %s already used by user_id=%s",
                user.user_id,
                normalized_new,
                taken_by_other[0],
            )
            continue
        user.email = normalized_new
        updated_count += 1

    if updated_count:
        logger.info(
            "[vendor-email] Updated %s local user(s) for vendor_id=%s to %s",
            updated_count,
            vendor.vendor_id,
            normalized_new,
        )
