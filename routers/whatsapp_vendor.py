"""
WhatsApp vendor onboarding APIs (service key auth).

Flow:
  1. POST /vendor/whatsapp/register  (or start → ocr → provision)
  2. Bot returns email + password to user on WhatsApp
  3. User logs in on web → complete-registration + more OCR docs

Also:
  - GET  /vendor/whatsapp/pending — list SUBMITTED vendors (buyer review)
  - POST /vendor/whatsapp/{id}/approve — same as UI approve (+ notify)
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import Vendor
from schemas.vendor import VendorProfileOut
from schemas.whatsapp_vendor import (
    WhatsAppProvisionResponse,
    WhatsAppRegisterResponse,
    WhatsAppSetEmailResponse,
    WhatsAppStartRegistrationResponse,
    VendorPhoneCheckResponse,
)
from services.whatsapp_auth import require_whatsapp_api_key
from services.vendor_phone_lookup import find_vendor_by_phone, vendor_has_login
from services.company_scope import resolve_company_id
from services.vendor_codes import assign_vendor_code_on_approval
from services.whatsapp_notify_service import notify_vendor_approved_whatsapp
from services.whatsapp_vendor_registration import (
    cleanup_job_dir,
    provision_vendor_account,
    register_vendor_via_whatsapp,
    run_registration_ocr,
    save_upload_files,
    set_vendor_registration_email,
    start_or_get_vendor,
    whatsapp_email_from_mobile,
)

router = APIRouter(
    prefix="/vendor/whatsapp",
    tags=["vendor-whatsapp"],
    dependencies=[Depends(require_whatsapp_api_key)],
)


@router.get("/check-phone", response_model=VendorPhoneCheckResponse)
async def whatsapp_check_vendor_phone(
    mobile: str = Query(..., description="Phone / mobile number to look up"),
    db: Session = Depends(get_db),
):
    """
    Check whether a vendor already exists for this phone number.
    Matches common formats (e.g. 8086076193, 918086076193, +918086076193).
    """
    vendor = find_vendor_by_phone(db, mobile)
    if not vendor:
        return VendorPhoneCheckResponse(
            exists=False,
            mobile=mobile,
            message="No vendor found for this phone number",
        )
    return VendorPhoneCheckResponse(
        exists=True,
        mobile=mobile,
        vendor_id=vendor.vendor_id,
        email=vendor.email,
        status=vendor.status,
        vendor_registration_no=vendor.vendor_registration_no,
        has_login_account=vendor_has_login(db, vendor),
        message="Vendor already registered with this phone number",
    )


@router.get("/pending", response_model=List[VendorProfileOut])
async def whatsapp_list_pending_vendors(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    company_id: Optional[int] = Query(None),
    tenant_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Same as UI `GET /vendor/pending` — vendors with status SUBMITTED.
    Used by WhatsApp buyer flow: list → approve vendor N.
    """
    scoped_company = resolve_company_id(company_id) if company_id is not None else None
    q = db.query(Vendor).filter(func.upper(Vendor.status) == "SUBMITTED")
    if scoped_company is not None:
        q = q.filter(Vendor.company_id == scoped_company)
    if tenant_id is not None:
        q = q.filter(Vendor.tenant_id == int(tenant_id))
    return (
        q.order_by(Vendor.created_at.desc().nullslast(), Vendor.vendor_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.post("/{vendor_id}/approve")
async def whatsapp_approve_vendor(
    vendor_id: int,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Same as UI `POST /vendor/{id}/approve`:
    assign vendor_code, set ACTIVE, fire notify_vendor_approved_whatsapp.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )

    current_upper = (vendor.status or "").strip().upper()
    if current_upper in ("ACTIVE", "APPROVED"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vendor is already approved.",
        )

    try:
        assign_vendor_code_on_approval(db, vendor)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )

    vendor.status = "ACTIVE"
    vendor.modified_on_a = datetime.utcnow()
    db.commit()
    db.refresh(vendor)

    try:
        notify_vendor_approved_whatsapp(
            vendor_id=int(vendor.vendor_id),
            vendor_name=vendor.company_name,
            vendor_email=vendor.email,
            vendor_mobile=vendor.mobile,
        )
    except Exception as notify_exc:
        print(
            f"[VENDOR-APPROVE-WHATSAPP] unexpected notify error (ignored): {notify_exc}"
        )

    return {
        "message": "Vendor approved successfully",
        "vendor_id": vendor.vendor_id,
        "vendor_code": vendor.vendor_code,
        "vendor_registration_no": vendor.vendor_registration_no,
        "status": vendor.status,
        "company_name": vendor.company_name,
    }


@router.post("/start", response_model=WhatsAppStartRegistrationResponse)
async def whatsapp_start_registration(
    mobile: str = Form(..., description="Vendor mobile number (E.164 or local digits)"),
    email: Optional[str] = Form(
        None,
        description="Optional login email; default is {mobile}@whatsapp.vendor.local",
    ),
    tenant_id: int = Form(1),
    company_id: int = Form(3),
    company_code: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """
    Step 1 — same as GET /vendor/start-registration but for WhatsApp bots (API key).
    """
    company_id = resolve_company_id(company_id)
    vendor, created = start_or_get_vendor(
        db,
        mobile=mobile,
        email=email,
        tenant_id=tenant_id,
        company_id=company_id,
        company_code=company_code,
        created_by_a="whatsapp",
    )
    return WhatsAppStartRegistrationResponse(
        vendor_id=vendor.vendor_id,
        vendor_registration_no=vendor.vendor_registration_no,
        email=vendor.email or whatsapp_email_from_mobile(mobile),
        mobile=vendor.mobile or mobile,
        status=vendor.status or "pending_verification",
        vendor_created=created,
        message=(
            "Vendor registration started. Please verify OTP."
            if created
            else "Vendor already exists. You can continue with registration."
        ),
    )


@router.post("/{vendor_id}/ocr")
async def whatsapp_registration_ocr(
    vendor_id: int,
    files: List[UploadFile] = File(...),
    tenant_id: int = Form(1),
    company_id: int = Form(3),
    db: Session = Depends(get_db),
):
    """
    Step 2 — upload basic registration documents (GST, PAN, rate contract, etc.).
    No vendor JWT required; uses X-WhatsApp-API-Key only.
    """
    company_id = resolve_company_id(company_id)
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")

    job_id, job_dir, saved_files, received = await save_upload_files(files)
    try:
        return run_registration_ocr(
            db,
            vendor_id=vendor_id,
            saved_files=saved_files,
            received_files=received,
            tenant_id=tenant_id,
            company_id=company_id,
        )
    finally:
        cleanup_job_dir(job_dir, saved_files)


@router.post("/{vendor_id}/provision-account", response_model=WhatsAppProvisionResponse)
async def whatsapp_provision_account(
    vendor_id: int,
    db: Session = Depends(get_db),
):
    """
    Step 3 — create login email + generated password for the vendor.
  Skips OTP (sets status otp_verified). Return credentials to user on WhatsApp.
    """
    result = provision_vendor_account(db, vendor_id)
    return WhatsAppProvisionResponse(**result)


@router.post(
    "/register",
    response_model=WhatsAppRegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def whatsapp_register_vendor(
    mobile: str = Form(...),
    files: List[UploadFile] = File(...),
    email: Optional[str] = Form(None),
    tenant_id: int = Form(1),
    company_id: int = Form(3),
    company_code: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """
    WhatsApp onboarding: start + OCR basic docs → submit for approval.

    Email is taken from OCR (or optional form). No password is created.
    If email is missing, response has email_required=true — call
    POST /vendor/whatsapp/{vendor_id}/email next.
    """
    company_id = resolve_company_id(company_id)
    result = await register_vendor_via_whatsapp(
        db,
        mobile=mobile,
        files=files,
        email=email,
        tenant_id=tenant_id,
        company_id=company_id,
        company_code=company_code,
    )
    return WhatsAppRegisterResponse(**result)


@router.post("/{vendor_id}/email", response_model=WhatsAppSetEmailResponse)
async def whatsapp_set_vendor_email(
    vendor_id: int,
    email: str = Form(..., description="Business email extracted/asked on WhatsApp"),
    db: Session = Depends(get_db),
):
    """Set vendor email after docs when OCR did not find one. No password created."""
    result = set_vendor_registration_email(db, vendor_id, email)
    return WhatsAppSetEmailResponse(**result)
