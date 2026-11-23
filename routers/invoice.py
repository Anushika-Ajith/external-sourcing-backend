"""Invoice routes (ERP-backed delete + pending list + attachment download)."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db
from models import User
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
from services.invoice_list_service import PENDING_INVOICE_STATUS, list_invoice_headers
from services.three_way_match_queue_service import list_three_way_match_queue

router = APIRouter(
    prefix="/invoice",
    tags=["invoice"],
    responses={404: {"description": "Not found"}},
)


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
