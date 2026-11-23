"""Delete a vendor and related onboarding / ERP records."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import (
    OTP,
    RFQVendor,
    User,
    Vendor,
    VendorBank,
    VendorDocument,
    VendorItem,
    VendorRate,
)
from services.foundation_user import delete_foundation_user_by_email
from services.po_delete_all_service import delete_all_purchase_orders
from services.vendor_email_sync import normalize_vendor_email

logger = logging.getLogger(__name__)


def delete_vendor_and_related(
    db: Session,
    *,
    vendor_id: int,
    current_user: User,
) -> Dict[str, Any]:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )

    branch_count = (
        db.query(Vendor.vendor_id)
        .filter(Vendor.parent_vendor_id == vendor_id)
        .count()
    )
    if branch_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a vendor that has branch vendors. Delete branches first.",
        )

    po_result = delete_all_purchase_orders(
        db,
        tenant_id=vendor.tenant_id,
        company_id=vendor.company_id,
        vendor_id=vendor_id,
        current_user=current_user,
    )

    deleted_rfq_vendors = (
        db.query(RFQVendor)
        .filter(RFQVendor.vendor_id == vendor_id)
        .delete(synchronize_session=False)
    )

    item_ids_subq = db.query(VendorItem.id).filter(VendorItem.vendor_id == vendor_id)
    deleted_rates = (
        db.query(VendorRate)
        .filter(VendorRate.item_id.in_(item_ids_subq))
        .delete(synchronize_session=False)
    )
    deleted_items = (
        db.query(VendorItem)
        .filter(VendorItem.vendor_id == vendor_id)
        .delete(synchronize_session=False)
    )
    deleted_banks = (
        db.query(VendorBank)
        .filter(VendorBank.vendor_id == vendor_id)
        .delete(synchronize_session=False)
    )
    deleted_documents = (
        db.query(VendorDocument)
        .filter(VendorDocument.vendor_id == vendor_id)
        .delete(synchronize_session=False)
    )

    linked_users = db.query(User).filter(User.vendor_id == vendor_id).all()
    foundation_emails: set[str] = set()
    if vendor.email:
        normalized_vendor_email = normalize_vendor_email(vendor.email)
        if normalized_vendor_email:
            foundation_emails.add(normalized_vendor_email)
    for linked_user in linked_users:
        normalized_user_email = normalize_vendor_email(linked_user.email)
        if normalized_user_email:
            foundation_emails.add(normalized_user_email)

    deleted_foundation_users = sum(
        1 for email in foundation_emails if delete_foundation_user_by_email(email)
    )

    deleted_users = (
        db.query(User)
        .filter(User.vendor_id == vendor_id)
        .delete(synchronize_session=False)
    )

    deleted_otps = 0
    if vendor.email:
        deleted_otps = (
            db.query(OTP)
            .filter(OTP.email == vendor.email)
            .delete(synchronize_session=False)
        )

    db.delete(vendor)
    db.commit()

    logger.info(
        "[vendor-delete] Removed vendor_id=%s users=%s items=%s pos=%s",
        vendor_id,
        deleted_users,
        deleted_items,
        po_result.get("deleted", 0),
    )

    return {
        "vendor_id": vendor_id,
        "deleted_purchase_orders": int(po_result.get("deleted") or 0),
        "deleted_po_lines": int(po_result.get("deleted_po_lines") or 0),
        "deleted_grn_headers": int(po_result.get("deleted_grn_headers") or 0),
        "deleted_invoice_headers": int(po_result.get("deleted_invoice_headers") or 0),
        "deleted_rfq_vendors": int(deleted_rfq_vendors or 0),
        "deleted_vendor_items": int(deleted_items or 0),
        "deleted_vendor_rates": int(deleted_rates or 0),
        "deleted_vendor_banks": int(deleted_banks or 0),
        "deleted_vendor_documents": int(deleted_documents or 0),
        "deleted_users": int(deleted_users or 0),
        "deleted_foundation_users": int(deleted_foundation_users or 0),
        "deleted_otps": int(deleted_otps or 0),
    }
