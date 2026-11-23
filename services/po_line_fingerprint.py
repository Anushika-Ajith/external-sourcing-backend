"""Resolve product_fingerprint from vendor_items for PO line create."""

from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from models import VendorItem
from services.document_line_embedding_sync import resolve_vendor_item
from services.po_vendor_rate_enrich import _find_vendor_item_for_po_line


def _as_fingerprint(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict) and value:
        return value
    return None


def resolve_vendor_item_for_po_line_fingerprint(
    db: Session,
    *,
    vendor_id: int,
    item_id: Optional[int] = None,
    item_no: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[VendorItem]:
    """Locate the vendor catalog row that owns the product fingerprint."""
    if item_id is not None:
        by_pk = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == vendor_id,
                VendorItem.id == item_id,
            )
            .first()
        )
        if by_pk is not None:
            return by_pk

    hit = resolve_vendor_item(
        db,
        vendor_id,
        item_id=item_id,
        item_no=item_no,
    )
    if hit is not None:
        return hit

    return _find_vendor_item_for_po_line(
        db,
        vendor_id=vendor_id,
        line={"item_no": item_no, "description": description},
    )


def get_product_fingerprint_from_vendor_item(
    db: Session,
    *,
    vendor_id: int,
    item_id: Optional[int] = None,
    item_no: Optional[str] = None,
    description: Optional[str] = None,
    vendor_item: Optional[VendorItem] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return vendor_items.product_fingerprint for the matched catalog product.
    """
    item = vendor_item or resolve_vendor_item_for_po_line_fingerprint(
        db,
        vendor_id=vendor_id,
        item_id=item_id,
        item_no=item_no,
        description=description,
    )
    if item is None:
        return None
    return _as_fingerprint(item.product_fingerprint)


def attach_product_fingerprint_to_line_dict(
    db: Session,
    line_dict: Dict[str, Any],
    *,
    vendor_id: int,
    vendor_item: Optional[VendorItem] = None,
) -> Dict[str, Any]:
    """
    Set product_fingerprint on a PO line payload from vendor_items when missing.
    Always prefers the catalog fingerprint when a vendor item can be resolved.
    """
    catalog_fp = get_product_fingerprint_from_vendor_item(
        db,
        vendor_id=vendor_id,
        item_id=line_dict.get("item_id"),
        item_no=line_dict.get("item_no"),
        description=line_dict.get("description"),
        vendor_item=vendor_item,
    )
    if catalog_fp is not None:
        line_dict["product_fingerprint"] = catalog_fp
    return line_dict
