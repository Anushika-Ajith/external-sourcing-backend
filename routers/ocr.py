"""
Document OCR service API.

Centralizes all OCR operations under /ocr. Legacy endpoints on /vendor and
/vendor/whatsapp remain as thin wrappers for backward compatibility.
"""

import os
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from database import get_db
from models import User, Vendor
from schemas.ocr import (
    OcrExtractResponse,
    OcrPoExtractResponse,
    OcrSalesProcessResponse,
    OcrStockProcessResponse,
    OcrVendorProcessResponse,
)
from services.auth_utils import get_current_user
from services.company_scope import resolve_company_id
from services.document_ocr import (
    cleanup_job_dir,
    extract_documents,
    process_registration_documents,
    process_sales_documents,
    process_stock_documents,
    process_vendor_documents,
    save_upload_files,
)
from services.document_ocr.constants import (
    ALLOWED_SALES_OCR_MIME_TYPES,
    ALLOWED_STOCK_OCR_MIME_TYPES,
)
from services.sales_stock_sync_job import run_sales_stock_sync_job
from services.sales_upload_perf import SalesUploadPerf
from services.whatsapp_auth import require_whatsapp_api_key


def _request_client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    if request.client and request.client.host:
        return request.client.host[:45]
    return None


router = APIRouter(
    prefix="/ocr",
    tags=["ocr"],
    responses={404: {"description": "Not found"}},
)


def _assert_vendor_ocr_access(current_user, vendor_id=None):
    """Role gate removed — permissions API owns access."""
    _ = (current_user, vendor_id)
    return



@router.post("/extract", response_model=OcrExtractResponse)
async def ocr_extract_only(
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Extract and classify document text without persisting to the database.
  Useful for previewing OCR results before import.
    """
    _ = current_user
    job_id, job_dir, saved_files, _ = await save_upload_files(files)
    try:
        return extract_documents(saved_files)
    finally:
        cleanup_job_dir(job_dir, saved_files)


@router.post("/registration/extract", response_model=OcrExtractResponse)
async def ocr_registration_extract_only(
    files: List[UploadFile] = File(...),
):
    """
    Public extract for pre-auth vendor registration autofill.
    No authentication and no database writes — preview only.
    """
    job_id, job_dir, saved_files, _ = await save_upload_files(files)
    try:
        return extract_documents(saved_files)
    finally:
        cleanup_job_dir(job_dir, saved_files)


def _whatsapp_bot_user_id(db: Session) -> int:
    raw = (os.getenv("WHATSAPP_PROCUREMENT_USER_ID") or "").strip()
    if raw.isdigit():
        return int(raw)
    user = db.query(User).order_by(User.user_id.asc()).first()
    if user and user.user_id is not None:
        return int(user.user_id)
    return 1


@router.post(
    "/vendor/{vendor_id}",
    response_model=OcrVendorProcessResponse,
)
async def ocr_process_vendor_documents(
    vendor_id: int,
    request: Request,
    files: List[UploadFile] = File(...),
    po_number: Optional[str] = Form(None),
    po_id: Optional[int] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Operational vendor document OCR: import items/rates and procurement records
    (GRN, invoice, dispatch note, sales) when applicable.

    Does **not** update vendor master, banks, or registration documents —
    use ``POST /ocr/vendor/{vendor_id}/registration`` for those.
    """
    _assert_vendor_ocr_access(current_user, vendor_id)

    job_id, job_dir, saved_files, received = await save_upload_files(files)
    try:
        return process_vendor_documents(
            db,
            vendor_id=vendor_id,
            saved_files=saved_files,
            received_files=received,
            tenant_id=int(getattr(current_user, "token_tenant_id", None) or 1),
            company_id=resolve_company_id(
                getattr(current_user, "token_company_id", None)
            ),
            created_by=str(current_user.user_id),
            po_number=po_number,
            po_id=po_id,
            bearer_token=request.headers.get("authorization") or "",
        )
    finally:
        cleanup_job_dir(job_dir, saved_files)


@router.post(
    "/whatsapp/vendor/{vendor_id}",
    response_model=OcrVendorProcessResponse,
    dependencies=[Depends(require_whatsapp_api_key)],
)
async def ocr_process_vendor_documents_for_whatsapp(
    vendor_id: int,
    files: List[UploadFile] = File(...),
    po_number: Optional[str] = Form(None),
    po_id: Optional[int] = Form(None),
    tenant_id: int = Form(1),
    company_id: int = Form(3),
    db: Session = Depends(get_db),
):
    """
    Same import path as UI `POST /ocr/vendor/{vendor_id}` (process_vendor_documents),
    authenticated with X-WhatsApp-API-Key for post-PO invoice/GRN uploads.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )

    job_id, job_dir, saved_files, received = await save_upload_files(files)
    try:
        return process_vendor_documents(
            db,
            vendor_id=vendor_id,
            saved_files=saved_files,
            received_files=received,
            tenant_id=int(tenant_id or 1),
            company_id=resolve_company_id(company_id),
            created_by=str(_whatsapp_bot_user_id(db)),
            po_number=po_number,
            po_id=po_id,
            bearer_token="",
        )
    finally:
        cleanup_job_dir(job_dir, saved_files)


@router.post(
    "/vendor/{vendor_id}/registration",
    response_model=OcrVendorProcessResponse,
)
async def ocr_process_registration_documents(
    vendor_id: int,
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Registration-phase OCR (onboarding docs only) for authenticated UI users.
    Updates vendor master / banks / registration documents.
    Skips GRN / invoice / dispatch imports.
    WhatsApp registration continues to use ``POST /vendor/whatsapp/{id}/ocr``.
    """
    _assert_vendor_ocr_access(current_user, vendor_id)

    job_id, job_dir, saved_files, received = await save_upload_files(files)
    try:
        return process_registration_documents(
            db,
            vendor_id=vendor_id,
            saved_files=saved_files,
            received_files=received,
            tenant_id=int(getattr(current_user, "token_tenant_id", None) or 1),
            company_id=resolve_company_id(
                getattr(current_user, "token_company_id", None)
            ),
            created_by=str(current_user.user_id),
        )
    finally:
        cleanup_job_dir(job_dir, saved_files)


@router.post(
    "/sales",
    response_model=OcrSalesProcessResponse,
)
async def ocr_process_sales_documents(
    request: Request,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Import sales lines from scanned sales documents (PDF/images) or CSV into
    sales_transactions.

    Expected columns / extracted fields:
    Date, Product_ID, Product_Name, Category, Qty_Sold
    """
    perf = SalesUploadPerf()
    with perf.stage("authentication"):
        tenant_id = int(getattr(current_user, "token_tenant_id", None) or 1)
        company_id = resolve_company_id(
            getattr(current_user, "token_company_id", None)
        )
        created_by = current_user.user_id
        created_ip = _request_client_ip(request)

    with perf.stage("file_save"):
        job_id, job_dir, saved_files, received = await save_upload_files(
            files,
            allowed_mime_types=ALLOWED_SALES_OCR_MIME_TYPES,
        )
    try:
        result = process_sales_documents(
            db,
            saved_files=saved_files,
            received_files=received,
            tenant_id=tenant_id,
            company_id=company_id,
            created_by=created_by,
            created_ip=created_ip,
            perf=perf,
        )
        affected_product_ids = result.pop("affected_product_ids", [])
        performance = result.pop("performance", None)
        if performance:
            result["performance"] = performance

        stock_sync = result.get("stock_trend_sync") or {}
        if stock_sync.get("status") == "queued" and affected_product_ids:
            background_tasks.add_task(
                run_sales_stock_sync_job,
                tenant_id=tenant_id,
                company_id=company_id,
                product_ids=affected_product_ids,
                updated_by=created_by,
            )
        return result
    finally:
        cleanup_job_dir(job_dir, saved_files)


@router.post(
    "/stock",
    response_model=OcrStockProcessResponse,
)
async def ocr_process_stock_documents(
    files: List[UploadFile] = File(...),
    document_no: Optional[str] = Form(None),
    upload_source: Optional[str] = Form("WEB"),
    tenant_id: Optional[int] = Form(None),
    company_id: Optional[int] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Import stock lines from CSV/Excel or scanned stock documents into:
      - stock (master; upsert by product_code; available_stock overwritten)
      - stock_upload_header
      - stock_upload_detail

    Expected columns / extracted fields:
    Product_Code (or SKU), Product_Name, Available_Stock (qty),
    optional Category, Brand, Unit, Barcode, Reorder_Level, Reorder_Qty, Supplier
    """
    company_id = resolve_company_id(company_id)
    job_id, job_dir, saved_files, received = await save_upload_files(
        files,
        allowed_mime_types=ALLOWED_STOCK_OCR_MIME_TYPES,
    )
    try:
        return process_stock_documents(
            db,
            saved_files=saved_files,
            received_files=received,
            tenant_id=int(
                tenant_id
                if tenant_id is not None
                else getattr(current_user, "token_tenant_id", None) or 1
            ),
            company_id=resolve_company_id(
                company_id
                if company_id is not None
                else getattr(current_user, "token_company_id", None)
            ),
            created_by=current_user.user_id,
            document_no=document_no,
            upload_source=(upload_source or "WEB"),
        )
    finally:
        cleanup_job_dir(job_dir, saved_files)


@router.post(
    "/purchase-order/extract",
    response_model=OcrPoExtractResponse,
)
async def ocr_extract_purchase_order(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Extract purchase-order business fields from a document without creating a PO.
    Proxies to the standalone OCR microservice.
    """
    _ = current_user
    job_id, job_dir, saved_files, _ = await save_upload_files([file])
    try:
        from services.ocr_client import extract_purchase_order_from_paths

        data = extract_purchase_order_from_paths(saved_files)
        return {
            "status": data.get("status", "completed"),
            "document_classification": data.get("document_classification"),
            "raw_text": data.get("raw_text"),
            "business_fields": data.get("business_fields") or {},
        }
    finally:
        cleanup_job_dir(job_dir, saved_files)
