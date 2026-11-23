"""Invoice line product match review and confirmation."""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from models import User
from schemas.invoice_line_match import (
    InvoiceConfirmedNotifyRequest,
    InvoiceConfirmedNotifyResponse,
    InvoiceLineMatchConfirmRequest,
    InvoiceLineMatchConfirmResponse,
    InvoiceLineMatchReviewResponse,
)
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id
from services.invoice_line_match_service import (
    confirm_invoice_line_match,
    get_invoice_match_review,
)
from services.whatsapp_notify_service import notify_invoice_confirmed_whatsapp

router = APIRouter(
    prefix="/invoice-line-match",
    tags=["invoice-line-match"],
    responses={404: {"description": "Not found"}},
)


@router.get("/review", response_model=InvoiceLineMatchReviewResponse)
async def get_invoice_line_match_review(
    tenant_id: int = Query(..., alias="tenantId"),
    company_id: int = Query(..., alias="companyId"),
    document_type: str = Query(..., alias="documentType"),
    invoice_no: str = Query(..., alias="invoiceNo"),
    version_no: int = Query(1, alias="versionNo"),
    line_no: Optional[int] = Query(None, alias="lineNo"),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    company_id = resolve_company_id(company_id)
    result = get_invoice_match_review(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=invoice_no,
        version_no=version_no,
        line_no=line_no,
    )
    return InvoiceLineMatchReviewResponse(**result)


@router.post("/confirm", response_model=InvoiceLineMatchConfirmResponse)
async def confirm_invoice_line_product_match(
    body: InvoiceLineMatchConfirmRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    body.companyId = resolve_company_id(body.companyId)
    result = confirm_invoice_line_match(
        db,
        tenant_id=body.tenantId,
        company_id=body.companyId,
        document_type=body.documentType,
        invoice_no=body.invoiceNo,
        version_no=body.versionNo,
        line_no=body.lineNo,
        selected_po_line_no=body.selectedPoLineNo,
        selected_item_id=body.selectedItemId,
        selected_product_fingerprint=body.selectedProductFingerprint,
        create_alias=body.createAlias,
    )
    return InvoiceLineMatchConfirmResponse(**result)


@router.post("/notify-confirmed", response_model=InvoiceConfirmedNotifyResponse)
async def notify_invoice_confirmed(
    body: InvoiceConfirmedNotifyRequest,
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    try:
        outcome = notify_invoice_confirmed_whatsapp(
            po_number=body.poNumber,
            invoice_no=body.invoiceNo,
            invoice_nos=body.invoiceNos,
            line_count=body.lineCount,
            to=body.to,
            message=body.message,
        )
        return InvoiceConfirmedNotifyResponse(
            ok=bool(outcome.get("ok")),
            sent=bool(outcome.get("sent")),
            reason=outcome.get("reason"),
        )
    except Exception as exc:
        print(f"[INVOICE-WHATSAPP] unexpected notify error (ignored): {exc}")
        return InvoiceConfirmedNotifyResponse(ok=False, sent=False, reason="error")
