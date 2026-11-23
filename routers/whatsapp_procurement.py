"""
WhatsApp-key wrappers around the same stock / vendor / PO APIs the
external-sourcing stock-upload UI uses (Generate PO + per-item vendors).

Auth: X-WhatsApp-API-Key only (no user JWT). Seed these URLs into mcp_vectors
so WhatsApp MCP executeMatch can call them without hardcoding in TypeScript.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import Stock, User, Vendor
from schemas.stock import StockResponse
from schemas.stock_spike import (
    CreatePoFromSalesSpikesRequest,
    CreatePoFromSalesSpikesResponse,
)
from schemas.vendor import VendorDirectoryItem, VendorsForErpCodeOut
from services.company_scope import resolve_company_id
from services.internal_product_identity import list_vendors_for_stock_product_codes
from services.po_vendor_rate_enrich import get_vendor_rate_for_product_code
from services.stock_pipeline_qty import (
    compute_pipeline_qty_by_product_codes,
    enrich_stock_quantities,
)
from services.stock_spike_po_create import create_purchase_order_from_spike_stocks
from services.whatsapp_auth import require_whatsapp_api_key
from services.whatsapp_notify_service import notify_po_created_whatsapp
from services.three_way_match_detail_service import get_three_way_match_detail
from services.three_way_match_queue_service import list_three_way_match_queue
from services.whatsapp_procurement_post_invoice import (
    apply_grn_qtys_for_whatsapp,
    approve_grn_for_whatsapp,
    approve_invoice_for_whatsapp,
    confirm_invoice_line_for_whatsapp,
    delete_invoice_line_for_whatsapp,
    prepare_grn_for_whatsapp,
    preview_invoice_match_for_whatsapp,
    update_grn_qty_for_whatsapp,
)

router = APIRouter(
    prefix="",
    tags=["whatsapp-procurement"],
    dependencies=[Depends(require_whatsapp_api_key)],
)


def _default_tenant_id() -> int:
    raw = (os.getenv("MCP_TENANT_ID") or "1").strip()
    try:
        return int(raw)
    except ValueError:
        return 1


def _default_company_id() -> int:
    raw = (os.getenv("MCP_COMPANY_ID") or "3").strip()
    try:
        return resolve_company_id(int(raw))
    except Exception:
        return resolve_company_id(None)


def _bot_user_id(db: Session) -> int:
    """Service user for PO created_by when no WhatsApp-mapped user exists."""
    raw = (os.getenv("WHATSAPP_PROCUREMENT_USER_ID") or "").strip()
    if raw.isdigit():
        return int(raw)
    user = db.query(User).order_by(User.user_id.asc()).first()
    if user and user.user_id is not None:
        return int(user.user_id)
    return 1


def _stock_to_response(stock: Stock, pipeline_map: dict) -> StockResponse:
    product_code = (stock.product_code or "").strip()
    pipeline = pipeline_map.get(product_code)
    if pipeline is None:
        # Same default as UI GET /stock — Decimal, not int (enrich uses .quantize).
        pipeline = pipeline_map.get(product_code.upper(), Decimal("0"))
    if not isinstance(pipeline, Decimal):
        pipeline = Decimal(str(pipeline or 0))
    extras = enrich_stock_quantities(
        available_stock=stock.available_stock,
        reorder_level=stock.reorder_level,
        pipeline_qty=pipeline,
    )
    return StockResponse(
        stock_id=stock.stock_id,
        product_code=stock.product_code,
        barcode=stock.barcode,
        product_name=stock.product_name,
        category=stock.category,
        brand=stock.brand,
        unit=stock.unit,
        available_stock=stock.available_stock,
        reorder_level=stock.reorder_level,
        reorder_qty=stock.reorder_qty,
        last_week_sales=stock.last_week_sales,
        weekly_trend=stock.weekly_trend,
        suggested_qty=stock.suggested_qty,
        preferred_supplier=stock.preferred_supplier,
        tenant_id=stock.tenant_id,
        company_id=stock.company_id,
        created_by=stock.created_by,
        updated_by=stock.updated_by,
        created_at=stock.created_at,
        updated_at=stock.updated_at,
        pipeline_qty=extras["pipeline_qty"],
        stock_with_pipeline=extras["stock_with_pipeline"],
        needs_reorder=bool(extras["needs_reorder"]),
    )


@router.get(
    "/stock/whatsapp/below-reorder",
    response_model=List[StockResponse],
)
async def list_stock_below_reorder_for_whatsapp(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Same filter as UI `GET /stock?below_reorder_level=true`
    (available_stock + pipeline_qty < reorder_level).
    """
    tenant = tenant_id if tenant_id is not None else _default_tenant_id()
    company = resolve_company_id(
        company_id if company_id is not None else _default_company_id()
    )

    q = db.query(Stock)
    if tenant is not None:
        q = q.filter(Stock.tenant_id == tenant)
    if company is not None:
        q = q.filter(Stock.company_id == company)

    candidate_limit = min(max(skip + limit, limit), 1000)
    rows = q.order_by(Stock.stock_id.desc()).limit(candidate_limit).all()
    pipeline_map = compute_pipeline_qty_by_product_codes(
        db,
        tenant_id=tenant if tenant is not None else 1,
        company_id=company,
        product_codes=[row.product_code for row in rows],
    )
    enriched = [_stock_to_response(row, pipeline_map) for row in rows]
    filtered = [row for row in enriched if row.needs_reorder]
    return filtered[skip : skip + limit]


@router.get(
    "/vendor/whatsapp/by-erp-code",
    response_model=List[VendorsForErpCodeOut],
)
async def list_vendors_by_erp_code_for_whatsapp(
    erp_codes: List[str] = Query(
        ...,
        description="stock.product_code values (same as UI GET /vendor/by-erp-code)",
    ),
    product_names: Optional[List[str]] = Query(None),
    company_id: Optional[int] = Query(None),
    tenant_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """Same matching as UI `GET /vendor/by-erp-code` (vendors per product)."""
    scoped_company_id = resolve_company_id(
        company_id if company_id is not None else _default_company_id()
    )
    tenant = tenant_id if tenant_id is not None else _default_tenant_id()

    normalized: List[str] = []
    seen_upper: set[str] = set()
    for raw in erp_codes:
        code = (raw or "").strip()
        if not code or code == "—":
            continue
        key = code.upper()
        if key in seen_upper:
            continue
        seen_upper.add(key)
        normalized.append(code)

    if not normalized:
        return []

    code_to_product_name: Dict[str, str] = {}
    if product_names:
        for idx, code in enumerate(normalized):
            if idx >= len(product_names):
                break
            name = (product_names[idx] or "").strip()
            if name and name != "—":
                code_to_product_name[code] = name

    vendor_ids_by_code = list_vendors_for_stock_product_codes(
        db,
        normalized,
        code_to_product_name=code_to_product_name,
    )
    all_vendor_ids: set[int] = set()
    for ids in vendor_ids_by_code.values():
        all_vendor_ids.update(ids)

    vendors_by_id: Dict[int, Vendor] = {}
    if all_vendor_ids:
        q = (
            db.query(Vendor)
            .filter(
                Vendor.vendor_id.in_(all_vendor_ids),
                Vendor.company_id == scoped_company_id,
                func.upper(Vendor.status).in_(["ACTIVE", "APPROVED"]),
            )
            .order_by(Vendor.company_name.asc().nullslast())
        )
        if tenant is not None:
            q = q.filter(Vendor.tenant_id == int(tenant))
        for vendor in q.all():
            if vendor.vendor_id is not None:
                vendors_by_id[int(vendor.vendor_id)] = vendor

    out: List[VendorsForErpCodeOut] = []
    for code in normalized:
        product_name = code_to_product_name.get(code)
        vendors_for_code: List[VendorDirectoryItem] = []
        for vendor_id in vendor_ids_by_code.get(code, []):
            vendor = vendors_by_id.get(vendor_id)
            if vendor is None:
                continue
            rate_info = get_vendor_rate_for_product_code(
                db,
                vendor_id=int(vendor_id),
                erp_code=code,
                product_name=product_name,
            )
            vendors_for_code.append(
                VendorDirectoryItem(
                    vendor_id=vendor.vendor_id,
                    vendor_name=vendor.company_name,
                    vendor_code=(vendor.vendor_code or vendor.vendor_registration_no),
                    gst_category=vendor.vendor_type,
                    status=vendor.status,
                    rate=rate_info.get("rate"),
                    uom=rate_info.get("uom"),
                    currency=rate_info.get("currency"),
                )
            )
        vendors_for_code.sort(key=lambda v: (v.vendor_name or "").lower())
        out.append(VendorsForErpCodeOut(erp_code=code, vendors=vendors_for_code))
    return out


@router.post(
    "/stock/whatsapp/create-purchase-order",
    response_model=CreatePoFromSalesSpikesResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_purchase_order_for_whatsapp(
    body: CreatePoFromSalesSpikesRequest,
    db: Session = Depends(get_db),
):
    """
    Same create path as UI Generate PO:
    `POST /stock/sales-spikes/create-purchase-order`.
    """
    if body.vendor_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vendor_id is required for WhatsApp PO create",
        )

    tenant_id = int(body.tenant_id or _default_tenant_id())
    company_id = resolve_company_id(body.company_id or _default_company_id())
    user_id = _bot_user_id(db)

    header = create_purchase_order_from_spike_stocks(
        db,
        stock_ids=body.stock_ids,
        tenant_id=tenant_id,
        company_id=company_id,
        vendor_id=int(body.vendor_id),
        user_id=user_id,
        department_id=body.department_id,
        document_type=body.document_type,
        version_no=body.version_no,
        quantities=body.quantities,
    )
    from models import PurchaseOrderLine

    line_count = (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.po_id == header.po_id)
        .count()
    )

    # Same as UI PO create / sales-spike: best-effort WhatsApp notify (never fails create).
    vendor_name: Optional[str] = None
    try:
        vendor_row = (
            db.query(Vendor).filter(Vendor.vendor_id == int(body.vendor_id)).first()
        )
        if vendor_row is not None:
            vendor_name = vendor_row.company_name
    except Exception:
        vendor_name = None
    try:
        notify_po_created_whatsapp(
            po_number=header.po_number,
            po_id=int(header.po_id) if header.po_id is not None else None,
            vendor_name=vendor_name,
            line_count=line_count,
        )
    except Exception as notify_exc:
        print(f"[PO-WHATSAPP] unexpected notify error (ignored): {notify_exc}")

    return CreatePoFromSalesSpikesResponse(
        po_id=header.po_id,
        po_number=header.po_number,
        line_count=line_count,
        po_total_amount=header.po_total_amount,
        net_total_amount=header.net_total_amount,
        message=f"Purchase order {header.po_number} created with {line_count} line(s).",
        approval_status=header.approval_status,
        document_type=header.document_type,
        version_no=header.version_no,
        tenant_id=header.tenant_id,
        company_id=header.company_id,
    )


class WhatsAppGrnLineQty(BaseModel):
    lineNo: int
    qtyReceived: float
    poLineNo: Optional[int] = None
    description: Optional[str] = None
    batchNo: Optional[str] = None
    expiryDate: Optional[str] = None


class WhatsAppPostInvoiceRequest(BaseModel):
    po_number: str = Field(..., min_length=1)
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    invoice_no: Optional[str] = None
    grn_number: Optional[str] = None
    prepare_only: bool = False
    line_qtys: Optional[List[WhatsAppGrnLineQty]] = None


class WhatsAppConfirmInvoiceLineRequest(BaseModel):
    po_number: str = Field(..., min_length=1)
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    invoice_no: str = Field(..., min_length=1)
    document_type: str = "IN"
    version_no: int = 1
    line_no: int
    selected_po_line_no: int
    selected_item_id: Optional[int] = None
    selected_product_fingerprint: Optional[dict] = None


class WhatsAppDeleteInvoiceLineRequest(BaseModel):
    po_number: str = Field(..., min_length=1)
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    invoice_no: str = Field(..., min_length=1)
    document_type: str = "IN"
    version_no: int = 1
    line_no: int


@router.post("/procurement/whatsapp/preview-invoice-match")
async def preview_invoice_match_for_whatsapp_endpoint(
    body: WhatsAppPostInvoiceRequest,
):
    """
    Read-only Invoice Review summary (AUTO / REVIEW / Select match).
    Used right after WhatsApp invoice OCR upload — does not confirm lines.
    """
    tenant_id = int(body.tenant_id or _default_tenant_id())
    company_id = resolve_company_id(body.company_id or _default_company_id())
    return preview_invoice_match_for_whatsapp(
        po_number=body.po_number.strip(),
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=(body.invoice_no or "").strip() or None,
    )


@router.post("/procurement/whatsapp/approve-invoice")
async def approve_invoice_for_whatsapp_endpoint(
    body: WhatsAppPostInvoiceRequest,
    db: Session = Depends(get_db),
):
    """
    Same as UI Invoice Review Confirm + sidebar:
    auto-confirm suggested matches; return pending lines for manual PO pick.
    """
    tenant_id = int(body.tenant_id or _default_tenant_id())
    company_id = resolve_company_id(body.company_id or _default_company_id())
    return approve_invoice_for_whatsapp(
        db,
        po_number=body.po_number.strip(),
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=(body.invoice_no or "").strip() or None,
    )


@router.post("/procurement/whatsapp/confirm-invoice-line")
async def confirm_invoice_line_for_whatsapp_endpoint(
    body: WhatsAppConfirmInvoiceLineRequest,
    db: Session = Depends(get_db),
):
    """Same as UI match sidebar confirm for one invoice line."""
    tenant_id = int(body.tenant_id or _default_tenant_id())
    company_id = resolve_company_id(body.company_id or _default_company_id())
    return confirm_invoice_line_for_whatsapp(
        db,
        po_number=body.po_number.strip(),
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=body.invoice_no.strip(),
        document_type=(body.document_type or "IN").strip() or "IN",
        version_no=int(body.version_no or 1),
        line_no=int(body.line_no),
        selected_po_line_no=int(body.selected_po_line_no),
        selected_item_id=body.selected_item_id,
        selected_product_fingerprint=body.selected_product_fingerprint,
    )


@router.post("/procurement/whatsapp/delete-invoice-line")
async def delete_invoice_line_for_whatsapp_endpoint(
    body: WhatsAppDeleteInvoiceLineRequest,
):
    """Remove an unmatched invoice line (product not found on the PO)."""
    tenant_id = int(body.tenant_id or _default_tenant_id())
    company_id = resolve_company_id(body.company_id or _default_company_id())
    return delete_invoice_line_for_whatsapp(
        po_number=body.po_number.strip(),
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=body.invoice_no.strip(),
        document_type=(body.document_type or "IN").strip() or "IN",
        version_no=int(body.version_no or 1),
        line_no=int(body.line_no),
    )


@router.post("/procurement/whatsapp/prepare-grn")
async def prepare_grn_for_whatsapp_endpoint(
    body: WhatsAppPostInvoiceRequest,
):
    """
    Create/load draft GRN and return lines with suggested qty.
    WhatsApp asks the user for each item qty before applying.
    """
    tenant_id = int(body.tenant_id or _default_tenant_id())
    company_id = resolve_company_id(body.company_id or _default_company_id())
    return prepare_grn_for_whatsapp(
        po_number=body.po_number.strip(),
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=(body.invoice_no or "").strip() or None,
        grn_number=(body.grn_number or "").strip() or None,
    )


@router.post("/procurement/whatsapp/update-grn-qty")
async def update_grn_qty_for_whatsapp_endpoint(
    body: WhatsAppPostInvoiceRequest,
):
    """
    Apply user-entered GRN line quantities (UI save GRN qty),
    or prepare GRN lines when prepare_only / no line_qtys.
    """
    tenant_id = int(body.tenant_id or _default_tenant_id())
    company_id = resolve_company_id(body.company_id or _default_company_id())
    line_qtys = (
        [q.model_dump() for q in body.line_qtys] if body.line_qtys else None
    )
    if line_qtys:
        return apply_grn_qtys_for_whatsapp(
            po_number=body.po_number.strip(),
            tenant_id=tenant_id,
            company_id=company_id,
            line_qtys=line_qtys,
            invoice_no=(body.invoice_no or "").strip() or None,
            grn_number=(body.grn_number or "").strip() or None,
        )
    return update_grn_qty_for_whatsapp(
        po_number=body.po_number.strip(),
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=(body.invoice_no or "").strip() or None,
        grn_number=(body.grn_number or "").strip() or None,
        prepare_only=True if body.prepare_only or not line_qtys else False,
        line_qtys=line_qtys,
    )


@router.post("/procurement/whatsapp/approve-grn")
async def approve_grn_for_whatsapp_endpoint(
    body: WhatsAppPostInvoiceRequest,
):
    """Same as UI Confirm GRN: PATCH grn-header status=CONFIRMED."""
    tenant_id = int(body.tenant_id or _default_tenant_id())
    company_id = resolve_company_id(body.company_id or _default_company_id())
    return approve_grn_for_whatsapp(
        po_number=body.po_number.strip(),
        tenant_id=tenant_id,
        company_id=company_id,
        grn_number=(body.grn_number or "").strip() or None,
    )


@router.get("/procurement/whatsapp/three-way-match-queue")
async def three_way_match_queue_for_whatsapp(
    tenant_id: Optional[int] = Query(None, alias="tenantId"),
    company_id: Optional[int] = Query(None, alias="companyId"),
    vendor_id: Optional[int] = Query(None, alias="vendorId"),
    document_type: str = Query("IN", alias="documentType"),
    status_filter: Optional[str] = Query("DRAFT", alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    Same queue as UI Invoice Review:
    `GET /invoice/three-way-match-queue` (POs with invoice + GRN).
    """
    resolved_tenant = int(tenant_id or _default_tenant_id())
    resolved_company = resolve_company_id(company_id or _default_company_id())
    try:
        return list_three_way_match_queue(
            tenant_id=resolved_tenant,
            company_id=resolved_company,
            document_type=document_type or "IN",
            status=status_filter,
            vendor_id=vendor_id,
            page=page,
            limit=limit,
            db=db,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.get("/procurement/whatsapp/three-way-match/{po_number}")
async def three_way_match_detail_for_whatsapp(
    po_number: str,
    tenant_id: Optional[int] = Query(None, alias="tenantId"),
    company_id: Optional[int] = Query(None, alias="companyId"),
    db: Session = Depends(get_db),
):
    """
    Same match summary the UI builds on Invoice Review detail for a PO
    (amount/qty checks across PO, GRN, delivery note, invoice).
    """
    resolved_tenant = int(tenant_id or _default_tenant_id())
    resolved_company = resolve_company_id(company_id or _default_company_id())
    return get_three_way_match_detail(
        db,
        po_number=po_number,
        tenant_id=resolved_tenant,
        company_id=resolved_company,
    )
