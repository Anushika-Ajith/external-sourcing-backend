"""Create a purchase order from ADC sales-spike products (sales_transactions)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import Product, PurchaseOrderHeader, PurchaseOrderLine, Stock, Vendor
from services.next_numbering import get_next_po_number_from_nest
from services.po_line_embedding_sync import create_embeddings_for_po_lines
from services.po_line_fingerprint import get_product_fingerprint_from_vendor_item
from services.po_line_qty import qty_fields_for_create
from services.po_amounts import ensure_po_line_rates_and_totals, recalculate_po_totals
from services.po_pdf_service import generate_and_attach_po_pdf
from services.sales_transaction_adc import SalesAdcSpikeAnalysis, detect_sales_adc_spikes
from services.po_vendor_rate_enrich import (
    _find_vendor_item_for_po_line,
    resolve_po_line_rate_fields,
)
from services.stock_spike_po_create import (
    _po_item_id_from_vendor_item,
    _resolve_vendor_item_for_stock,
)

def _to_decimal(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _order_qty_from_adc(analysis: SalesAdcSpikeAnalysis) -> Decimal:
    suggested = _to_decimal(analysis.suggested_qty)
    if suggested > 0:
        return suggested
    increase = _to_decimal(analysis.demand_increase_units)
    if increase > 0:
        return increase * Decimal(str(analysis.period_days))
    return Decimal("1")


def _normalize_qty_overrides(
    quantities: Optional[Dict[str, float]],
) -> Dict[int, Decimal]:
    if not quantities:
        return {}
    out: Dict[int, Decimal] = {}
    for key, value in quantities.items():
        try:
            product_id = int(key)
        except (TypeError, ValueError):
            continue
        qty = _to_decimal(value)
        if qty > 0:
            out[product_id] = qty
    return out


def create_purchase_order_from_adc_spike_products(
    db: Session,
    *,
    product_ids: List[int],
    tenant_id: int,
    company_id: int,
    vendor_id: int,
    user_id: int,
    department_id: int = 1,
    document_type: str = "PO",
    version_no: int = 1,
    quantities: Optional[Dict[str, float]] = None,
) -> PurchaseOrderHeader:
    if not product_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one product.",
        )

    unique_ids = list(dict.fromkeys(int(pid) for pid in product_ids))
    qty_overrides = _normalize_qty_overrides(quantities)

    spikes: List[SalesAdcSpikeAnalysis] = detect_sales_adc_spikes(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        only_huge=False,
        only_sustained=False,
        min_growth_pct=-1000.0,
        limit=500,
    )
    spike_by_id: Dict[int, SalesAdcSpikeAnalysis] = {
        s.product_id: s for s in spikes
    }

    products = (
        db.query(Product).filter(Product.product_id.in_(unique_ids)).all()
    )
    product_by_id = {p.product_id: p for p in products}

    missing = [pid for pid in unique_ids if pid not in spike_by_id and pid not in product_by_id]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No sales ADC data or product catalog entry for product_id(s): {missing}",
        )

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
        source_type="SALES_ADC_SPIKE",
        remarks=f"Created from {len(unique_ids)} ADC sales-spike product(s).",
        revision_no=1,
        current_revision=True,
        current_editor="BUYER",
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(header)
    db.flush()

    for line_no, product_id in enumerate(unique_ids, start=1):
        adc = spike_by_id.get(product_id)
        product = product_by_id.get(product_id)
        if product_id in qty_overrides:
            qty = qty_overrides[product_id]
        else:
            qty = _order_qty_from_adc(adc) if adc else Decimal("1")

        item_no = str(product_id)
        description = (
            (product.name if product else None)
            or (adc.product_name if adc else None)
            or f"Product {product_id}"
        )

        stock_row = (
            db.query(Stock)
            .filter(
                Stock.tenant_id == tenant_id,
                Stock.product_code == item_no,
            )
            .first()
        )
        vendor_item = (
            _resolve_vendor_item_for_stock(
                db, vendor_id=vendor_id, stock_row=stock_row
            )
            if stock_row is not None
            else None
        )
        if vendor_item is None:
            # Fall back to matching by product id / name against vendor catalog.
            vendor_item = _find_vendor_item_for_po_line(
                db,
                vendor_id=vendor_id,
                line={"item_no": item_no, "description": description},
            )
        item_id = _po_item_id_from_vendor_item(vendor_item)
        uom = (
            (vendor_item.uom if vendor_item and vendor_item.uom else None)
            or (stock_row.unit if stock_row and stock_row.unit else None)
            or "EA"
        )
        product_fingerprint = get_product_fingerprint_from_vendor_item(
            db,
            vendor_id=vendor_id,
            item_id=item_id,
            item_no=item_no,
            description=description,
            vendor_item=vendor_item,
        )

        growth_note = f"{adc.growth_pct}%" if adc else "n/a"
        qty_fields = qty_fields_for_create(suggested_qty=qty, qty_ordered=qty)
        rate_fields = resolve_po_line_rate_fields(
            db,
            vendor_id=vendor_id,
            line={"item_no": item_no, "description": description},
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
            "item_no": item_no,
            "description": description[:255] if description else item_no,
            "uom": uom,
            "product_fingerprint": product_fingerprint,
            "po_item_rate": po_item_rate,
            "rate_contract_item_rate": rate_fields["rate_contract_item_rate"],
            "line_status": "OPEN",
            "remarks": f"ADC spike · product_id={product_id} · growth={growth_note}",
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
