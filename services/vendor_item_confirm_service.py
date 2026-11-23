"""Confirm vendor catalog items after rate-contract OCR import."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import VendorItem
from services.ocr_item_rate_import import assign_vendor_item_master_codes
from services.rate_contract_product_matching_engine import (
    VENDOR_ITEM_STATUS_CONFIRMED,
    VENDOR_ITEM_STATUS_PENDING,
)

logger = logging.getLogger(__name__)


def assert_no_duplicate_vendor_item(
    db: Session,
    db_item: VendorItem,
    *,
    exclude_item_id: Optional[int] = None,
) -> None:
    """
    409 when the vendor already has an item with the same name and pack size.
    Runs on Submit and Confirm only; Save Draft may hold duplicates until then.
    """
    name = ((db_item.product_name or "").strip() or (db_item.item_name or "").strip())
    if not name or name.upper() == "UNKNOWN PRODUCT":
        return
    key = name.lower()
    with db.no_autoflush:
        q = db.query(VendorItem).filter(
            VendorItem.vendor_id == db_item.vendor_id,
            or_(
                func.lower(func.trim(VendorItem.product_name)) == key,
                func.lower(func.trim(VendorItem.item_name)) == key,
            ),
        )
        if exclude_item_id is not None:
            q = q.filter(VendorItem.id != int(exclude_item_id))
        if db_item.pack_size is not None:
            q = q.filter(VendorItem.pack_size == db_item.pack_size)
        duplicate = q.first()
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{name}' already exists for this vendor (item #{duplicate.id}). "
                "Edit or delete the duplicate before submitting or confirming."
            ),
        )


def confirm_vendor_item_status(
    db: Session,
    *,
    vendor_item_id: int,
    vendor_id: Optional[int] = None,
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    """Set ``vendor_items.status`` to CONFIRMED for a pending item and generate its codes.

    Lookup is by item id only. Optional ``vendor_id`` is an extra filter when provided;
    buyers (no linked vendor) can confirm any pending item their permissions allow.
    """
    q = db.query(VendorItem).filter(VendorItem.id == int(vendor_item_id))
    if vendor_id is not None:
        q = q.filter(VendorItem.vendor_id == int(vendor_id))
    db_item = q.first()
    if db_item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor item not found",
        )

    current = (db_item.status or "").strip().upper()
    if current == VENDOR_ITEM_STATUS_CONFIRMED:
        logger.info(
            "[VENDOR ITEM] confirm noop vendor_item_id=%s already CONFIRMED",
            vendor_item_id,
        )
        return {
            "status": "confirmed",
            "vendorItemId": int(db_item.id),
            "itemStatus": VENDOR_ITEM_STATUS_CONFIRMED,
            "alreadyConfirmed": True,
        }

    # ACTIVE: manual rows saved before items needed a confirm step.
    if current and current not in (VENDOR_ITEM_STATUS_PENDING, "ACTIVE"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Item status {db_item.status!r} cannot be confirmed",
        )

    assert_no_duplicate_vendor_item(db, db_item, exclude_item_id=db_item.id)

    try:
        # Codes (vendor_product_code, erp_code, company_code) are generated only here.
        assign_vendor_item_master_codes(db, db_item, created_by=created_by)
        db_item.status = VENDOR_ITEM_STATUS_CONFIRMED
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(db_item)
    logger.info(
        "[VENDOR ITEM] confirmed vendor_item_id=%s vendor_id=%s",
        vendor_item_id,
        db_item.vendor_id,
    )
    return {
        "status": "confirmed",
        "vendorItemId": int(db_item.id),
        "itemStatus": VENDOR_ITEM_STATUS_CONFIRMED,
        "alreadyConfirmed": False,
        "vendorProductCode": db_item.vendor_product_code,
        "erpCode": db_item.erp_code,
        "companyCode": db_item.company_code,
    }
