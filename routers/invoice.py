"""Invoice routes (ERP-backed delete + pending list + attachment download)."""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import User, Vendor
from schemas.invoice_delete import InvoiceDeleteBatchResponse, InvoiceDeleteResponse
from schemas.invoice_list import InvoiceHeaderOut, InvoicePendingListResponse
from schemas.three_way_match_queue import (
    ThreeWayMatchPoQueueItem,
    ThreeWayMatchQueueResponse,
)
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id
from services.invoice_attachment_service import (
    media_type_for_attachment,
    resolve_invoice_attachment_file,
)
from services.invoice_delete_service import delete_invoice_with_linked_grn
from services.invoice_line_match_service import list_erp_invoice_lines_for_header
from services.invoice_list_service import (
    PENDING_INVOICE_STATUS,
    list_invoice_headers,
    normalize_invoice_line,
)
from services.three_way_match_queue_service import list_three_way_match_queue
from services.whatsapp_auth import require_whatsapp_api_key
from services.whatsapp_list_scope import resolve_whatsapp_vendor_scope

router = APIRouter(
    prefix="/invoice",
    tags=["invoice"],
    responses={404: {"description": "Not found"}},
)


class WhatsAppInvoiceLineSummary(BaseModel):
    line_no: int
    item_name: str
    item_no: Optional[str] = None
    qty: Optional[float] = None
    uom: Optional[str] = None
    unit_price: Optional[float] = None
    line_amount: Optional[float] = None


class WhatsAppInvoiceListItem(BaseModel):
    invoice_no: str
    version_no: int = 1
    document_type: str = "IN"
    po_number: Optional[str] = None
    vendor_id: Optional[int] = None
    vendor_name: Optional[str] = None
    invoice_date: Optional[str] = None
    status: Optional[str] = None
    currency_code: Optional[str] = None
    invoice_amount: Optional[str] = None
    line_count: int = 0
    lines: List[WhatsAppInvoiceLineSummary] = Field(default_factory=list)


def _vendor_names_by_id(db: Session, vendor_ids: List[int]) -> Dict[int, str]:
    ids = sorted({int(v) for v in vendor_ids if v})
    if not ids:
        return {}
    rows = db.query(Vendor).filter(Vendor.vendor_id.in_(ids)).all()
    out: Dict[int, str] = {}
    for row in rows:
        if row.vendor_id is None:
            continue
        name = (row.company_name or "").strip() or f"Vendor {row.vendor_id}"
        out[int(row.vendor_id)] = name
    return out


@router.get(
    "/whatsapp/by-phone",
    response_model=List[WhatsAppInvoiceListItem],
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def list_invoices_for_whatsapp_phone(
    mobile: str = Query(..., description="WhatsApp sender phone / mobile"),
    tenant_id: Optional[int] = Query(1),
    company_id: Optional[int] = Query(3),
    document_type: str = Query("IN"),
    status_filter: Optional[str] = Query(
        None,
        alias="status",
        description="Optional invoice status filter (omit for all)",
    ),
    scope: str = Query(
        "all",
        description=(
            "all = all invoices in tenant/company (buyer default); "
            "auto = vendor invoices only if phone matches an *approved* vendor; "
            "vendor = only that vendor's invoices. "
            "BUYER role always lists all."
        ),
    ),
    role: Optional[str] = Query(
        None,
        description="App role (BUYER/VENDOR). BUYER → all invoices. "
        "If omitted, resolved via Foundation by phone.",
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """
    WhatsApp list of invoices via existing ERP ``list_invoice_headers`` +
    ``list_erp_invoice_lines_for_header``. Compact chat summary (no raw dump).
    """
    company_id = resolve_company_id(company_id)
    tenant = int(tenant_id or 1)
    vendor_id, scope_reason = resolve_whatsapp_vendor_scope(
        db, mobile, scope, role=role
    )
    if scope_reason == "vendor_required_missing":
        return []

    try:
        result = list_invoice_headers(
            tenant_id=tenant,
            company_id=company_id,
            document_type=document_type or "IN",
            status=status_filter,
            vendor_id=vendor_id,
            page=page,
            limit=limit,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    items_raw: List[Dict[str, Any]] = list(result.get("items") or [])
    vendor_names = _vendor_names_by_id(
        db,
        [int(r.get("vendor_id") or 0) for r in items_raw],
    )

    out: List[WhatsAppInvoiceListItem] = []
    for row in items_raw:
        invoice_no = str(row.get("invoice_no") or "").strip()
        if not invoice_no:
            continue
        version_no = int(row.get("version_no") or 1)
        doc_type = str(row.get("document_type") or document_type or "IN")
        try:
            erp_lines = list_erp_invoice_lines_for_header(
                tenant_id=int(row.get("tenant_id") or tenant),
                company_id=int(row.get("company_id") or company_id),
                document_type=doc_type,
                invoice_no=invoice_no,
                version_no=version_no,
            )
        except Exception:
            erp_lines = []
        lines = [
            WhatsAppInvoiceLineSummary(**normalize_invoice_line(line))
            for line in erp_lines
            if isinstance(line, dict)
        ]
        vid = int(row.get("vendor_id") or 0) or None
        inv_date = row.get("invoice_date")
        amount = row.get("invoice_amount")
        out.append(
            WhatsAppInvoiceListItem(
                invoice_no=invoice_no,
                version_no=version_no,
                document_type=doc_type,
                po_number=str(row.get("po_number") or "") or None,
                vendor_id=vid,
                vendor_name=vendor_names.get(vid) if vid else None,
                invoice_date=str(inv_date)[:10] if inv_date is not None else None,
                status=str(row.get("status") or "") or None,
                currency_code=str(row.get("currency_code") or "") or None,
                invoice_amount=str(amount) if amount is not None else None,
                line_count=len(lines),
                lines=lines,
            )
        )
    return out


@router.get("/pending", response_model=InvoicePendingListResponse)
async def list_pending_invoices(
    tenant_id: int = Query(1, alias="tenantId"),
    company_id: int = Query(3, alias="companyId"),
    document_type: str = Query("IN", alias="documentType"),
    status: str = Query(
        PENDING_INVOICE_STATUS,
        description="Invoice status treated as pending (default DRAFT)",
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List pending invoices from ERP. Access is permissions-API driven."""
    _ = db, current_user
    company_id = resolve_company_id(company_id)

    try:
        result = list_invoice_headers(
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=document_type,
            status=status,
            vendor_id=None,
            page=page,
            limit=limit,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    items = [InvoiceHeaderOut(**row) for row in result["items"]]
    return InvoicePendingListResponse(
        items=items,
        total=int(result["total"]),
        take=int(result["take"]),
        skip=int(result["skip"]),
    )


@router.get("/three-way-match-queue", response_model=ThreeWayMatchQueueResponse)
async def list_three_way_match_review_queue(
    tenant_id: int = Query(1, alias="tenantId"),
    company_id: int = Query(3, alias="companyId"),
    document_type: str = Query("IN", alias="documentType"),
    status: str = Query(
        PENDING_INVOICE_STATUS,
        description="Invoice status filter (default DRAFT). POs without a matching invoice are excluded.",
    ),
    vendor_id: Optional[int] = Query(None, alias="vendorId"),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List purchase orders eligible for invoice 3-way match review.

    Only POs that have **both** at least one invoice (matching documentType/status)
    and at least one GRN are returned.
    """
    _ = current_user
    company_id = resolve_company_id(company_id)

    try:
        result = list_three_way_match_queue(
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=document_type,
            status=status or None,
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

    items = [ThreeWayMatchPoQueueItem(**row) for row in result["items"]]
    return ThreeWayMatchQueueResponse(
        items=items,
        total=int(result["total"]),
        take=int(result["take"]),
        skip=int(result["skip"]),
    )


@router.get("/by-key/attachment")
async def download_invoice_attachment(
    tenant_id: int = Query(..., alias="tenantId"),
    company_id: int = Query(..., alias="companyId"),
    document_type: str = Query("IN", alias="documentType"),
    invoice_no: str = Query(..., alias="invoiceNo"),
    version_no: int = Query(1, alias="versionNo"),
    current_user: User = Depends(get_current_user),
):
    """Download the vendor-uploaded invoice document (PDF/image)."""
    _ = current_user
    company_id = resolve_company_id(company_id)
    file_path, filename = resolve_invoice_attachment_file(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=invoice_no,
        version_no=version_no,
    )
    return FileResponse(
        path=str(file_path),
        media_type=media_type_for_attachment(file_path),
        filename=filename,
    )


@router.delete("/by-key/with-linked-grn", response_model=InvoiceDeleteResponse)
async def delete_invoice_and_linked_grn(
    tenant_id: int = Query(..., alias="tenantId"),
    company_id: int = Query(..., alias="companyId"),
    document_type: str = Query(..., alias="documentType"),
    invoice_no: str = Query(..., alias="invoiceNo"),
    version_no: int = Query(1, alias="versionNo"),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    company_id = resolve_company_id(company_id)
    result = delete_invoice_with_linked_grn(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=invoice_no,
        version_no=version_no,
    )
    return InvoiceDeleteResponse(**result)


@router.delete("/by-po/with-linked-grn", response_model=InvoiceDeleteBatchResponse)
async def delete_invoices_for_po_with_linked_grn(
    tenant_id: int = Query(..., alias="tenantId"),
    company_id: int = Query(..., alias="companyId"),
    invoice_nos: str = Query(..., alias="invoiceNos"),
    document_type: str = Query("IN", alias="documentType"),
    version_no: int = Query(1, alias="versionNo"),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    company_id = resolve_company_id(company_id)
    numbers = [value.strip() for value in invoice_nos.split(",") if value.strip()]
    if not numbers:
        return InvoiceDeleteBatchResponse(
            deletedInvoices=0,
            deletedGrnNumbers=[],
            results=[],
        )

    results = []
    deleted_grn_numbers: set[str] = set()
    for invoice_no in numbers:
        result = delete_invoice_with_linked_grn(
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=document_type,
            invoice_no=invoice_no,
            version_no=version_no,
        )
        payload = InvoiceDeleteResponse(**result)
        results.append(payload)
        deleted_grn_numbers.update(payload.deleted_grn_numbers)

    return InvoiceDeleteBatchResponse(
        deletedInvoices=len(results),
        deletedGrnNumbers=sorted(deleted_grn_numbers),
        results=results,
    )
