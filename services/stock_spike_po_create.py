"""Create a purchase order from selected stock rows (sales spike / stock master workflow)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import PurchaseOrderHeader, PurchaseOrderLine, Stock, Vendor, VendorItem
from services.next_numbering import get_next_po_number_from_nest
from services.ocr_item_rate_import import _find_existing_vendor_item
from services.po_line_embedding_sync import create_embeddings_for_po_lines
from services.po_line_fingerprint import get_product_fingerprint_from_vendor_item
from services.po_line_qty import qty_fields_for_create
from services.po_amounts import ensure_po_line_rates_and_totals, recalculate_po_totals
from services.po_pdf_service import generate_and_attach_po_pdf
from services.po_vendor_rate_enrich import (
    _find_vendor_item_for_po_line,
    resolve_po_line_rate_fields,
)


def _to_decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _order_qty(row: Stock) -> Decimal:
    suggested = _to_decimal(row.suggested_qty)
    if suggested > 0:
        return suggested
    reorder = _to_decimal(row.reorder_qty)
    if reorder > 0:
        return reorder
    sales = _to_decimal(row.last_week_sales)
    if sales > 0:
        return sales
    return Decimal("1")


def _normalize_qty_overrides(
    quantities: Optional[Dict[str, float]],
) -> Dict[int, Decimal]:
    if not quantities:
        return {}
    out: Dict[int, Decimal] = {}
    for key, value in quantities.items():
        try:
            stock_id = int(key)
        except (TypeError, ValueError):
            continue
        qty = _to_decimal(value)
        if qty > 0:
            out[stock_id] = qty
    return out


def _resolve_vendor_item_for_stock(
    db: Session,
    *,
    vendor_id: int,
    stock_row: Stock,
) -> Optional[VendorItem]:
    """Match stock master product to vendor_items for the selected vendor."""
    code = (stock_row.product_code or "").strip() or None
    name = (stock_row.product_name or "").strip() or ""
    barcode = (stock_row.barcode or "").strip() or None

    hit = _find_existing_vendor_item(
        db,
        vendor_id,
        name or (code or ""),
        code,
        code,
        barcode,
    )
    if hit is not None:
        return hit

    return _find_vendor_item_for_po_line(
        db,
        vendor_id=vendor_id,
        line={"item_no": code, "description": name or None},
    )


def _po_item_id_from_vendor_item(vendor_item: Optional[VendorItem]) -> Optional[int]:
    """Prefer vendor_items.item_id; fall back to vendor_items.id."""
    if vendor_item is None:
        return None
    if vendor_item.item_id is not None:
        return int(vendor_item.item_id)
    if vendor_item.id is not None:
        return int(vendor_item.id)
    return None


def create_purchase_order_from_spike_stocks(
    db: Session,
    *,
    stock_ids: List[int],
    tenant_id: int,
    company_id: int,
    vendor_id: int,
    user_id: int,
    department_id: int = 1,
    document_type: str = "PO",
    version_no: int = 1,
    quantities: Optional[Dict[str, float]] = None,
) -> PurchaseOrderHeader:
    if not stock_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one product.",
        )

    unique_ids = list(dict.fromkeys(stock_ids))
    qty_overrides = _normalize_qty_overrides(quantities)
    rows: List[Stock] = (
        db.query(Stock)
        .filter(Stock.stock_id.in_(unique_ids))
        .all()
    )
    by_id = {row.stock_id: row for row in rows}
    missing = [sid for sid in unique_ids if sid not in by_id]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Stock record(s) not found: {missing}",
        )

    ordered_rows = [by_id[sid] for sid in unique_ids]

    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid vendor_id={vendor_id}",
        )

    try:
        po_number = get_next_po_number_from_nest(
            tenant_id=tenant_id,
            company_id=company_id,
            department_id=department_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to allocate PO number: {exc}",
        ) from exc

    header = PurchaseOrderHeader(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        po_number=po_number,
        version_no=version_no,
        po_date=date.today(),
        vendor_id=vendor_id,
        vendor_name_snapshot=vendor.company_name,
        currency_code="INR",
        status="OPEN",
        approval_status="DRAFT",
        source_type="STOCK_MASTER",
        remarks=f"Created from {len(ordered_rows)} stock product(s).",
        revision_no=1,
        current_revision=True,
        current_editor="BUYER",
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(header)
    db.flush()

    for line_no, stock_row in enumerate(ordered_rows, start=1):
        if stock_row.stock_id in qty_overrides:
            qty = qty_overrides[stock_row.stock_id]
        else:
            qty = _order_qty(stock_row)
        qty_fields = qty_fields_for_create(suggested_qty=qty, qty_ordered=qty)
        vendor_item = _resolve_vendor_item_for_stock(
            db, vendor_id=vendor_id, stock_row=stock_row
        )
        item_id = _po_item_id_from_vendor_item(vendor_item)
        uom = (
            (vendor_item.uom if vendor_item and vendor_item.uom else None)
            or stock_row.unit
        )
        product_fingerprint = get_product_fingerprint_from_vendor_item(
            db,
            vendor_id=vendor_id,
            item_id=item_id,
            item_no=stock_row.product_code,
            description=stock_row.product_name,
            vendor_item=vendor_item,
        )
        rate_fields = resolve_po_line_rate_fields(
            db,
            vendor_id=vendor_id,
            line={
                "item_no": stock_row.product_code,
                "description": stock_row.product_name,
            },
            vendor_item=vendor_item,
        )
        unit_price = rate_fields["unit_price"]
        po_item_rate = rate_fields["po_item_rate"] or unit_price
        line_kwargs: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "company_id": company_id,
            "document_type": document_type,
            "po_number": po_number,
            "version_no": version_no,
            "line_no": line_no,
            "po_id": header.po_id,
            "item_id": item_id,
            "item_no": stock_row.product_code,
            "description": stock_row.product_name,
            "uom": uom,
            "product_fingerprint": product_fingerprint,
            "po_item_rate": po_item_rate,
            "rate_contract_item_rate": rate_fields["rate_contract_item_rate"],
            "line_status": "OPEN",
            "remarks": f"Stock master · stock_id={stock_row.stock_id}",
            "created_by": user_id,
            "updated_by": user_id,
            **qty_fields,
        }
        if unit_price is not None:
            line_kwargs["unit_price"] = unit_price
        db.add(PurchaseOrderLine(**line_kwargs))

    db.flush()
    created_lines = (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.po_id == header.po_id)
        .order_by(PurchaseOrderLine.line_no)
        .all()
    )
    ensure_po_line_rates_and_totals(db, header, commit=False)
    create_embeddings_for_po_lines(db, header, created_lines)
    generate_and_attach_po_pdf(header, created_lines)
    header.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(header)
    return header
