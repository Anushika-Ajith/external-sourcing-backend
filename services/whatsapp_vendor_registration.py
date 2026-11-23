"""
WhatsApp vendor onboarding: start registration, basic-doc OCR, login provisioning.

Phase 1 (WhatsApp bot, service API key):
  start → OCR basic documents → return email + generated password

Phase 2 (vendor web app, normal JWT):
  login → upload remaining docs via POST /vendor/{id}/ocr
  → POST /vendor/{id}/complete-registration
"""

from __future__ import annotations

import re
import secrets
import string
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from models import User, Vendor
from services.auth_utils import create_access_token, get_password_hash
from services.document_ocr import (
    cleanup_job_dir,
    process_registration_documents,
    save_upload_files,
)
from services.foundation_user import create_vendor_user_in_foundation
from services.db_sequences import create_user


def whatsapp_email_from_mobile(mobile: str) -> str:
    digits = re.sub(r"\D", "", mobile or "")
    if not digits:
        raise ValueError("mobile is required")
    return f"{digits}@whatsapp.vendor.local"


def _generate_password(length: int = 12) -> str:
    """Meets vendor UI rules: upper, lower, digit, special."""
    lowers = string.ascii_lowercase
    uppers = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%&*"
    all_chars = lowers + uppers + digits + special
    while True:
        pwd = "".join(secrets.choice(all_chars) for _ in range(length))
        if (
            any(c in lowers for c in pwd)
            and any(c in uppers for c in pwd)
            and any(c in digits for c in pwd)
            and any(c in special for c in pwd)
        ):
            return pwd


def start_or_get_vendor(
    db: Session,
    *,
    mobile: str,
    email: Optional[str] = None,
    tenant_id: int = 1,
    company_id: int = 3,
    company_code: Optional[str] = None,
    created_by_a: str = "whatsapp",
    created_ip_a: Optional[str] = None,
) -> Tuple[Vendor, bool]:
    """
    Mirror GET /vendor/start-registration.
    Returns (vendor, created) where created is True when a new row was inserted.

    Resume by email only. A mobile match on a different email must not reuse that
    vendor (would overwrite company details on later complete-registration).
    """
    from services.po_vendor_scope import find_vendor_by_email
    from services.vendor_email_sync import normalize_vendor_email
    from services.vendor_phone_lookup import find_vendor_by_phone

    mobile = (mobile or "").strip()
    resolved_email = (
        normalize_vendor_email(email)
        or (email or "").strip()
        or whatsapp_email_from_mobile(mobile)
    )

    existing_by_email = find_vendor_by_email(db, resolved_email)
    if existing_by_email:
        return existing_by_email, False

    existing_by_mobile = find_vendor_by_phone(db, mobile) if mobile else None
    if existing_by_mobile is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This mobile number is already registered to another vendor. "
                "Use a different mobile number, or continue with that vendor's email."
            ),
        )

    vendor = Vendor(
        email=resolved_email,
        mobile=mobile,
        status="pending_verification",
        tenant_id=tenant_id,
        company_id=company_id,
        company_code=company_code,
        vendor_registration_no=None,
        vendor_code=None,
        created_by_a=created_by_a,
        created_ip_a=created_ip_a,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor, True


def run_registration_ocr(
    db: Session,
    *,
    vendor_id: int,
    saved_files: List[str],
    received_files: List[Dict[str, str]],
    tenant_id: int = 1,
    company_id: int = 3,
    created_by: str = "whatsapp",
) -> Dict[str, Any]:
    """Backward-compatible wrapper. Prefer document_ocr.process_registration_documents."""
    return process_registration_documents(
        db,
        vendor_id=vendor_id,
        saved_files=saved_files,
        received_files=received_files,
        tenant_id=tenant_id,
        company_id=company_id,
        created_by=created_by,
    )


def provision_vendor_account(
    db: Session,
    vendor_id: int,
    *,
    password: Optional[str] = None,
    reset_existing: bool = True,
) -> Dict[str, Any]:
    """
    Create or update users row for vendor login. Returns plaintext password once.
    Sets vendor status to otp_verified (WhatsApp skips OTP UI).
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )
    if not vendor.email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vendor has no email; run start-registration first",
        )

    plain_password = password or _generate_password()

    # Create / update Foundation user with vendor UserRole before local account
    create_vendor_user_in_foundation(
        vendor.email,
        plain_password,
        company_id=int(getattr(vendor, "company_id", None) or 3),
        tenant_id=int(getattr(vendor, "tenant_id", None) or 1),
        phone_number=getattr(vendor, "mobile", None),
        name=getattr(vendor, "company_name", None) or vendor.email,
    )

    hashed = get_password_hash(plain_password)

    user = db.query(User).filter(User.email == vendor.email).first()
    if user:
        if user.vendor_id and user.vendor_id != vendor_id and not reset_existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already linked to another vendor",
            )
        user.hashed_password = hashed
        user.vendor_id = vendor.vendor_id
        user.role = "vendor"
        user.is_active = True
        message = "Password updated for existing vendor account"
    else:
        user = create_user(
            db,
            email=vendor.email,
            hashed_password=hashed,
            role="vendor",
            is_active=True,
            vendor_id=vendor.vendor_id,
            commit=False,
        )
        message = "Vendor account created"

    if vendor.status == "pending_verification":
        vendor.status = "otp_verified"

    db.commit()
    db.refresh(user)
    db.refresh(vendor)

    token = create_access_token(
        data={
            "sub": user.email,
            "user_id": user.user_id,
            "role": user.role,
            "email": user.email,
        }
    )

    return {
        "vendor_id": vendor.vendor_id,
        "email": vendor.email,
        "password": plain_password,
        "message": message,
        "vendor_status": vendor.status,
        "access_token": token,
        "token_type": "bearer",
        "next_step": "Login at /vendor/login or upload more documents at /vendor/complete-registration",
    }


def _email_from_ocr_payload(ocr_result: Optional[Dict[str, Any]]) -> Optional[str]:
    """Pull vendor.email from registration OCR raw_text when present."""
    if not isinstance(ocr_result, dict):
        return None
    raw = ocr_result.get("raw_text")
    if not isinstance(raw, dict):
        return None
    vendor_blob = raw.get("vendor")
    if not isinstance(vendor_blob, dict):
        return None
    email = str(vendor_blob.get("email") or "").strip()
    return email or None


def _is_synthetic_whatsapp_email(email: Optional[str]) -> bool:
    value = (email or "").strip().lower()
    return value.endswith("@whatsapp.vendor.local")


def _apply_ocr_email_before_provision(
    db: Session,
    vendor: Vendor,
    ocr_result: Optional[Dict[str, Any]],
) -> None:
    """
    Prefer a real email extracted from documents over the synthetic
    {mobile}@whatsapp.vendor.local placeholder created at start.
    """
    extracted = _email_from_ocr_payload(ocr_result)
    if not extracted:
        return
    # Keep an explicitly provided real email (do not overwrite with OCR).
    if vendor.email and not _is_synthetic_whatsapp_email(vendor.email):
        return
    conflict = (
        db.query(Vendor.vendor_id)
        .filter(Vendor.email == extracted, Vendor.vendor_id != vendor.vendor_id)
        .first()
    )
    if conflict:
        return
    if vendor.email != extracted:
        vendor.email = extracted
        db.add(vendor)
        db.commit()
        db.refresh(vendor)


def _persist_uploads_without_ocr(
    db: Session,
    *,
    vendor_id: int,
    saved_files: List[str],
    received_files: List[Dict[str, str]],
    tenant_id: int,
    company_id: int,
    ocr_error: str,
) -> Dict[str, Any]:
    """
    Save uploaded registration files even when the OCR microservice is down.
    Bank details are never required — rows are only inserted when OCR finds them.
    """
    from services.ocr_document_import import insert_ocr_vendor_documents
    from services.vendor_document_storage import persist_vendor_upload_files

    upload_names = [r["filename"] for r in received_files]
    persisted_paths, upload_names = persist_vendor_upload_files(
        vendor_id, saved_files, upload_names
    )
    docs_inserted = insert_ocr_vendor_documents(
        db,
        vendor_id,
        {},
        persisted_paths,
        upload_names,
        document_classification=None,
    )
    db.commit()
    return {
        "vendor_id": vendor_id,
        "status": "completed_without_ocr",
        "files_received": received_files,
        "document_classification": {},
        "vendor_items_inserted": 0,
        "vendor_rates_inserted": 0,
        "vendor_banks_inserted": 0,
        "vendor_documents_inserted": docs_inserted,
        "raw_text": {},
        "ocr_error": ocr_error,
        "message": (
            "Documents saved without OCR. Registration continues; "
            "bank details are optional and can be added later on the web portal."
        ),
    }


def _resolved_real_email(vendor: Vendor, ocr_result: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return a non-synthetic email from vendor row or OCR payload."""
    if vendor.email and not _is_synthetic_whatsapp_email(vendor.email):
        return vendor.email.strip()
    return _email_from_ocr_payload(ocr_result)


def set_vendor_registration_email(
    db: Session,
    vendor_id: int,
    email: str,
) -> Dict[str, Any]:
    """
    Attach a real business email after WhatsApp docs were uploaded without one.
    Does not create a password — vendor sets password after buyer approval.
    """
    from services.vendor_email_sync import normalize_vendor_email

    cleaned = normalize_vendor_email(email) or (email or "").strip().lower()
    if not cleaned or "@" not in cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A valid email address is required",
        )
    if _is_synthetic_whatsapp_email(cleaned):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please provide your real business email address",
        )

    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )

    conflict = (
        db.query(Vendor.vendor_id)
        .filter(Vendor.email == cleaned, Vendor.vendor_id != vendor_id)
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This email is already registered to another vendor",
        )

    vendor.email = cleaned
    if vendor.status in (None, "pending_verification", "otp_verified"):
        vendor.status = "pending"
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    return {
        "vendor_id": vendor.vendor_id,
        "email": vendor.email,
        "vendor_status": vendor.status or "pending",
        "message": (
            "Email saved. Your registration has been submitted for approval. "
            "You will receive a WhatsApp message to set your password once approved."
        ),
    }


async def register_vendor_via_whatsapp(
    db: Session,
    *,
    mobile: str,
    files: List[UploadFile],
    email: Optional[str] = None,
    tenant_id: int = 1,
    company_id: int = 3,
    company_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    WhatsApp onboarding: start → OCR basic docs → submit for approval.

    - Uses the same OCR microservice as POST /ocr/registration/extract
    - Email is taken from OCR (or the optional form email)
    - No password is generated — set after buyer approval
    - If email cannot be found, returns email_required=True
    - Missing bank details do not block registration
    """
    start_email = (email or "").strip() or None
    if start_email and _is_synthetic_whatsapp_email(start_email):
        start_email = None

    vendor, created = start_or_get_vendor(
        db,
        mobile=mobile,
        email=start_email,
        tenant_id=tenant_id,
        company_id=company_id,
        company_code=company_code,
        created_by_a="whatsapp",
    )

    job_id, job_dir, saved_files, received = await save_upload_files(files)
    ocr_result: Dict[str, Any]
    try:
        try:
            ocr_result = run_registration_ocr(
                db,
                vendor_id=vendor.vendor_id,
                saved_files=saved_files,
                received_files=received,
                tenant_id=tenant_id,
                company_id=company_id,
            )
        except HTTPException as exc:
            if exc.status_code in (
                status.HTTP_503_SERVICE_UNAVAILABLE,
                status.HTTP_502_BAD_GATEWAY,
                status.HTTP_504_GATEWAY_TIMEOUT,
            ):
                ocr_result = _persist_uploads_without_ocr(
                    db,
                    vendor_id=vendor.vendor_id,
                    saved_files=saved_files,
                    received_files=received,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    ocr_error=str(exc.detail),
                )
            else:
                raise

        db.refresh(vendor)
        _apply_ocr_email_before_provision(db, vendor, ocr_result)
        db.refresh(vendor)

        real_email = _resolved_real_email(vendor, ocr_result)
        if not real_email:
            return {
                "vendor_id": vendor.vendor_id,
                "vendor_created": created,
                "email": None,
                "password": None,
                "access_token": None,
                "vendor_status": vendor.status or "pending_verification",
                "email_required": True,
                "ocr": ocr_result,
                "message": (
                    "Documents received, but no email was found in them. "
                    "Please provide your business email to finish registration."
                ),
                "next_steps": [
                    f"POST /vendor/whatsapp/{vendor.vendor_id}/email with email=...",
                ],
            }

        if vendor.email != real_email:
            vendor.email = real_email
        vendor.status = "pending"
        db.add(vendor)
        db.commit()
        db.refresh(vendor)
    finally:
        cleanup_job_dir(job_dir, saved_files)

    ocr_note = ""
    if isinstance(ocr_result, dict) and ocr_result.get("ocr_error"):
        ocr_note = (
            " OCR was temporarily unavailable — documents were saved and can be "
            "re-processed from the web portal."
        )

    return {
        "vendor_id": vendor.vendor_id,
        "vendor_created": created,
        "email": vendor.email,
        "password": None,
        "access_token": None,
        "vendor_status": vendor.status or "pending",
        "email_required": False,
        "ocr": ocr_result,
        "message": (
            "Registration submitted for approval via WhatsApp. "
            "No password was set — the vendor will set one after approval."
            + ocr_note
        ),
        "next_steps": [
            "Buyer reviews vendor in onboarding / complete-registration",
            "After ACTIVE, vendor receives WhatsApp to set password",
        ],
    }
