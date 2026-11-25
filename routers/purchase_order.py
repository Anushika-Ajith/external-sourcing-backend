from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File, Form, Body
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from typing import List, Optional
from datetime import datetime, timezone
from pathlib import Path

from database import get_db
from models import PurchaseOrderHeader, PurchaseOrderLine, User, Vendor
from schemas.purchase_order import (
    PurchaseOrderHeaderCreate,
    PurchaseOrderHeaderUpdate,
    PurchaseOrderHeaderResponse,
    PurchaseOrderCompleteCreate,
    PurchaseOrderFullResponse,
    PurchaseOrderLineCreate,
    PurchaseOrderLineUpdate,
    PurchaseOrderLineResponse,
)
from schemas.purchase_order_upload import PurchaseOrderUploadResponse
from services.po_upload_service import process_po_upload
from services.whatsapp_notify_service import (
    notify_po_created_whatsapp,
    notify_po_handoff_whatsapp,
)
from services.po_line_embedding_sync import (
    create_embedding_for_po_line,
    create_embeddings_for_po_lines,
)
from services.po_line_fingerprint import attach_product_fingerprint_to_line_dict
from services.po_line_qty import apply_line_qty_update, apply_qty_defaults_on_create
from services.po_amounts import ensure_po_line_rates_and_totals, recalculate_po_totals
from services.po_pdf_service import generate_and_attach_po_pdf
from services.po_vendor_rate_enrich import enrich_po_lines_with_vendor_rates
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id
from services.po_delete_all_service import delete_all_purchase_orders
from services.vendor_phone_lookup import find_vendor_by_phone
from services.whatsapp_auth import require_whatsapp_api_key

router = APIRouter(
    prefix="/purchase-order",
    tags=["purchase-order"],
    responses={404: {"description": "Not found"}},
)


def _ensure_vendor_exists(db: Session, vendor_id: int) -> None:
    """purchase_order_header.vendor_id references vendors.vendor_id (fk_po_vendor)."""
    if db.query(Vendor.vendor_id).filter(Vendor.vendor_id == vendor_id).first() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid vendor_id: no vendor exists with vendor_id={vendor_id}",
        )


def _raise_po_header_integrity_error(e: IntegrityError) -> None:
    """Map FK violations to clearer messages (e.g. fk_po_vendor)."""
    msg = str(getattr(e, "orig", e))
    low = msg.lower()
    if "vendor" in low or "fk_po_vendor" in low or "vendors" in low:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid vendor_id: vendor must exist in vendors (fk_po_vendor).",
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Database constraint violation: {msg}",
    )


ALLOWED_PO_UPLOAD_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
}


@router.post("/upload", response_model=PurchaseOrderUploadResponse)
async def upload_purchase_order_document(
    file: UploadFile = File(...),
    tenant_id: int = Form(1),
    company_id: int = Form(3),
    department_id: int = Form(1),
    document_type: str = Form("PO"),
    version_no: int = Form(1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Vendor PO upload with SHA256 duplicate detection and business-hash revisions.

    po_number is assigned from Nest next-numbering (PURCHASE_ORDER), not from OCR.
    OCR-extracted buyer PO number is stored as document_number for reference/revisions.

    Decision matrix:
    - Same document hash → duplicate (OCR skipped)
    - New file + same business hash → attachment update only
    - New file + different business hash → new revision (history snapshot)
    - New PO → create revision 1 with next-numbering po_number
    """
    company_id = resolve_company_id(company_id)
    # Access control is via permissions API (frontend). Resolve vendor for upload payload.
    upload_vendor_id = current_user.vendor_id
    if upload_vendor_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="vendor_id is required on the user profile to upload a purchase order",
        )

    content_type = (file.content_type or "").strip().lower()
    if content_type not in ALLOWED_PO_UPLOAD_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {file.content_type}",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file upload",
        )

    vendor = db.query(Vendor).filter(Vendor.vendor_id == upload_vendor_id).first()
    vendor_name = vendor.company_name if vendor else None

    try:
        outcome = process_po_upload(
            db,
            file_bytes=file_bytes,
            filename=file.filename or "upload.pdf",
            content_type=content_type,
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=document_type,
            version_no=version_no,
            vendor_id=int(upload_vendor_id),
            user_id=int(current_user.user_id),
            vendor_name=vendor_name,
            department_id=department_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"PO upload failed: {exc}",
        ) from exc

    result = outcome.to_dict()
    # Notify WhatsApp only for real creates/updates — skip duplicate file uploads.
    action = str(result.get("action") or "").strip().lower()
    if action and action != "duplicate":
        try:
            notify_po_created_whatsapp(
                po_number=result.get("po_number"),
                po_id=result.get("po_id"),
                vendor_name=vendor_name,
            )
        except Exception as notify_exc:
            print(f"[PO-WHATSAPP] unexpected notify error (ignored): {notify_exc}")

    return PurchaseOrderUploadResponse(**result)


def _apply_po_role_scope(
    q,
    *,
    db: Session,
    current_user: User,
    vendor_id: Optional[int] = None,
):
    """
    Optional vendor_id filter only. Role-based scoping removed —
    access restrictions come from the permissions API.
    """
    _ = (db, current_user)
    if vendor_id is not None:
        return q.filter(PurchaseOrderHeader.vendor_id == vendor_id)
    return q


@router.get("", response_model=List[PurchaseOrderHeaderResponse])
async def list_purchase_order_headers(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    vendor_id: Optional[int] = Query(None),
    current_editor: Optional[str] = Query(
        None,
        description="Filter by current_editor (e.g. BUYER, VENDOR)",
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    q = db.query(PurchaseOrderHeader)
    if tenant_id is not None:
        q = q.filter(PurchaseOrderHeader.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(PurchaseOrderHeader.company_id == company_id)
    if document_type:
        q = q.filter(PurchaseOrderHeader.document_type == document_type)
    if status:
        q = q.filter(PurchaseOrderHeader.status == status)

    q = _apply_po_role_scope(
        q, db=db, current_user=current_user, vendor_id=vendor_id
    )
    if q is None:
        return []

    if current_editor is not None and current_editor.strip():
        q = q.filter(PurchaseOrderHeader.current_editor == current_editor.strip())
    return q.order_by(PurchaseOrderHeader.po_date.desc()).offset(skip).limit(limit).all()


class WhatsAppPoLineSummary(BaseModel):
    line_no: int
    item_name: str
    item_no: Optional[str] = None
    qty: Optional[float] = None
    uom: Optional[str] = None
    unit_price: Optional[float] = None
    line_amount: Optional[float] = None


class WhatsAppPoListItem(BaseModel):
    po_id: int
    po_number: str
    po_date: Optional[str] = None
    vendor_name: Optional[str] = None
    status: Optional[str] = None
    approval_status: Optional[str] = None
    currency_code: Optional[str] = None
    net_total_amount: Optional[str] = None
    po_total_amount: Optional[str] = None
    line_count: int = 0
    lines: List[WhatsAppPoLineSummary] = Field(default_factory=list)


def _whatsapp_po_line_summary(line: PurchaseOrderLine) -> WhatsAppPoLineSummary:
    qty_raw = line.qty_ordered if line.qty_ordered is not None else line.buyer_qty
    qty = float(qty_raw) if qty_raw is not None else None
    price_raw = (
        line.unit_price
        if line.unit_price is not None
        else line.po_item_rate
        if line.po_item_rate is not None
        else line.rate_contract_item_rate
    )
    unit_price = float(price_raw) if price_raw is not None else None
    amount_raw = line.total_amount
    if amount_raw is None and qty is not None and unit_price is not None:
        amount_raw = qty * unit_price
    item_name = (
        (line.description or "").strip()
        or (line.item_no or "").strip()
        or f"Line {line.line_no}"
    )
    return WhatsAppPoLineSummary(
        line_no=int(line.line_no),
        item_name=item_name,
        item_no=(line.item_no or None),
        qty=qty,
        uom=(line.uom or None),
        unit_price=unit_price,
        line_amount=float(amount_raw) if amount_raw is not None else None,
    )


@router.get(
    "/whatsapp/by-phone",
    response_model=List[WhatsAppPoListItem],
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def list_purchase_orders_for_whatsapp_phone(
    mobile: str = Query(..., description="WhatsApp sender phone / mobile"),
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    scope: str = Query(
        "auto",
        description=(
            "auto = vendor POs if phone matches a vendor, else all in scope; "
            "all = all POs in tenant/company (buyer); "
            "vendor = only that vendor's POs"
        ),
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    WhatsApp MCP path for listing POs by sender phone.
    Returns a compact summary (header + line items) for chat display.
    Auth: X-WhatsApp-API-Key only (no user JWT / buyer role check).
    """
    company_id = resolve_company_id(company_id)
    q = db.query(PurchaseOrderHeader)
    if tenant_id is not None:
        q = q.filter(PurchaseOrderHeader.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(PurchaseOrderHeader.company_id == company_id)

    scope_norm = (scope or "auto").strip().lower()
    vendor = find_vendor_by_phone(db, mobile)
    if scope_norm == "all":
        pass
    elif scope_norm == "vendor":
        if not vendor:
            return []
        q = q.filter(PurchaseOrderHeader.vendor_id == int(vendor.vendor_id))
    elif vendor:
        # auto: phone tied to a vendor → vendor-scoped list
        q = q.filter(PurchaseOrderHeader.vendor_id == int(vendor.vendor_id))

    headers = (
        q.order_by(PurchaseOrderHeader.po_date.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    if not headers:
        return []

    po_ids = [int(h.po_id) for h in headers if h.po_id is not None]
    lines_by_po: dict[int, list[PurchaseOrderLine]] = {po_id: [] for po_id in po_ids}
    if po_ids:
        line_rows = (
            db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.po_id.in_(po_ids))
            .order_by(PurchaseOrderLine.po_id, PurchaseOrderLine.line_no)
            .all()
        )
        for line in line_rows:
            if line.po_id is None:
                continue
            lines_by_po.setdefault(int(line.po_id), []).append(line)

    out: List[WhatsAppPoListItem] = []
    for header in headers:
        po_id = int(header.po_id)
        line_summaries = [
            _whatsapp_po_line_summary(line) for line in lines_by_po.get(po_id, [])
        ]
        po_date = header.po_date.isoformat() if header.po_date is not None else None
        out.append(
            WhatsAppPoListItem(
                po_id=po_id,
                po_number=str(header.po_number or ""),
                po_date=po_date,
                vendor_name=header.vendor_name_snapshot,
                status=header.status,
                approval_status=header.approval_status,
                currency_code=header.currency_code,
                net_total_amount=(
                    str(header.net_total_amount)
                    if header.net_total_amount is not None
                    else None
                ),
                po_total_amount=(
                    str(header.po_total_amount)
                    if header.po_total_amount is not None
                    else None
                ),
                line_count=len(line_summaries),
                lines=line_summaries,
            )
        )
    return out


@router.get("/pending", response_model=List[PurchaseOrderHeaderResponse])
async def list_pending_purchase_orders(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    document_type: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List pending POs (vendor_acknowledged is false/null).

    Role scoping is derived from the authenticated user:
    - buyer/admin → all pending POs in scope
    - vendor → only pending POs for that vendor
    """
    company_id = resolve_company_id(company_id)
    q = db.query(PurchaseOrderHeader).filter(
        (PurchaseOrderHeader.vendor_acknowledged.is_(False))
        | (PurchaseOrderHeader.vendor_acknowledged.is_(None))
    )
    if tenant_id is not None:
        q = q.filter(PurchaseOrderHeader.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(PurchaseOrderHeader.company_id == company_id)
    if document_type:
        q = q.filter(PurchaseOrderHeader.document_type == document_type)

    q = _apply_po_role_scope(q, db=db, current_user=current_user)
    if q is None:
        return []

    return (
        q.order_by(PurchaseOrderHeader.po_date.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.delete("")
async def delete_all_purchase_order_headers(
    tenant_id: Optional[int] = Query(None),
    company_id: Optional[int] = Query(None),
    vendor_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete all purchase orders for tenant/company scope and related GRN/invoice rows."""
    try:
        return delete_all_purchase_orders(
            db,
            tenant_id=tenant_id,
            company_id=company_id,
            vendor_id=vendor_id,
            current_user=current_user,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting purchase orders: {exc}",
        ) from exc


@router.get(
    "/whatsapp/by-po-id/{po_id}",
    response_model=PurchaseOrderHeaderResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def get_purchase_order_header_by_po_id_for_whatsapp(
    po_id: int,
    db: Session = Depends(get_db),
):
    """
    Same as UI `GET /purchase-order/by-po-id/{po_id}` (approval_status for invoice gate).
    Auth: X-WhatsApp-API-Key.
    """
    row = db.query(PurchaseOrderHeader).filter(PurchaseOrderHeader.po_id == po_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")
    if row.po_total_amount is None or float(row.po_total_amount or 0) == 0:
        ensure_po_line_rates_and_totals(db, row, commit=True)
    return row


@router.post(
    "/whatsapp/by-po-id/{po_id}/approve",
    response_model=PurchaseOrderHeaderResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def approve_purchase_order_for_whatsapp(
    po_id: int,
    remarks: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Same finalize as UI PO approve:
    `PUT /purchase-order/{tenant}/{company}/{docType}/{poNo}/{version}`
    with approval_status=approved (+ approved_at / approved_by).
    """
    row = db.query(PurchaseOrderHeader).filter(PurchaseOrderHeader.po_id == po_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")

    current = (row.approval_status or "").strip().lower()
    if current == "approved":
        return row
    if current == "rejected":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This purchase order was rejected and cannot be approved as-is.",
        )

    now = datetime.now(timezone.utc)
    bot_user = db.query(User).order_by(User.user_id.asc()).first()
    bot_user_id = int(bot_user.user_id) if bot_user and bot_user.user_id is not None else 1

    row.approval_status = "approved"
    row.approved_at = now
    row.approved_by = bot_user_id
    if remarks and remarks.strip():
        row.remarks = remarks.strip()
    row.updated_by = bot_user_id
    row.updated_at = now

    try:
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as e:
        db.rollback()
        _raise_po_header_integrity_error(e)


def _whatsapp_bot_user_id(db: Session) -> int:
    bot_user = db.query(User).order_by(User.user_id.asc()).first()
    return int(bot_user.user_id) if bot_user and bot_user.user_id is not None else 1


def _notify_handoff_for_po(db: Session, row: PurchaseOrderHeader, handoff_to: str) -> None:
    vendor_name: Optional[str] = None
    vendor_mobile: Optional[str] = None
    if row.vendor_id is not None:
        vendor = (
            db.query(Vendor)
            .filter(Vendor.vendor_id == int(row.vendor_id))
            .first()
        )
        if vendor:
            vendor_name = vendor.company_name
            vendor_mobile = vendor.mobile
    try:
        notify_po_handoff_whatsapp(
            handoff_to=handoff_to,
            po_number=row.po_number,
            po_id=int(row.po_id) if row.po_id is not None else None,
            vendor_name=vendor_name,
            vendor_mobile=vendor_mobile,
        )
    except Exception as notify_exc:
        print(f"[PO-HANDOFF-WHATSAPP] unexpected notify error (ignored): {notify_exc}")


@router.post(
    "/whatsapp/by-po-id/{po_id}/reject",
    response_model=PurchaseOrderHeaderResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def reject_purchase_order_for_whatsapp(
    po_id: int,
    rejection_reason: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Same finalize as UI PO reject:
    `PUT /purchase-order/...` with approval_status=rejected + rejection_reason.
    """
    row = db.query(PurchaseOrderHeader).filter(PurchaseOrderHeader.po_id == po_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")

    current = (row.approval_status or "").strip().lower()
    if current == "rejected":
        return row
    if current == "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This purchase order is already approved and cannot be rejected.",
        )

    now = datetime.now(timezone.utc)
    bot_user_id = _whatsapp_bot_user_id(db)
    reason = (rejection_reason or "").strip() or "Rejected via WhatsApp"

    row.approval_status = "rejected"
    row.rejected_at = now
    row.rejected_by = bot_user_id
    row.rejection_reason = reason[:500]
    row.updated_by = bot_user_id
    row.updated_at = now

    try:
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as e:
        db.rollback()
        _raise_po_header_integrity_error(e)


@router.post(
    "/whatsapp/by-po-id/{po_id}/handoff",
    response_model=PurchaseOrderHeaderResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def handoff_purchase_order_for_whatsapp(
    po_id: int,
    current_editor: str = Form(..., description="BUYER or VENDOR"),
    db: Session = Depends(get_db),
):
    """
    Same as UI Submit to Vendor / Buyer:
    `PUT /purchase-order/...` with current_editor (+ WhatsApp handoff notify).
    """
    row = db.query(PurchaseOrderHeader).filter(PurchaseOrderHeader.po_id == po_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")

    editor = (current_editor or "").strip().upper()
    if editor not in ("BUYER", "VENDOR"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="current_editor must be BUYER or VENDOR",
        )

    previous = (getattr(row, "current_editor", None) or "BUYER").strip().upper()
    bot_user_id = _whatsapp_bot_user_id(db)
    row.current_editor = editor
    row.updated_by = bot_user_id
    row.updated_at = datetime.now(timezone.utc)

    try:
        db.commit()
        db.refresh(row)
        if editor != previous:
            _notify_handoff_for_po(db, row, editor)
        return row
    except IntegrityError as e:
        db.rollback()
        _raise_po_header_integrity_error(e)


@router.get(
    "/whatsapp/by-po-id/{po_id}/lines",
    response_model=List[PurchaseOrderLineResponse],
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def list_purchase_order_lines_for_whatsapp(
    po_id: int,
    db: Session = Depends(get_db),
):
    """Same as UI list PO lines for a PO id."""
    header = db.query(PurchaseOrderHeader).filter(PurchaseOrderHeader.po_id == po_id).first()
    if not header:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")
    if header.po_total_amount is None or float(header.po_total_amount or 0) == 0:
        ensure_po_line_rates_and_totals(db, header, commit=True)
    return (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.po_id == po_id)
        .order_by(PurchaseOrderLine.line_no)
        .all()
    )


class WhatsAppPoLineUpdateBody(BaseModel):
    qty_ordered: Optional[float] = None
    buyer_qty: Optional[float] = None
    supplier_qty: Optional[float] = None
    unit_price: Optional[float] = None
    line_status: Optional[str] = Field(default="DRAFT")


@router.put(
    "/whatsapp/by-po-id/{po_id}/lines/{line_no}",
    response_model=PurchaseOrderLineResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def update_purchase_order_line_for_whatsapp(
    po_id: int,
    line_no: int,
    body: WhatsAppPoLineUpdateBody = Body(...),
    db: Session = Depends(get_db),
):
    """
    Same as UI line save:
    `PUT /purchase-order/lines/.../{line_no}` with buyer_qty/qty_ordered/unit_price.
    """
    header = db.query(PurchaseOrderHeader).filter(PurchaseOrderHeader.po_id == po_id).first()
    if not header:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")

    row = (
        db.query(PurchaseOrderLine)
        .filter(
            PurchaseOrderLine.po_id == po_id,
            PurchaseOrderLine.line_no == line_no,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO line not found")

    update_data = body.model_dump(exclude_unset=True)
    # WhatsApp buyer edits: map qty_ordered into buyer_qty path when only qty given.
    if (
        update_data.get("qty_ordered") is not None
        and update_data.get("buyer_qty") is None
        and update_data.get("supplier_qty") is None
    ):
        update_data["buyer_qty"] = update_data["qty_ordered"]

    update_data = apply_line_qty_update(
        row,
        update_data,
        current_editor=getattr(header, "current_editor", None) or "BUYER",
    )

    for key, value in update_data.items():
        if hasattr(row, key):
            setattr(row, key, value)

    bot_user_id = _whatsapp_bot_user_id(db)
    row.updated_by = bot_user_id
    row.updated_at = datetime.now(timezone.utc)
    if body.line_status:
        row.line_status = body.line_status

    try:
        db.flush()
        all_lines = (
            db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.po_id == header.po_id)
            .order_by(PurchaseOrderLine.line_no)
            .all()
        )
        recalculate_po_totals(header, all_lines)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {getattr(e, 'orig', e)}",
        )


@router.get("/by-po-id/{po_id}", response_model=PurchaseOrderHeaderResponse)
async def get_purchase_order_header_by_po_id(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(PurchaseOrderHeader).filter(PurchaseOrderHeader.po_id == po_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")
    if row.po_total_amount is None or float(row.po_total_amount or 0) == 0:
        ensure_po_line_rates_and_totals(db, row, commit=True)
    return row


@router.get("/by-po-id/{po_id}/full", response_model=PurchaseOrderFullResponse)
async def get_purchase_order_full_by_po_id(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get purchase order header and all lines by numeric po_id (detail page)."""
    header = (
        db.query(PurchaseOrderHeader)
        .options(joinedload(PurchaseOrderHeader.po_lines))
        .filter(PurchaseOrderHeader.po_id == po_id)
        .first()
    )
    if not header:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")
    if header.po_total_amount is None or float(header.po_total_amount or 0) == 0:
        ensure_po_line_rates_and_totals(db, header, commit=True)

    hdr_payload = PurchaseOrderHeaderResponse.model_validate(header).model_dump()
    lines_sorted = sorted(header.po_lines, key=lambda r: r.line_no)
    line_payloads = [PurchaseOrderLineResponse.model_validate(l) for l in lines_sorted]
    return PurchaseOrderFullResponse(**hdr_payload, lines=line_payloads)


def _resolve_po_attachment_file(attachment_path: str) -> Path:
    raw = (attachment_path or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No uploaded document for this purchase order",
        )
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / raw
    path = path.resolve()
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Uploaded document file is missing on the server",
        )
    return path


def _media_type_for_attachment(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix in (".png",):
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    return "application/octet-stream"


@router.get("/by-po-id/{po_id}/attachment")
async def download_purchase_order_attachment(
    po_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Download the PO document (system-generated or uploaded PDF/image)."""
    row = db.query(PurchaseOrderHeader).filter(PurchaseOrderHeader.po_id == po_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")

    file_path = _resolve_po_attachment_file(getattr(row, "attachment_path", None) or "")
    return FileResponse(
        path=str(file_path),
        media_type=_media_type_for_attachment(file_path),
        filename=file_path.name,
    )


@router.get(
    "/full/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}",
    response_model=PurchaseOrderFullResponse,
)
async def get_purchase_order_full(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get purchase order header and all lines in a single response.
    """
    company_id = resolve_company_id(company_id)
    header = (
        db.query(PurchaseOrderHeader)
        .options(joinedload(PurchaseOrderHeader.po_lines))
        .filter(
            PurchaseOrderHeader.tenant_id == tenant_id,
            PurchaseOrderHeader.company_id == company_id,
            PurchaseOrderHeader.document_type == document_type,
            PurchaseOrderHeader.po_number == po_number,
            PurchaseOrderHeader.version_no == version_no,
        )
        .first()
    )
    if not header:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PO header not found",
        )

    hdr_payload = PurchaseOrderHeaderResponse.model_validate(header).model_dump()
    lines_sorted = sorted(header.po_lines, key=lambda r: r.line_no)
    line_payloads = [PurchaseOrderLineResponse.model_validate(l) for l in lines_sorted]
    return PurchaseOrderFullResponse(**hdr_payload, lines=line_payloads)


@router.get(
    "/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}",
    response_model=PurchaseOrderHeaderResponse,
)
async def get_purchase_order_header(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    row = db.query(PurchaseOrderHeader).filter(
        PurchaseOrderHeader.tenant_id == tenant_id,
        PurchaseOrderHeader.company_id == company_id,
        PurchaseOrderHeader.document_type == document_type,
        PurchaseOrderHeader.po_number == po_number,
        PurchaseOrderHeader.version_no == version_no,
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")
    return row


@router.post("", response_model=PurchaseOrderHeaderResponse, status_code=status.HTTP_201_CREATED)
async def create_purchase_order_header(
    body: PurchaseOrderHeaderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = body.model_dump()
    data["company_id"] = resolve_company_id(data.get("company_id"))
    if data.get("created_by") is None:
        data["created_by"] = current_user.user_id
    if not (data.get("approval_status") or "").strip():
        data["approval_status"] = "DRAFT"

    existing = db.query(PurchaseOrderHeader).filter(
        PurchaseOrderHeader.tenant_id == data["tenant_id"],
        PurchaseOrderHeader.company_id == data["company_id"],
        PurchaseOrderHeader.document_type == data["document_type"],
        PurchaseOrderHeader.po_number == data["po_number"],
        PurchaseOrderHeader.version_no == data["version_no"],
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Purchase order header with this composite key already exists",
        )

    _ensure_vendor_exists(db, data["vendor_id"])

    try:
        header = PurchaseOrderHeader(**data)
        db.add(header)
        db.commit()
        db.refresh(header)
        return header
    except IntegrityError as e:
        db.rollback()
        _raise_po_header_integrity_error(e)


@router.post("/complete", response_model=PurchaseOrderFullResponse, status_code=status.HTTP_201_CREATED)
async def create_purchase_order_complete(
    body: PurchaseOrderCompleteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Create a purchase order header and all lines in a single transaction.
    """
    header_created_by = body.created_by if body.created_by is not None else current_user.user_id
    body.company_id = resolve_company_id(body.company_id)

    existing = db.query(PurchaseOrderHeader).filter(
        PurchaseOrderHeader.tenant_id == body.tenant_id,
        PurchaseOrderHeader.company_id == body.company_id,
        PurchaseOrderHeader.document_type == body.document_type,
        PurchaseOrderHeader.po_number == body.po_number,
        PurchaseOrderHeader.version_no == body.version_no,
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Purchase order header with this composite key already exists",
        )

    _ensure_vendor_exists(db, body.vendor_id)

    line_nos = [ln.line_no for ln in body.lines]
    if len(line_nos) != len(set(line_nos)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Duplicate line_no values in lines payload",
        )

    header_dict = body.model_dump(exclude={"lines", "created_by"}, exclude_none=False)
    header_dict["created_by"] = header_created_by
    if not (header_dict.get("approval_status") or "").strip():
        header_dict["approval_status"] = "DRAFT"

    try:
        header = PurchaseOrderHeader(**header_dict)
        db.add(header)
        db.flush()

        po_id = header.po_id
        keys = {
            "tenant_id": body.tenant_id,
            "company_id": body.company_id,
            "document_type": body.document_type,
            "po_number": body.po_number,
            "version_no": body.version_no,
            "po_id": po_id,
        }
        raw_lines = [ln.model_dump(exclude_none=False) for ln in body.lines]
        enriched_lines, _ = enrich_po_lines_with_vendor_rates(
            db,
            vendor_id=body.vendor_id,
            lines=raw_lines,
        )
        for line in enriched_lines:
            line_dict = dict(line)
            line_created = line_dict.pop("created_by", None)
            line_dict.update(keys)
            line_dict["created_by"] = (
                line_created if line_created is not None else header_created_by
            )
            apply_qty_defaults_on_create(line_dict)
            attach_product_fingerprint_to_line_dict(
                db, line_dict, vendor_id=body.vendor_id
            )
            db.add(PurchaseOrderLine(**line_dict))

        db.flush()
        created_lines = (
            db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.po_id == po_id)
            .order_by(PurchaseOrderLine.line_no)
            .all()
        )
        recalculate_po_totals(header, created_lines)
        create_embeddings_for_po_lines(db, header, created_lines)
        generate_and_attach_po_pdf(header, created_lines)

        db.commit()
    except IntegrityError as e:
        db.rollback()
        _raise_po_header_integrity_error(e)

    header = (
        db.query(PurchaseOrderHeader)
        .options(joinedload(PurchaseOrderHeader.po_lines))
        .filter(
            PurchaseOrderHeader.tenant_id == body.tenant_id,
            PurchaseOrderHeader.company_id == body.company_id,
            PurchaseOrderHeader.document_type == body.document_type,
            PurchaseOrderHeader.po_number == body.po_number,
            PurchaseOrderHeader.version_no == body.version_no,
        )
        .first()
    )

    hdr_payload = PurchaseOrderHeaderResponse.model_validate(header).model_dump()
    lines_sorted = sorted(header.po_lines, key=lambda r: r.line_no)
    line_payloads = [PurchaseOrderLineResponse.model_validate(l) for l in lines_sorted]
    return PurchaseOrderFullResponse(**hdr_payload, lines=line_payloads)


@router.put(
    "/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}",
    response_model=PurchaseOrderHeaderResponse,
)
async def update_purchase_order_header(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    body: PurchaseOrderHeaderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    row = db.query(PurchaseOrderHeader).filter(
        PurchaseOrderHeader.tenant_id == tenant_id,
        PurchaseOrderHeader.company_id == company_id,
        PurchaseOrderHeader.document_type == document_type,
        PurchaseOrderHeader.po_number == po_number,
        PurchaseOrderHeader.version_no == version_no,
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")

    previous_editor = (getattr(row, "current_editor", None) or "BUYER").strip().upper()
    update_data = body.model_dump(exclude_unset=True)
    if "vendor_id" in update_data and update_data["vendor_id"] is not None:
        _ensure_vendor_exists(db, update_data["vendor_id"])

    for key, value in update_data.items():
        if key == "updated_by":
            continue
        if hasattr(row, key):
            setattr(row, key, value)

    new_editor_raw = update_data.get("current_editor")
    editor_changed = (
        new_editor_raw is not None
        and str(new_editor_raw).strip().upper() != previous_editor
    )
    handoff_to = (
        str(new_editor_raw).strip().upper() if editor_changed else None
    )

    if body.updated_by is not None:
        row.updated_by = body.updated_by
    else:
        row.updated_by = current_user.user_id
    row.updated_at = datetime.now(timezone.utc)

    try:
        db.commit()
        db.refresh(row)

        if handoff_to in ("BUYER", "VENDOR"):
            vendor_name: Optional[str] = None
            vendor_mobile: Optional[str] = None
            if row.vendor_id is not None:
                vendor = (
                    db.query(Vendor)
                    .filter(Vendor.vendor_id == int(row.vendor_id))
                    .first()
                )
                if vendor:
                    vendor_name = vendor.company_name
                    vendor_mobile = vendor.mobile

            try:
                notify_po_handoff_whatsapp(
                    handoff_to=handoff_to,
                    po_number=row.po_number,
                    po_id=int(row.po_id) if row.po_id is not None else None,
                    vendor_name=vendor_name,
                    vendor_mobile=vendor_mobile,
                )
            except Exception as notify_exc:
                print(
                    f"[PO-HANDOFF-WHATSAPP] unexpected notify error (ignored): {notify_exc}"
                )

        return row
    except IntegrityError as e:
        db.rollback()
        _raise_po_header_integrity_error(e)


@router.delete(
    "/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_purchase_order_header(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    row = db.query(PurchaseOrderHeader).filter(
        PurchaseOrderHeader.tenant_id == tenant_id,
        PurchaseOrderHeader.company_id == company_id,
        PurchaseOrderHeader.document_type == document_type,
        PurchaseOrderHeader.po_number == po_number,
        PurchaseOrderHeader.version_no == version_no,
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")
    try:
        db.delete(row)
        db.commit()
        return None
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting PO header: {e}",
        )


# --- Purchase order lines ---


def _get_po_header(
    db: Session,
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
) -> PurchaseOrderHeader:
    company_id = resolve_company_id(company_id)
    row = db.query(PurchaseOrderHeader).filter(
        PurchaseOrderHeader.tenant_id == tenant_id,
        PurchaseOrderHeader.company_id == company_id,
        PurchaseOrderHeader.document_type == document_type,
        PurchaseOrderHeader.po_number == po_number,
        PurchaseOrderHeader.version_no == version_no,
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO header not found")
    return row


@router.get("/lines/by-line-id/{line_id}", response_model=PurchaseOrderLineResponse)
async def get_purchase_order_line_by_line_id(
    line_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(PurchaseOrderLine).filter(PurchaseOrderLine.line_id == line_id).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO line not found")
    return row


@router.get(
    "/lines/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}",
    response_model=List[PurchaseOrderLineResponse],
)
async def list_purchase_order_lines(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    header = _get_po_header(db, tenant_id, company_id, document_type, po_number, version_no)
    if header.po_total_amount is None or float(header.po_total_amount or 0) == 0:
        ensure_po_line_rates_and_totals(db, header, commit=True)
    return (
        db.query(PurchaseOrderLine)
        .filter(
            PurchaseOrderLine.tenant_id == tenant_id,
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrderLine.document_type == document_type,
            PurchaseOrderLine.po_number == po_number,
            PurchaseOrderLine.version_no == version_no,
        )
        .order_by(PurchaseOrderLine.line_no)
        .all()
    )


@router.get(
    "/lines/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}/{line_no}",
    response_model=PurchaseOrderLineResponse,
)
async def get_purchase_order_line(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    line_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    row = db.query(PurchaseOrderLine).filter(
        PurchaseOrderLine.tenant_id == tenant_id,
        PurchaseOrderLine.company_id == company_id,
        PurchaseOrderLine.document_type == document_type,
        PurchaseOrderLine.po_number == po_number,
        PurchaseOrderLine.version_no == version_no,
        PurchaseOrderLine.line_no == line_no,
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO line not found")
    return row


@router.post(
    "/lines/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}",
    response_model=PurchaseOrderLineResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_purchase_order_line(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    body: PurchaseOrderLineCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    header = _get_po_header(db, tenant_id, company_id, document_type, po_number, version_no)

    existing = db.query(PurchaseOrderLine).filter(
        PurchaseOrderLine.tenant_id == tenant_id,
        PurchaseOrderLine.company_id == company_id,
        PurchaseOrderLine.document_type == document_type,
        PurchaseOrderLine.po_number == po_number,
        PurchaseOrderLine.version_no == version_no,
        PurchaseOrderLine.line_no == body.line_no,
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PO line with this line_no already exists",
        )

    data = body.model_dump()
    if data.get("created_by") is None:
        data["created_by"] = current_user.user_id

    req_po_id = data.pop("po_id", None)
    if req_po_id is None:
        data["po_id"] = header.po_id
    elif req_po_id != header.po_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="po_id does not match purchase order header",
        )
    else:
        data["po_id"] = req_po_id

    data.update(
        {
            "tenant_id": tenant_id,
            "company_id": company_id,
            "document_type": document_type,
            "po_number": po_number,
            "version_no": version_no,
        }
    )
    apply_qty_defaults_on_create(data)
    attach_product_fingerprint_to_line_dict(
        db, data, vendor_id=header.vendor_id
    )

    try:
        line = PurchaseOrderLine(**data)
        db.add(line)
        db.flush()
        create_embedding_for_po_line(db, header, line)
        all_lines = (
            db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.po_id == header.po_id)
            .order_by(PurchaseOrderLine.line_no)
            .all()
        )
        recalculate_po_totals(header, all_lines)
        db.commit()
        db.refresh(line)
        return line
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {getattr(e, 'orig', e)}",
        )


@router.put(
    "/lines/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}/{line_no}",
    response_model=PurchaseOrderLineResponse,
)
async def update_purchase_order_line(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    line_no: int,
    body: PurchaseOrderLineUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    row = db.query(PurchaseOrderLine).filter(
        PurchaseOrderLine.tenant_id == tenant_id,
        PurchaseOrderLine.company_id == company_id,
        PurchaseOrderLine.document_type == document_type,
        PurchaseOrderLine.po_number == po_number,
        PurchaseOrderLine.version_no == version_no,
        PurchaseOrderLine.line_no == line_no,
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO line not found")

    header = _get_po_header(db, tenant_id, company_id, document_type, po_number, version_no)

    update_data = body.model_dump(exclude_unset=True)
    if "po_id" in update_data and update_data["po_id"] is not None and update_data["po_id"] != header.po_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="po_id does not match purchase order header",
        )

    update_data = apply_line_qty_update(
        row,
        update_data,
        current_editor=getattr(header, "current_editor", None),
    )

    for key, value in update_data.items():
        if key == "updated_by":
            continue
        if hasattr(row, key):
            setattr(row, key, value)

    if body.updated_by is not None:
        row.updated_by = body.updated_by
    else:
        row.updated_by = current_user.user_id
    row.updated_at = datetime.now(timezone.utc)

    try:
        db.flush()
        all_lines = (
            db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.po_id == header.po_id)
            .order_by(PurchaseOrderLine.line_no)
            .all()
        )
        recalculate_po_totals(header, all_lines)
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {getattr(e, 'orig', e)}",
        )


@router.delete(
    "/lines/{tenant_id}/{company_id}/{document_type}/{po_number}/{version_no}/{line_no}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_purchase_order_line(
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
    line_no: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    company_id = resolve_company_id(company_id)
    header = _get_po_header(db, tenant_id, company_id, document_type, po_number, version_no)
    row = db.query(PurchaseOrderLine).filter(
        PurchaseOrderLine.tenant_id == tenant_id,
        PurchaseOrderLine.company_id == company_id,
        PurchaseOrderLine.document_type == document_type,
        PurchaseOrderLine.po_number == po_number,
        PurchaseOrderLine.version_no == version_no,
        PurchaseOrderLine.line_no == line_no,
    ).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PO line not found")
    try:
        db.delete(row)
        db.flush()
        remaining_lines = (
            db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.po_id == header.po_id)
            .order_by(PurchaseOrderLine.line_no)
            .all()
        )
        recalculate_po_totals(header, remaining_lines)
        db.commit()
        return None
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting PO line: {e}",
        )
