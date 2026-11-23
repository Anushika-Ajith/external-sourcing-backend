from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form, Query, Response, Request
from fastapi.responses import FileResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text, func, or_
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor
import os
import shutil
import uuid
from pathlib import Path
import requests
from jose import jwt
from email_validator import EmailNotValidError, validate_email
import re

# Nest hops during login; keep total under the frontend 30s axios budget where possible.
_NEST_LOGIN_TIMEOUT_S = 10
_NEST_CONTEXT_TIMEOUT_S = 12
_NEST_SIDE_TIMEOUT_S = 15  # /me/permissions + /auth/session (Render can be slow on cold start)

# vendors.status values set by complete-registration Save Draft / Submit.
REGISTRATION_STATUSES = ("DRAFT", "SUBMITTED")

from services.document_ocr import (
    cleanup_job_dir,
    process_vendor_documents,
    save_upload_files,
)
from services.vendor_document_storage import (
    persist_vendor_upload_files,
    resolve_document_file_path,
    to_public_document_path,
    vendor_documents_dir,
)
from services.rate_contract_product_matching_engine import VENDOR_ITEM_STATUS_PENDING
from services.vendor_codes import (
    clip_vendor_code,
    ensure_vendor_code_from_registration,
    assign_vendor_code_on_approval,
)
from services.db_sequences import create_user
from schemas.rfq import VendorRFQWorkbench
from models import RFQHeader, RFQVendor

from models import RFQLine
from schemas.rfq import RFQLineResponse
def _read_pem_from_env_file(var_name: str, begin_marker: str, end_marker: str) -> Optional[str]:
    """Fallback parser for multiline PEM values from backend/.env."""
    try:
        env_path = Path(__file__).resolve().parents[1] / ".env"
        raw = env_path.read_text(encoding="utf-8")
        pattern = rf"{var_name}=(?P<val>{re.escape(begin_marker)}.*?{re.escape(end_marker)})"
        m = re.search(pattern, raw, flags=re.DOTALL)
        if not m:
            return None
        return m.group("val").strip().strip('"').replace("\\n", "\n")
    except Exception:
        return None


def _nest_rs256_private_key() -> str:
    key = settings.jwt_private_key
    if key:
        key = key.strip().strip('"').replace("\\n", "\n")
    if not key or "END PRIVATE KEY" not in key:
        key = _read_pem_from_env_file(
            "JWT_PRIVATE_KEY",
            "-----BEGIN PRIVATE KEY-----",
            "-----END PRIVATE KEY-----",
        )
    if not key:
        raise RuntimeError("JWT_PRIVATE_KEY missing or malformed for RS256")
    return key


def _fetch_nest_permissions_and_session(
    nest_base: str, nest_token: str
) -> Tuple[Optional[Any], Optional[Dict[str, Any]], Optional[str]]:
    """Fetch /me/permissions and /auth/session in parallel (soft-fail)."""

    def _get_permissions() -> Any:
        me_permissions_url = f"{nest_base.rstrip('/')}/me/permissions"
        print(f"[LOGIN API] GET {me_permissions_url}?applicationId=2")
        me_perm_resp = requests.get(
            me_permissions_url,
            params={"applicationId": 2},
            headers={"Authorization": f"Bearer {nest_token}"},
            timeout=_NEST_SIDE_TIMEOUT_S,
        )
        body = me_perm_resp.json() if me_perm_resp.content else None
        print(
            f"[LOGIN] /me/permissions (applicationId=2) status={me_perm_resp.status_code}"
        )
        return body

    def _get_session() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        session_url = f"{nest_base.rstrip('/')}/auth/session"
        print(f"[LOGIN API] GET {session_url}?applicationId=2")
        resp = requests.get(
            session_url,
            params={"applicationId": 2},
            headers={"Authorization": f"Bearer {nest_token}"},
            timeout=_NEST_SIDE_TIMEOUT_S,
        )
        if not resp.ok:
            return None, f"session_fetch_failed: {resp.status_code}"
        payload = resp.json() if resp.content else None
        if isinstance(payload, dict):
            app_sec = (
                payload.get("applicationSecurity")
                or payload.get("permissions")
                or payload.get("applicationPermissions")
            )
            payload["applicationSecurity"] = app_sec
        return payload if isinstance(payload, dict) else None, None

    permissions_payload: Optional[Any] = None
    session_payload: Optional[Dict[str, Any]] = None
    session_error: Optional[str] = None

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_perm = pool.submit(_get_permissions)
        fut_sess = pool.submit(_get_session)
        try:
            permissions_payload = fut_perm.result()
        except Exception as e:
            print(f"[LOGIN] /me/permissions fetch error: {e}")
        try:
            session_payload, session_error = fut_sess.result()
        except Exception as e:
            print(f"[LOGIN] /auth/session fetch error: {e}")
            session_error = f"session_fetch_error: {e}"

    return permissions_payload, session_payload, session_error


# Import from models
from models import Vendor, VendorBank, VendorDocument, VendorItem, User, OTP, VendorRate, AuditLog
# Import from database
from database import get_db
# Import config for database info
from config import settings
# Import schemas
from schemas.rate_contract_match import (
    RateContractMatchConfirmRequest,
    RateContractMatchConfirmResponse,
)
from schemas.vendor_item_confirm import VendorItemConfirmResponse
from schemas.vendor import (
    Vendor as VendorSchema,
    VendorCreate,
    VendorUpdate,
    VendorBank as VendorBankSchema,
    VendorBankCreate,
    VendorBankBase,
    VendorItem as VendorItemSchema,
    VendorItemCreate,
    VendorItemUpdate,
    VendorItemWithRateCreate,
    VendorItemResponse,
    VendorDocument as VendorDocumentSchema,
    VendorDocumentCreate,
    VendorRate as VendorRateSchema,
    VendorRateCreate,
    VendorRateCreateBody,
    AuditLog as AuditLogSchema,
    OTPVerify,
    VendorApplication,
    VendorProfileOut,
    VendorProfileUpdate,
    VendorDetailOut,
    VendorDirectoryItem,
    VendorsForErpCodeOut,
    VendorWithProductsAndRatesOut,
    VendorWithBanksProductsAndRatesOut,
    VendorRegistrationReviewOut,
    VendorStatusUpdate,
)
from schemas.whatsapp_vendor import (
    VendorPhoneCheckResponse,
    VendorEmailCheckResponse,
    VendorSetupPasswordByEmailRequest,
    VendorForgotPasswordRequest,
    VendorForgotPasswordResponse,
    VendorResetPasswordRequest,
    VendorResetPasswordResponse,
)
from services.whatsapp_notify_service import notify_vendor_approved_whatsapp
from services.vendor_phone_lookup import (
    find_vendor_by_phone,
    user_password_is_set,
    vendor_has_login,
)
# Import auth and utils
from services.po_vendor_scope import resolve_vendor_for_user
from services.auth_utils import (
    _is_users_schema_mismatch,
    get_current_user,
    get_current_vendor,
    get_password_hash,
    verify_password,
    create_access_token,
    authenticate_vendor,
)
from services.foundation_user import create_vendor_user_in_foundation
from services.password_reset_tokens import (
    build_password_reset_url,
    consume_password_reset_token,
    create_password_reset_token,
)
from services.vendor_email_sync import normalize_vendor_email, sync_vendor_email_change
from services.vendor_delete_service import delete_vendor_and_related
from services.company_scope import resolve_company_id
from services.ocr_item_rate_import import apply_canonical_pack_size_to_item
from services.internal_product_identity import list_vendors_for_stock_product_codes
from services.po_vendor_rate_enrich import get_vendor_rate_for_product_code
from services.rate_contract_match_service import confirm_rate_contract_line_match
from services.vendor_item_confirm_service import (
    assert_no_duplicate_vendor_item,
    confirm_vendor_item_status,
)
from services.approval_workflow_client import assert_vendor_activation_allowed
from services.object_reservation_client import (
    assert_vendor_edit_reservation,
    release_vendor_edit_reservation_by_key,
)
from services.vendor_identity_guard import protect_vendor_update_payload

def _bearer_token_from_request(request: Request) -> str:
    return (
        request.headers.get("authorization")
        or request.headers.get("Authorization")
        or ""
    )

router = APIRouter(
    prefix="/vendor",
    tags=["vendor"],
    responses={404: {"description": "Not found"}},
)

# KEEP THESE PROFILE ENDPOINTS AT THE TOP
@router.get("/profile", response_model=VendorProfileOut)
async def get_vendor_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from services.po_vendor_scope import resolve_vendor_for_user

    vendor = resolve_vendor_for_user(db, current_user, persist_link=True)
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor profile not found"
        )

    return vendor

@router.put("/profile", response_model=VendorProfileOut)
async def update_vendor_profile(
    vendor_data: VendorProfileUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from services.po_vendor_scope import resolve_vendor_for_user

    vendor = resolve_vendor_for_user(db, current_user, persist_link=True)
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor profile not found"
        )

    bearer = _bearer_token_from_request(request)
    assert_vendor_edit_reservation(
        vendor_id=int(vendor.vendor_id),
        bearer_token=bearer,
    )

    old_email = vendor.email
    updates = protect_vendor_update_payload(
        vendor, vendor_data.dict(exclude_unset=True)
    )
    if "email" in updates and updates["email"] is not None:
        new_email = str(updates["email"])
        if normalize_vendor_email(new_email) != normalize_vendor_email(old_email):
            sync_vendor_email_change(
                db,
                vendor=vendor,
                old_email=old_email,
                new_email=new_email,
                current_user_id=current_user.user_id,
            )

    # Update vendor fields (only the keys that exist on the SQLAlchemy model).
    # Identity fields cannot be cleared or replaced with OCR placeholder junk.
    for field, value in updates.items():
        if field == "email" and value is not None:
            value = normalize_vendor_email(str(value))
        if hasattr(vendor, field):
            setattr(vendor, field, value)
    vendor.modified_on_a = datetime.utcnow()
    
    try:
        db.commit()
        db.refresh(vendor)
    except Exception:
        db.rollback()
        raise

    release_vendor_edit_reservation_by_key(
        vendor_id=int(vendor.vendor_id),
        bearer_token=bearer,
    )
    return vendor

@router.get("/check-phone", response_model=VendorPhoneCheckResponse)
async def check_vendor_phone(
    mobile: str = Query(..., description="Phone / mobile number to look up"),
    db: Session = Depends(get_db),
):
    """
    Check if a vendor exists for the given phone number (no auth required).
  Handles 8086076193, 918086076193, +918086076193, etc.
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


@router.get("/check-email", response_model=VendorEmailCheckResponse)
async def check_vendor_email(
    email: str = Query(..., description="Business email to look up"),
    db: Session = Depends(get_db),
):
    """
    Check if a vendor exists for the given email (no auth required).
    Used by the set-password link page before creating a login account.
    """
    from services.po_vendor_scope import find_vendor_by_email

    normalized = normalize_vendor_email(email) or (email or "").strip()
    if not normalized:
        return VendorEmailCheckResponse(
            exists=False,
            email=email or "",
            message="Please enter a valid email address",
        )

    vendor = find_vendor_by_email(db, normalized)
    if not vendor:
        return VendorEmailCheckResponse(
            exists=False,
            email=normalized,
            message="No vendor found for this email address",
        )

    has_login = vendor_has_login(db, vendor)
    return VendorEmailCheckResponse(
        exists=True,
        email=normalized,
        vendor_id=vendor.vendor_id,
        status=vendor.status,
        vendor_registration_no=vendor.vendor_registration_no,
        company_name=vendor.company_name,
        has_login_account=has_login,
        message=(
            "Vendor already has a login account. Please sign in."
            if has_login
            else "Vendor found. You can set a password."
        ),
    )


@router.post("/setup-password-by-email")
async def setup_vendor_password_by_email(
    body: VendorSetupPasswordByEmailRequest,
    db: Session = Depends(get_db),
):
    """
    Create a vendor login account by email (no auth required).
    Intended for invite links after registration is approved/submitted.
    """
    from services.po_vendor_scope import find_vendor_by_email

    normalized = normalize_vendor_email(body.email) or (body.email or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="email is required",
        )

    vendor = find_vendor_by_email(db, normalized)
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No vendor found for this email address",
        )

    plain_password = (body.password or "").strip()
    if not plain_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="password is required",
        )

    # Prefer vendor-linked user; fall back to email match (JWT-provisioned rows).
    # Soft-fail when local `users` is Foundation-shaped (no email / hashed_password).
    existing_user = None
    try:
        existing_user = (
            db.query(User)
            .filter(User.vendor_id == vendor.vendor_id)
            .first()
        )
        if existing_user is None and vendor.email:
            existing_user = (
                db.query(User).filter(User.email == vendor.email).first()
            )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        if not _is_users_schema_mismatch(exc):
            raise
        existing_user = None

    if existing_user and user_password_is_set(existing_user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vendor already has a login account. Please sign in.",
        )

    create_vendor_user_in_foundation(
        vendor.email,
        plain_password,
        company_id=int(getattr(vendor, "company_id", None) or 3),
        tenant_id=int(getattr(vendor, "tenant_id", None) or 1),
        phone_number=getattr(vendor, "mobile", None),
        name=getattr(vendor, "company_name", None) or vendor.email,
    )

    hashed_password = get_password_hash(plain_password)
    try:
        if existing_user:
            existing_user.email = vendor.email
            existing_user.hashed_password = hashed_password
            existing_user.role = "vendor"
            existing_user.is_active = True
            existing_user.vendor_id = vendor.vendor_id
            db.commit()
            db.refresh(existing_user)
        else:
            create_user(
                db,
                email=vendor.email,
                hashed_password=hashed_password,
                role="vendor",
                is_active=True,
                vendor_id=vendor.vendor_id,
            )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        if not _is_users_schema_mismatch(exc):
            raise
        # Foundation account is already created; local users sync is best-effort.

    return {
        "message": "Password setup successful. You can now login.",
        "vendor_id": vendor.vendor_id,
        "email": vendor.email,
    }


_PASSWORD_STRENGTH_RE = re.compile(
    r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9\s]).{8,}$"
)
_FORGOT_PASSWORD_GENERIC_MESSAGE = (
    "If an account exists for that email, check your messages for a reset link, "
    "or try again with the email you used to register."
)
_FORGOT_PASSWORD_LINK_MESSAGE = (
    "An account exists for that email, a password reset link is available. "
    "Follow the link to choose a new password."
)


def _assert_password_strength(plain_password: str) -> str:
    password = (plain_password or "").strip()
    if not _PASSWORD_STRENGTH_RE.match(password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Password must be at least 8 characters and include uppercase, "
                "lowercase, number, and special character"
            ),
        )
    return password


def _resolve_vendor_login_user(db: Session, *, email: str, vendor) -> Optional[User]:
    existing_user = (
        db.query(User)
        .filter(User.vendor_id == vendor.vendor_id)
        .first()
    )
    if existing_user is None:
        existing_user = db.query(User).filter(User.email == email).first()
    return existing_user


@router.post("/forgot-password", response_model=VendorForgotPasswordResponse)
async def forgot_vendor_password(
    body: VendorForgotPasswordRequest,
    db: Session = Depends(get_db),
):
    """
    Start password reset for a vendor login.

    Always returns a generic success message (no account enumeration).
    When PASSWORD_RESET_INCLUDE_LINK is enabled, also returns reset_url for the UI
    (useful until SMTP/WhatsApp delivery is wired).
    """
    from services.po_vendor_scope import find_vendor_by_email
    from config import settings

    normalized = normalize_vendor_email(body.email) or (body.email or "").strip().lower()
    response = VendorForgotPasswordResponse(message=_FORGOT_PASSWORD_GENERIC_MESSAGE)

    if not normalized:
        return response

    vendor = find_vendor_by_email(db, normalized)
    if not vendor:
        return response

    existing_user = _resolve_vendor_login_user(db, email=vendor.email or normalized, vendor=vendor)
    if not user_password_is_set(existing_user):
        return response

    token, expires_in = create_password_reset_token(
        user_id=int(existing_user.user_id),
        email=vendor.email or normalized,
        vendor_id=int(vendor.vendor_id) if vendor.vendor_id is not None else None,
    )
    reset_url = build_password_reset_url(token)
    import logging

    logging.getLogger(__name__).info(
        "[password-reset] Issued reset token for vendor_id=%s email=%s expires_in=%s",
        vendor.vendor_id,
        vendor.email,
        expires_in,
    )

    # Best-effort WhatsApp delivery when a mobile number is on file.
    try:
        mobile = (getattr(vendor, "mobile", None) or "").strip()
        if mobile:
            from services.whatsapp_notify_service import (
                WhatsAppNotifyService,
                normalize_whatsapp_phone,
            )

            recipient = normalize_whatsapp_phone(mobile)
            if recipient:
                msg = (
                    "Password reset requested for your vendor account.\n"
                    f"Reset link (expires in {max(1, expires_in // 60)} min):\n{reset_url}"
                )
                WhatsAppNotifyService(log_prefix="PASSWORD-RESET").send_text_meta(
                    to=recipient,
                    text=msg,
                )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "[password-reset] WhatsApp notify failed: %s", exc
        )

    if bool(getattr(settings, "password_reset_include_link", True)):
        response.message = _FORGOT_PASSWORD_LINK_MESSAGE
        response.reset_url = reset_url
        response.expires_in = expires_in

    return response


@router.post("/reset-password", response_model=VendorResetPasswordResponse)
async def reset_vendor_password(
    body: VendorResetPasswordRequest,
    db: Session = Depends(get_db),
):
    """Consume a password-reset token and set a new password (local + Foundation)."""
    from services.po_vendor_scope import find_vendor_by_email

    payload = consume_password_reset_token(body.token)
    plain_password = _assert_password_strength(body.password)
    email = (payload.get("email") or "").strip().lower()

    vendor = find_vendor_by_email(db, email)
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No vendor found for this reset link",
        )

    existing_user = _resolve_vendor_login_user(
        db, email=vendor.email or email, vendor=vendor
    )
    if existing_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No login account found for this reset link",
        )

    token_user_id = payload.get("user_id")
    if token_user_id is not None and int(existing_user.user_id) != int(token_user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset link.",
        )

    create_vendor_user_in_foundation(
        vendor.email,
        plain_password,
        company_id=int(getattr(vendor, "company_id", None) or 3),
        tenant_id=int(getattr(vendor, "tenant_id", None) or 1),
        phone_number=getattr(vendor, "mobile", None),
        name=getattr(vendor, "company_name", None) or vendor.email,
    )

    existing_user.email = vendor.email
    existing_user.hashed_password = get_password_hash(plain_password)
    existing_user.role = "vendor"
    existing_user.is_active = True
    existing_user.vendor_id = vendor.vendor_id
    db.commit()

    return VendorResetPasswordResponse(
        message="Password reset successful. You can now sign in.",
        email=vendor.email,
    )


# FIXED: Changed to accept query parameters instead of JSON body
@router.get("/start-registration")
async def start_vendor_registration(
    email: str,
    mobile: str,
    tenant_id: int = Query(1),
    company_id: int = Query(3),
    company_code: Optional[str] = Query(None),
    vendor_registration_no: Optional[str] = Query(None),
    created_by_a: Optional[str] = Query(None),
    created_ip_a: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    # Check if vendor already exists
    company_id = resolve_company_id(company_id)
    email = normalize_vendor_email(email) or (email or "").strip()
    mobile = (mobile or "").strip()
    from services.po_vendor_scope import find_vendor_by_email
    from services.vendor_phone_lookup import find_vendor_by_phone

    existing_by_email = find_vendor_by_email(db, email) if email else None
    existing_by_mobile = find_vendor_by_phone(db, mobile) if mobile else None

    # Resume only when the same email already has a vendor row.
    # Never reuse another vendor solely because the mobile matches — that overwrites
    # the previous company's name/details on complete-registration.
    if existing_by_email:
        return {
            "vendor_id": existing_by_email.vendor_id,
            "vendor_registration_no": existing_by_email.vendor_registration_no,
            "message": "Vendor already exists. You can continue with registration.",
            "status": existing_by_email.status,
        }

    if existing_by_mobile is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This mobile number is already registered to another vendor. "
                "Use a different mobile number, or continue with that vendor's email."
            ),
        )
    
    try:
        # Self-service register: draft row only — vendor_code is minted on approval.
        _ = vendor_registration_no
        db_vendor = Vendor(
            email=email,
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

        db.add(db_vendor)
        db.commit()
        db.refresh(db_vendor)

        return {
            "vendor_id": db_vendor.vendor_id,
            "vendor_registration_no": db_vendor.vendor_registration_no,
            "vendor_code": db_vendor.vendor_code,
            "message": (
                "Draft vendor created. Complete registration; "
                "vendor code is assigned when approved."
            ),
        }
    except IntegrityError as e:
        db.rollback()
        # Check if it's a duplicate key violation (primary key or unique constraint)
        error_str = str(e.orig) if hasattr(e, 'orig') else str(e)
        if "duplicate key" in error_str.lower() or "unique" in error_str.lower():
            # Try to find the existing vendor by email only (never by mobile alone).
            from services.po_vendor_scope import find_vendor_by_email
            from services.vendor_phone_lookup import find_vendor_by_phone

            existing_vendor = find_vendor_by_email(db, email) if email else None
            if existing_vendor:
                return {
                    "vendor_id": existing_vendor.vendor_id,
                    "vendor_registration_no": existing_vendor.vendor_registration_no,
                    "message": "Vendor already exists. You can continue with registration.",
                    "status": existing_vendor.status
                }
            mobile_owner = find_vendor_by_phone(db, mobile) if mobile else None
            if mobile_owner is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "This mobile number is already registered to another vendor. "
                        "Use a different mobile number, or continue with that vendor's email."
                    ),
                )
            # If duplicate key on vendor_id, fix the sequence and retry
            if "vendor_id" in error_str or "vendors_pkey" in error_str:
                try:
                    # Get the maximum vendor_id from the database
                    max_id_result = db.query(func.max(Vendor.vendor_id)).scalar()
                    max_id = max_id_result if max_id_result else 0
                    next_id = max_id + 1
                    
                    # Try to reset the sequence (sequence name might vary)
                    sequence_names = [
                        'vendors_vendor_id_seq',
                        'vendor_id_seq',
                        f'"{Vendor.__tablename__}_vendor_id_seq"'
                    ]
                    
                    sequence_fixed = False
                    for seq_name in sequence_names:
                        try:
                            db.execute(text(f"SELECT setval('{seq_name}', {next_id}, false)"))
                            db.commit()
                            sequence_fixed = True
                            break
                        except Exception:
                            db.rollback()
                            continue
                    
                    # If sequence reset worked, retry creating the vendor
                    if sequence_fixed:
                        db_vendor = Vendor(
                            email=email,
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
                        db.add(db_vendor)
                        db.commit()
                        db.refresh(db_vendor)
                        return {
                            "vendor_id": db_vendor.vendor_id,
                            "vendor_registration_no": db_vendor.vendor_registration_no,
                            "vendor_code": db_vendor.vendor_code,
                            "message": (
                                "Draft vendor created. Complete registration; "
                                "vendor code is assigned when approved."
                            ),
                        }

                    # If sequence doesn't exist or couldn't be fixed, manually set vendor_id
                    db_vendor = Vendor(
                        vendor_id=next_id,
                        email=email,
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
                    db.add(db_vendor)
                    db.commit()
                    db.refresh(db_vendor)

                    return {
                        "vendor_id": db_vendor.vendor_id,
                        "vendor_registration_no": db_vendor.vendor_registration_no,
                        "vendor_code": db_vendor.vendor_code,
                        "message": (
                            "Draft vendor created. Complete registration; "
                            "vendor code is assigned when approved."
                        ),
                    }
                except Exception as fix_error:
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Database sequence error. Could not fix automatically: {str(fix_error)}"
                    )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Database constraint violation: {error_str}"
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating vendor: {str(e)}"
        )

@router.post("/{vendor_id}/verify-otp")
async def verify_vendor_otp(
    vendor_id: int,
    otp_verify: OTPVerify,
    db: Session = Depends(get_db)
):
    # Verify OTP
    otp = db.query(OTP).filter(
        OTP.email == otp_verify.email,
        OTP.mobile == otp_verify.mobile,
        OTP.otp_code == otp_verify.otp_code,
        OTP.expires_at > datetime.utcnow(),
        OTP.is_used == False
    ).first()
    
    if not otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OTP"
        )
    
    # Mark OTP as used
    otp.is_used = True
    
    # Update vendor status to allow completion
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if vendor:
        vendor.status = "otp_verified"
    
    db.commit()
    
    return {"message": "OTP verified successfully. You can now complete registration."}

_GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_PAN_RE = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")

def _registration_submit_errors(vendor: Vendor) -> List[str]:
    """Completeness rules for Submit: company name, email and mobile are required.

    Bank accounts and documents are not required to submit.
    """
    errors: List[str] = []
    if not (vendor.company_name or "").strip():
        errors.append("Company name is required")
    if not (vendor.email or "").strip():
        errors.append("Email is required")
    if not (vendor.mobile or "").strip():
        errors.append("Mobile is required")
    gst = (vendor.gst_number or "").strip().upper()
    if gst and not _GSTIN_RE.match(gst):
        errors.append("GST number is invalid")
    pan = (vendor.pan_number or "").strip().upper()
    if pan and not _PAN_RE.match(pan):
        errors.append("PAN is invalid")
    return errors


_BANK_REQUIRED_FIELDS = (
    ("bank_name", "bank name"),
    ("account_number", "account number"),
    ("ifsc_code", "IFSC code"),
    ("branch_name", "branch name"),
    ("account_type", "account type"),
)


def _registration_row_errors(application: VendorApplication, sent: set) -> List[str]:
    """Submit-only checks for the bank / document rows the caller sent."""
    errors: List[str] = []
    if "banks" in sent:
        for index, bank in enumerate(application.banks, start=1):
            missing = [
                label
                for field, label in _BANK_REQUIRED_FIELDS
                if not (getattr(bank, field) or "").strip()
            ]
            if missing:
                errors.append(f"Bank {index}: {', '.join(missing)} required")
    if "documents" in sent:
        for index, doc in enumerate(application.documents, start=1):
            if not (doc.document_type or "").strip():
                errors.append(f"Document {index}: document type required")
    return errors


def _bank_row_is_empty(bank) -> bool:
    return not any(
        (getattr(bank, field) or "").strip() for field, _ in _BANK_REQUIRED_FIELDS
    )


def _assert_registration_email_valid(db: Session, *, vendor_id: int, email: str) -> None:
    """Format + uniqueness check for complete-registration (skipped for drafts)."""
    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid email address: {exc}",
        ) from exc
    normalized = normalize_vendor_email(email)
    conflict = (
        db.query(Vendor.vendor_id)
        .filter(func.lower(Vendor.email) == normalized, Vendor.vendor_id != vendor_id)
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already used by another vendor",
        )


@router.post("/{vendor_id}/complete-registration", response_model=VendorProfileOut)
async def complete_vendor_registration(
    vendor_id: int,
    application: VendorApplication,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _ = current_user  # Auth required so reservation assert uses the same JWT.
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found"
        )

    bearer = _bearer_token_from_request(request)
    assert_vendor_edit_reservation(
        vendor_id=int(vendor_id),
        bearer_token=bearer,
    )
    
    # TEMP: Bypass OTP verification for demo flow
    # TODO: Reinstate this check when real OTP is implemented
    # if vendor.status != "otp_verified":
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail="OTP verification required before completing registration"
    #     )
    
    try:
        old_email = vendor.email
        # Update vendor details from application.vendor (includes email/mobile if provided)
        vendor_data = application.vendor.dict(exclude_unset=True)
        requested_status = str(vendor_data.pop("status", None) or "").strip().upper()
        is_draft = requested_status == "DRAFT"
        payload_email = (str(vendor_data.get("email") or "").strip() or None)

        # Drafts are stored as typed; uniqueness, email format and login-email
        # sync run on Submit (or any non-draft save).
        if not is_draft:
            # Guard: never attach a mobile that already belongs to a different vendor.
            payload_mobile = (str(vendor_data.get("mobile") or "").strip() or None)
            if payload_mobile:
                from services.vendor_phone_lookup import find_other_vendor_by_phone

                mobile_owner = find_other_vendor_by_phone(
                    db, payload_mobile, exclude_vendor_id=int(vendor_id)
                )
                if mobile_owner is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            "This mobile number is already registered to another vendor. "
                            "Use a different mobile number."
                        ),
                    )

            if payload_email:
                _assert_registration_email_valid(db, vendor_id=int(vendor_id), email=payload_email)
                # A draft may already have stored this email on the vendor, so
                # compare against the login account's email to still sync it.
                # Local `users` on this database often has no email column (Foundation shape).
                login_email = None
                try:
                    login_user = (
                        db.query(User)
                        .filter(User.vendor_id == vendor.vendor_id)
                        .first()
                    )
                    login_email = login_user.email if login_user else None
                except Exception as exc:
                    if not _is_users_schema_mismatch(exc):
                        raise
                    db.rollback()
                    print(
                        "[complete-registration] local users.email missing; "
                        f"saving vendor_id={vendor_id} without login-row lookup"
                    )
                sync_vendor_email_change(
                    db,
                    vendor=vendor,
                    old_email=login_email or old_email,
                    new_email=payload_email,
                )

        # Never let the registration payload overwrite org / numbering fields.
        # Also block accidental clear / placeholder overwrite of company_name etc.
        protected_vendor_fields = {
            "vendor_id",
            "vendor_code",
            "vendor_registration_no",
            "company_id",
            "tenant_id",
            "status",
            "created_at",
            "created_by_a",
        }
        safe_vendor_data = protect_vendor_update_payload(vendor, vendor_data)
        for key, value in safe_vendor_data.items():
            if key in protected_vendor_fields:
                continue
            if key == "email" and value is not None:
                value = normalize_vendor_email(str(value))
            if hasattr(vendor, key):
                setattr(vendor, key, value)
        
        # Save Draft / Submit send DRAFT / SUBMITTED; older callers keep "pending".
        # Resubmit after ACTIVE/APPROVED also lands on SUBMITTED so approval restarts.
        vendor.status = (
            requested_status
            if requested_status in REGISTRATION_STATUSES
            else "pending"
        )
        vendor.modified_on_a = datetime.utcnow()

        # Only replace the collections the caller sent, so a partial save
        # (e.g. master + banks) does not wipe items, rates or documents.
        sent = getattr(application, "model_fields_set", None) or application.__fields_set__

        is_submit = requested_status == "SUBMITTED"
        if is_submit:
            errors = _registration_submit_errors(vendor) + _registration_row_errors(
                application, sent
            )
            if errors:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="; ".join(errors),
                )

        if "banks" in sent:
            db.query(VendorBank).filter(VendorBank.vendor_id == vendor.vendor_id).delete(synchronize_session=False)
            for bank in application.banks:
                if _bank_row_is_empty(bank):
                    continue
                # vendor_banks columns are NOT NULL: drafts store missing values as "".
                db.add(
                    VendorBank(
                        vendor_id=vendor.vendor_id,
                        bank_name=(bank.bank_name or "").strip(),
                        account_number=(bank.account_number or "").strip(),
                        ifsc_code=(bank.ifsc_code or "").strip().upper(),
                        branch_name=(bank.branch_name or "").strip(),
                        account_type=(bank.account_type or "").strip(),
                        is_primary=bool(bank.is_primary),
                    )
                )

        if "items" in sent:
            # vendor_rates references vendor_items — delete rates before items
            item_ids_subq = db.query(VendorItem.id).filter(
                VendorItem.vendor_id == vendor.vendor_id
            )
            db.query(VendorRate).filter(VendorRate.item_id.in_(item_ids_subq)).delete(
                synchronize_session=False
            )
            db.query(VendorItem).filter(VendorItem.vendor_id == vendor.vendor_id).delete(
                synchronize_session=False
            )
            for item in application.items:
                db_item = VendorItem(vendor_id=vendor.vendor_id, **item.dict())
                _stamp_vendor_item_audit(db_item, vendor=vendor)
                _apply_vendor_item_submit_rules(db_item, draft=not is_submit)
                _ensure_vendor_item_required_fields(db_item, vendor=vendor)
                db.add(db_item)

        if "documents" in sent:
            db.query(VendorDocument).filter(VendorDocument.vendor_id == vendor.vendor_id).delete(synchronize_session=False)
            for doc in application.documents:
                db.add(VendorDocument(vendor_id=vendor.vendor_id, **doc.dict()))

        db.commit()
        db.refresh(vendor)
    except Exception as _exc:
        db.rollback()
        raise

    # Save Draft and Submit both finish the edit: drop the vendor's object_reservation row.
    release_vendor_edit_reservation_by_key(
        vendor_id=int(vendor_id),
        bearer_token=bearer,
    )
    return vendor


@router.post("/{vendor_id}/setup-password")
async def setup_vendor_password(
    vendor_id: int,
    password_data: dict,  # {"password": "securepassword"}
    db: Session = Depends(get_db)
):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found"
        )
    
    # Check if vendor already has a usable login (non-empty password).
    existing_user = db.query(User).filter(User.email == vendor.email).first()
    if existing_user and user_password_is_set(existing_user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vendor already has a login account. Please sign in.",
        )

    plain_password = password_data.get("password") or ""
    if not plain_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="password is required",
        )

    # Create Foundation user + UserRole(vendor) before local account
    create_vendor_user_in_foundation(
        vendor.email,
        plain_password,
        company_id=int(getattr(vendor, "company_id", None) or 3),
        tenant_id=int(getattr(vendor, "tenant_id", None) or 1),
        phone_number=getattr(vendor, "mobile", None),
        name=getattr(vendor, "company_name", None) or vendor.email,
    )
    
    # Create or fill local user account for vendor
    hashed_password = get_password_hash(plain_password)
    if existing_user:
        existing_user.hashed_password = hashed_password
        existing_user.role = "vendor"
        existing_user.is_active = True
        existing_user.vendor_id = vendor.vendor_id
        db.commit()
        db.refresh(existing_user)
    else:
        create_user(
            db,
            email=vendor.email,
            hashed_password=hashed_password,
            role="vendor",
            is_active=True,
            vendor_id=vendor.vendor_id,
        )

    return {"message": "Password setup successful. Vendor can now login."}


def _resolved_vendor_id_or_400(db, current_user, vendor_id: int | None = None) -> int:
    """Resolve vendor_id for data access (not a role gate)."""
    if vendor_id is not None:
        return int(vendor_id)
    vendor = resolve_vendor_for_user(db, current_user)
    if vendor is not None:
        return int(vendor.vendor_id)
    if current_user.vendor_id is not None:
        return int(current_user.vendor_id)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="vendor_id is required (link a vendor on the user or pass vendor_id)",
    )

# Document Management (placed above dynamic '/{vendor_id}' routes)
@router.get("/documents", response_model=List[VendorDocumentSchema])
async def get_vendor_documents(
    vendor_id: Optional[int] = Query(
        None, description="Target vendor_id (required when actor has no linked vendor)."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    vid = _resolved_vendor_id_or_400(db, current_user, vendor_id)
    docs = db.query(VendorDocument).filter(
        VendorDocument.vendor_id == vid
    ).all()

    return docs


@router.get("/documents/{doc_id}/file")
async def download_vendor_document_file(
    doc_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):

    _ = current_user  # Access via permissions API; no vendor ownership gate.
    db_doc = (
        db.query(VendorDocument)
        .filter(VendorDocument.id == doc_id)
        .first()
    )
    if not db_doc or not db_doc.document_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    try:
        file_path = resolve_document_file_path(db_doc.document_path)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document file missing on server: {db_doc.document_path}",
        ) from exc

    media_type = "application/pdf"
    suffix = file_path.suffix.lower()
    if suffix in (".png",):
        media_type = "image/png"
    elif suffix in (".jpg", ".jpeg"):
        media_type = "image/jpeg"

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=file_path.name,
    )


@router.post("/documents", response_model=VendorDocumentSchema, status_code=status.HTTP_201_CREATED)
async def upload_vendor_document(
    document_type: str = Form(...),
    document_number: Optional[str] = Form(None),
    issue_date: Optional[str] = Form(None),
    expiry_date: Optional[str] = Form(None),
    file: UploadFile = File(...),
    vendor_id: Optional[int] = Query(
        None, description="Target vendor_id (required when actor has no linked vendor)."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    resolved_vendor_id = _resolved_vendor_id_or_400(db, current_user, vendor_id)

    # Prepare upload path
    base_dir = vendor_documents_dir(resolved_vendor_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    file_ext = os.path.splitext(file.filename or "")[1]
    safe_name = f"{uuid.uuid4().hex}{file_ext}"
    dest_path = base_dir / safe_name

    with open(dest_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    public_path = to_public_document_path(str(dest_path))

    # Parse dates if provided
    def parse_date(value: Optional[str]) -> Optional[date]:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except Exception:
            return None

    db_doc = VendorDocument(
        vendor_id=resolved_vendor_id,
        document_type=document_type,
        document_number=document_number,
        issue_date=parse_date(issue_date),
        expiry_date=parse_date(expiry_date),
        document_path=public_path,
        status="pending",
    )

    db.add(db_doc)
    db.commit()
    db.refresh(db_doc)

    return db_doc

@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vendor_document(
    doc_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    _ = current_user  # Access via permissions API; no vendor ownership gate.
    db_doc = db.query(VendorDocument).filter(
        VendorDocument.id == doc_id,
    ).first()

    if not db_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found"
        )

    # Attempt to remove file from disk (ignore errors)
    try:
        if db_doc.document_path and os.path.exists(db_doc.document_path):
            os.remove(db_doc.document_path)
    except Exception:
        pass

    db.delete(db_doc)
    db.commit()

    return Response(status_code=status.HTTP_204_NO_CONTENT)

# Bank Account Management (moved above dynamic '/{vendor_id}' routes to avoid 422)
@router.get("/banks", response_model=List[VendorBankSchema])
async def get_vendor_banks(
    vendor_id: Optional[int] = Query(
        None, description="Target vendor_id (required when actor has no linked vendor)."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get bank accounts for a vendor. Access is permissions-API driven."""
    resolved_vendor_id = _resolved_vendor_id_or_400(db, current_user, vendor_id)
    banks = db.query(VendorBank).filter(
        VendorBank.vendor_id == resolved_vendor_id
    ).all()
    
    return banks

@router.post("/banks", response_model=VendorBankSchema, status_code=status.HTTP_201_CREATED)
async def create_vendor_bank(
    bank: VendorBankCreate,
    vendor_id: Optional[int] = Query(
        None, description="Target vendor_id (required when actor has no linked vendor)."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a bank account for a vendor. Access is permissions-API driven."""
    resolved_vendor_id = _resolved_vendor_id_or_400(db, current_user, vendor_id)
    
    # If setting as primary, unset other primary accounts
    if bank.is_primary:
        db.query(VendorBank).filter(
            VendorBank.vendor_id == resolved_vendor_id,
            VendorBank.is_primary == True
        ).update({"is_primary": False})
    
    db_bank = VendorBank(
        **bank.dict(),
        vendor_id=resolved_vendor_id
    )
    
    db.add(db_bank)
    db.commit()
    db.refresh(db_bank)
    
    return db_bank

@router.put("/banks/{bank_id}", response_model=VendorBankSchema)
async def update_vendor_bank(
    bank_id: int,
    bank_update: VendorBankBase,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a vendor bank account. Access is permissions-API driven."""
    _ = current_user
    db_bank = db.query(VendorBank).filter(
        VendorBank.id == bank_id,
    ).first()
    
    if not db_bank:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bank account not found"
        )
    
    # If setting as primary, unset other primary accounts for the same vendor
    if bank_update.is_primary:
        db.query(VendorBank).filter(
            VendorBank.vendor_id == db_bank.vendor_id,
            VendorBank.id != bank_id,
            VendorBank.is_primary == True
        ).update({"is_primary": False})
    
    for key, value in bank_update.dict(exclude_unset=True).items():
        setattr(db_bank, key, value)
    
    db_bank.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_bank)
    
    return db_bank

@router.delete("/banks/{bank_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vendor_bank(
    bank_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a vendor bank account. Access is permissions-API driven."""
    _ = current_user
    db_bank = db.query(VendorBank).filter(
        VendorBank.id == bank_id,
    ).first()
    
    if not db_bank:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Bank account not found"
        )
    
    db.delete(db_bank)
    db.commit()
    
    return {"message": "Bank account deleted successfully"}

# Item Management
@router.get("/items", response_model=List[VendorItemResponse])
async def get_vendor_items(
    user_id: Optional[int] = Query(
        None,
        description="Optional vendor_id override (query alias used by FE client).",
    ),
    vendor_id: Optional[int] = Query(
        None,
        description="Target vendor_id (required when actor has no linked vendor).",
    ),
    category: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="Filter by item status (e.g. Active)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get all items for a vendor (includes rates), ordered by latest rate date.
    Access is permissions-API driven.
    """
    target_vendor_id = _resolved_vendor_id_or_400(
        db, current_user, vendor_id if vendor_id is not None else user_id
    )

    items_q = (
        db.query(VendorItem)
        .options(joinedload(VendorItem.rates))
        .filter(VendorItem.vendor_id == target_vendor_id)
    )
    if category:
        items_q = items_q.filter(VendorItem.category.ilike(f"%{category}%"))
    if brand:
        items_q = items_q.filter(VendorItem.brand.ilike(f"%{brand}%"))
    if status:
        items_q = items_q.filter(VendorItem.status.ilike(status))

    items = items_q.all()

    # Keep each item's rates newest-first and sort items by each item's latest rate date.
    def _rate_sort_key(r: VendorRate) -> datetime:
        if r.valid_from is not None:
            return datetime.combine(r.valid_from, datetime.min.time())
        if r.created_at is not None:
            return r.created_at
        return datetime.min

    for item in items:
        item.rates = sorted(item.rates or [], key=_rate_sort_key, reverse=True)

    items = sorted(
        items,
        key=lambda it: _rate_sort_key(it.rates[0]) if it.rates else datetime.min,
        reverse=True,
    )

    return items


def _request_client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    if request.client and request.client.host:
        return request.client.host[:45]
    return None


def _stamp_vendor_item_audit(
    db_item: VendorItem,
    *,
    user: Optional[User] = None,
    vendor: Optional[Vendor] = None,
    request: Optional[Request] = None,
    is_update: bool = False,
) -> None:
    uid = str(user.user_id) if user else None
    ip = _request_client_ip(request) if request else None
    now = datetime.utcnow()
    if is_update:
        if uid:
            db_item.modified_by = uid
        if ip:
            db_item.modified_ip = ip
        db_item.updated_at = now
    else:
        if uid:
            db_item.created_by = uid
        if ip:
            db_item.created_ip = ip
        if vendor:
            if db_item.tenant_id is None and vendor.tenant_id is not None:
                db_item.tenant_id = str(vendor.tenant_id)
            # company_code is the Foundation product code, set on item confirm.
            code = ensure_vendor_code_from_registration(vendor)
            if db_item.vendor_code is None and code:
                db_item.vendor_code = code
            if not db_item.status:
                db_item.status = "Active"


_VENDOR_ITEM_TEXT_LIMITS = {
    "item_name": 255,
    "item_type": 50,
    "uom": 50,
    "erp_code": 20,
    "vendor_product_code": 20,
    "barcode": 100,
    "brand": 100,
    "manufacturer": 255,
    "product_name": 255,
    "category": 100,
    "consumer_unit": 20,
    "consumer_unit_size": 50,
    "purchase_unit": 20,
    "status": 20,
    "vendor_code": 20,
    "tenant_id": 50,
    "company_code": 50,
}


def _apply_vendor_item_submit_rules(db_item: VendorItem, *, draft: bool) -> None:
    """
    Save Draft: no checks; over-long text is clipped to the column size.
    Submit: item name is required and over-long text is rejected (422).
    Call before `_ensure_vendor_item_required_fields`, which backfills the name.
    """
    errors: List[str] = []
    for field, limit in _VENDOR_ITEM_TEXT_LIMITS.items():
        value = getattr(db_item, field, None)
        if isinstance(value, str) and len(value) > limit:
            if draft:
                setattr(db_item, field, value[:limit])
            else:
                errors.append(f"{field} must be at most {limit} characters")
    if draft:
        return
    if not ((db_item.product_name or "").strip() or (db_item.item_name or "").strip()):
        errors.insert(0, "Item name is required")
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="; ".join(errors),
        )


def _ensure_vendor_item_required_fields(
    db_item: VendorItem,
    *,
    vendor: Optional[Vendor] = None,
) -> None:
    """Backfill vendor_items fields that are now NOT NULL in the database."""
    product_name = (
        (db_item.product_name or "").strip()
        or (db_item.item_name or "").strip()
        or (db_item.description or "").strip()
        or "UNKNOWN PRODUCT"
    )
    db_item.product_name = product_name[:255]

    brand = ((db_item.brand or "").strip() or (db_item.manufacturer or "").strip() or product_name)
    db_item.brand = brand[:100]

    manufacturer = (
        (db_item.manufacturer or "").strip()
        or (db_item.brand or "").strip()
        or brand
    )
    db_item.manufacturer = manufacturer[:255]

    category = ((db_item.category or "").strip() or "UNCATEGORIZED")
    db_item.category = category[:100]

    if not (db_item.vendor_code or "").strip() and vendor:
        code = ensure_vendor_code_from_registration(vendor)
        if code:
            db_item.vendor_code = code

    if db_item.pack_size is None:
        db_item.pack_size = 1

    # CHAR(3) UOM codes — trim/pad-safe clip
    if db_item.uom_p is not None:
        db_item.uom_p = (str(db_item.uom_p).strip()[:3] or None)
    if db_item.uom_s is not None:
        db_item.uom_s = (str(db_item.uom_s).strip()[:3] or None)


def _load_item_with_rates(db: Session, item_id: int) -> Optional[VendorItem]:
    return (
        db.query(VendorItem)
        .options(joinedload(VendorItem.rates))
        .filter(VendorItem.id == item_id)
        .first()
    )


@router.post(
    "/rate-contract-match/confirm",
    response_model=RateContractMatchConfirmResponse,
)
async def confirm_rate_contract_product_match(
    body: RateContractMatchConfirmRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply user-selected vendor catalog match for an ambiguous rate-contract OCR line."""
    _ = current_user  # Access via permissions API (FE deny-list); no vendor-role gate.
    result = confirm_rate_contract_line_match(
        db,
        vendor_id=int(body.vendorId),
        line_index=int(body.lineIndex),
        selected_vendor_item_id=body.selectedVendorItemId,
        ocr_line=body.ocrLine,
        create_alias=body.createAlias,
        create_uom_parameter=body.createUomParameter,
        confirmed_uom_code=body.confirmedUomCode,
    )
    return RateContractMatchConfirmResponse(**result)


@router.post(
    "/items/{item_id}/confirm",
    response_model=VendorItemConfirmResponse,
)
async def confirm_vendor_catalog_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Confirm a pending vendor catalog item (status → CONFIRMED).
    Generates the item's vendor_product_code, erp_code and company_code.
    """
    # Access via permissions API; buyers and vendors may confirm.
    result = confirm_vendor_item_status(
        db,
        vendor_item_id=item_id,
        created_by=getattr(current_user, "user_id", None),
    )
    return VendorItemConfirmResponse(**result)


def _vendor_item_integrity_detail(action: str, exc: IntegrityError) -> str:
    """User-facing message naming the column / constraint that failed."""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    column = getattr(diag, "column_name", None)
    constraint = getattr(diag, "constraint_name", None)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode == "23505":
        if constraint and "barcode" in constraint:
            return f"{action}: this barcode is already used by another item."
        return f"{action}: duplicate value ({constraint or 'unique constraint'})."
    if pgcode == "23502" and column:
        if column == "vendor_code":
            return f"{action}: vendor is not approved yet (no vendor code)."
        return f"{action}: {column} is required by the database."
    reason = (getattr(diag, "message_primary", None) or str(orig or exc)).splitlines()[0]
    return f"{action}: {reason}"


@router.post("/items", response_model=VendorItemResponse, status_code=status.HTTP_201_CREATED)
async def create_vendor_item(
    item: VendorItemCreate,
    request: Request,
    vendor_id: Optional[int] = Query(
        None, description="Target vendor_id (required when actor has no linked vendor)."
    ),
    draft: bool = Query(
        False, description="Save Draft: no validation or duplicate check."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Create a new item for a vendor. Access is permissions-API driven.
    Saved as PENDING; codes are generated when the item is confirmed.
    """
    resolved_vendor_id = _resolved_vendor_id_or_400(db, current_user, vendor_id)
    vendor = db.query(Vendor).filter(Vendor.vendor_id == resolved_vendor_id).first()
    db_item = VendorItem(
        **item.dict(),
        vendor_id=resolved_vendor_id,
    )
    db_item.status = VENDOR_ITEM_STATUS_PENDING
    _stamp_vendor_item_audit(
        db_item, user=current_user, vendor=vendor, request=request
    )
    _apply_vendor_item_submit_rules(db_item, draft=draft)
    _ensure_vendor_item_required_fields(db_item, vendor=vendor)

    # Manually added item: no existing/confirmed-product lookup is required.
    apply_canonical_pack_size_to_item(db_item)
    if not draft:
        assert_no_duplicate_vendor_item(db, db_item)

    try:
        db.add(db_item)
        db.flush()
        if db_item.item_id is None and db_item.id is not None:
            db_item.item_id = int(db_item.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_vendor_item_integrity_detail("Could not create item", exc),
        )

    db.refresh(db_item)

    return _load_item_with_rates(db, db_item.id)


@router.post(
    "/items/with-rate",
    response_model=VendorItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_vendor_item_with_rate(
    body: VendorItemWithRateCreate,
    request: Request,
    vendor_id: Optional[int] = Query(
        None, description="Target vendor_id (required when actor has no linked vendor)."
    ),
    draft: bool = Query(
        False, description="Save Draft: no validation or duplicate check (rate may be blank)."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a vendor item and one vendor rate in a single transaction.
    Access is permissions-API driven.
    Saved as PENDING; codes are generated when the item is confirmed.
    """

    rate_body = body.rate
    if not draft and rate_body.rate is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rate.rate is required",
        )

    resolved_vendor_id = _resolved_vendor_id_or_400(db, current_user, vendor_id)
    item_payload = body.dict(exclude={"rate"})
    vendor = db.query(Vendor).filter(Vendor.vendor_id == resolved_vendor_id).first()
    try:
        db_item = VendorItem(
            **item_payload,
            vendor_id=resolved_vendor_id,
        )
        db_item.status = VENDOR_ITEM_STATUS_PENDING
        _stamp_vendor_item_audit(
            db_item, user=current_user, vendor=vendor, request=request
        )
        _apply_vendor_item_submit_rules(db_item, draft=draft)
        _ensure_vendor_item_required_fields(db_item, vendor=vendor)

        # Manually added item: no existing/confirmed-product lookup is required.
        apply_canonical_pack_size_to_item(db_item)
        if not draft:
            assert_no_duplicate_vendor_item(db, db_item)

        db.add(db_item)
        db.flush()
        if db_item.item_id is None and db_item.id is not None:
            db_item.item_id = int(db_item.id)

        # A draft may leave the rate blank; the rate row is added on a later save.
        if rate_body.rate is not None:
            db_rate = VendorRate(
                item_id=db_item.id,
                vendor_id=db_item.vendor_id,
                rate=rate_body.rate,
                currency=rate_body.currency or "INR",
                valid_from=rate_body.valid_from,
                valid_to=rate_body.valid_to,
                status=rate_body.status or "pending",
            )
            db.add(db_rate)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        import logging

        logging.getLogger(__name__).warning(
            "[vendor-item-with-rate] vendor_id=%s product=%r barcode=%r: %s",
            resolved_vendor_id,
            body.product_name,
            body.barcode,
            getattr(exc, "orig", exc),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_vendor_item_integrity_detail("Could not create item or rate", exc),
        )

    return _load_item_with_rates(db, db_item.id)


@router.put("/items/{item_id}", response_model=VendorItemResponse)
async def update_vendor_item(
    item_id: int,
    item_update: VendorItemUpdate,
    request: Request,
    draft: bool = Query(
        False, description="Save Draft: skip the duplicate-product check (runs on Submit)."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Update an item for the current vendor
    """
    db_item = db.query(VendorItem).filter(VendorItem.id == item_id).first()

    if not db_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not found"
        )

    for key, value in item_update.model_dump(exclude_unset=True).items():
        setattr(db_item, key, value)

    vendor = db.query(Vendor).filter(Vendor.vendor_id == db_item.vendor_id).first()
    _stamp_vendor_item_audit(
        db_item,
        user=current_user,
        vendor=vendor,
        request=request,
        is_update=True,
    )
    try:
        _apply_vendor_item_submit_rules(db_item, draft=draft)
    except HTTPException:
        db.rollback()
        raise
    _ensure_vendor_item_required_fields(db_item, vendor=vendor)

    apply_canonical_pack_size_to_item(db_item)
    if not draft:
        try:
            assert_no_duplicate_vendor_item(db, db_item, exclude_item_id=db_item.id)
        except HTTPException:
            db.rollback()
            raise

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_vendor_item_integrity_detail("Could not update item", exc),
        )

    return _load_item_with_rates(db, db_item.id)

@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vendor_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Delete an item for the current vendor (and its rates).
    """

    _ = current_user  # Access via permissions API; no vendor ownership gate.
    db_item = db.query(VendorItem).filter(VendorItem.id == item_id).first()

    if not db_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not found"
        )

    # Keep ORM instance for vector DB cleanup after commit (item_code / vendor_code).
    deleted_item_snapshot = db_item
    # Explicitly remove dependent rates first (DB CASCADE may not always be present).
    db.query(VendorRate).filter(VendorRate.item_id == item_id).delete(
        synchronize_session=False
    )
    db.delete(db_item)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not delete item because it is referenced by other records",
        )

    # Best-effort cleanup of vector rows (separate DB).
    try:
        from database import VectorSessionLocal
        from models import VendorItemFingerprint
        from services.item_embedding_catalog import (
            delete_vendor_item_from_item_embeddings,
        )

        vector_db = VectorSessionLocal()
        try:
            vector_db.query(VendorItemFingerprint).filter(
                VendorItemFingerprint.vendor_item_id == item_id
            ).delete(synchronize_session=False)
            vector_db.commit()
        finally:
            vector_db.close()

        delete_vendor_item_from_item_embeddings(deleted_item_snapshot)
    except Exception:
        # Main item delete already succeeded; vector cleanup is non-blocking.
        pass

    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Item rates (VendorRate)
@router.post(
    "/items/{item_id}/rates",
    response_model=VendorRateSchema,
    status_code=status.HTTP_201_CREATED,
)
async def create_vendor_item_rate(
    item_id: int,
    body: VendorRateCreateBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):

    _ = current_user  # Access via permissions API; no vendor ownership gate.
    db_item = db.query(VendorItem).filter(VendorItem.id == item_id).first()
    if not db_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not found",
        )

    db_rate = VendorRate(
        item_id=item_id,
        vendor_id=db_item.vendor_id,
        rate=body.rate,
        currency=body.currency or "INR",
        valid_from=body.valid_from,
        valid_to=body.valid_to,
        status=body.status or "pending",
    )
    db.add(db_rate)
    db.commit()
    db.refresh(db_rate)
    return db_rate


@router.put("/rates/{rate_id}", response_model=VendorRateSchema)
async def update_vendor_rate(
    rate_id: int,
    body: VendorRateCreateBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):

    _ = current_user  # Access via permissions API; no vendor ownership gate.
    db_rate = (
        db.query(VendorRate)
        .filter(VendorRate.id == rate_id)
        .first()
    )
    if not db_rate:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rate not found",
        )

    if body.rate is not None:
        db_rate.rate = body.rate
    if body.currency is not None:
        db_rate.currency = body.currency
    if body.valid_from is not None:
        db_rate.valid_from = body.valid_from
    if body.valid_to is not None:
        db_rate.valid_to = body.valid_to
    if body.status is not None:
        db_rate.status = body.status

    db.commit()
    db.refresh(db_rate)
    return db_rate


@router.delete("/rates/{rate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vendor_rate(
    rate_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):

    _ = current_user  # Access via permissions API; no vendor ownership gate.
    db_rate = (
        db.query(VendorRate)
        .filter(VendorRate.id == rate_id)
        .first()
    )
    if not db_rate:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rate not found",
        )

    db.delete(db_rate)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/directory/create")
async def create_vendor_directory_draft(
    tenant_id: int = Query(1),
    company_id: int = Query(3),
    company_code: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Buyer onboarding: create a draft vendor row (no vendor_code yet).

    Email / mobile / profile are filled on complete-registration Save.
    ``vendor_code`` is minted from next numbering only when the vendor is approved.
    """
    company_id = resolve_company_id(company_id)

    db_vendor = Vendor(
        email=None,
        mobile=None,
        # Buyer-created drafts must be submitted before they can be approved.
        status="DRAFT",
        tenant_id=tenant_id,
        company_id=company_id,
        company_code=company_code,
        vendor_registration_no=None,
        vendor_code=None,
        created_by_a=str(getattr(current_user, "user_id", None) or "") or None,
    )
    try:
        db.add(db_vendor)
        db.commit()
        db.refresh(db_vendor)
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not create vendor: {e.orig if hasattr(e, 'orig') else e}",
        )

    return {
        "vendor_id": db_vendor.vendor_id,
        "vendor_registration_no": db_vendor.vendor_registration_no,
        "vendor_code": db_vendor.vendor_code,
        "message": "Draft vendor created. Save details, then approve to assign vendor code.",
    }


@router.get("/directory", response_model=List[VendorDirectoryItem])
async def list_vendor_directory(
    status: Optional[str] = Query(None, description="Filter by vendors.status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List vendors with vendor name, GST category, and status.

    Newest vendors first (`created_at` desc, then `vendor_id` desc).

    **DB mapping:** `vendor_name` ← `company_name`, `gst_category` ← `vendor_type`, `status` ← `status`.
    """
    scoped_company_id = resolve_company_id(None)
    q = (
        db.query(Vendor)
        .filter(Vendor.company_id == scoped_company_id)
        .order_by(
            Vendor.created_at.desc().nullslast(),
            Vendor.vendor_id.desc(),
        )
    )
    if status:
        status_key = status.strip().upper()
        if status_key in ("ACTIVE", "APPROVED"):
            q = q.filter(func.upper(Vendor.status).in_(["ACTIVE", "APPROVED"]))
        elif status_key in ("PENDING", "SUBMITTED", "DRAFT"):
            # Treat buyer drafts + submitted applications as the pending queue.
            if status_key == "PENDING":
                q = q.filter(
                    func.upper(Vendor.status).in_(
                        ["PENDING", "SUBMITTED", "DRAFT"]
                    )
                )
            else:
                q = q.filter(func.upper(Vendor.status) == status_key)
        else:
            q = q.filter(func.upper(Vendor.status) == status_key)
    rows = q.offset(skip).limit(limit).all()
    return [
        VendorDirectoryItem(
            vendor_id=v.vendor_id,
            vendor_name=v.company_name,
            vendor_code=(v.vendor_code or v.vendor_registration_no),
            gst_category=v.vendor_type,
            status=v.status,
        )
        for v in rows
    ]


@router.get("/pending", response_model=List[VendorProfileOut])
async def list_pending_vendors(
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List vendors awaiting approval (``vendors.status`` is ``SUBMITTED``).

    Requires authentication. Intended for buyer/admin onboarding review.
    """
    _ = current_user
    rows = (
        db.query(Vendor)
        .filter(func.upper(Vendor.status) == "SUBMITTED")
        .order_by(Vendor.created_at.desc().nullslast(), Vendor.vendor_id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return rows


@router.get("/by-erp-code", response_model=List[VendorsForErpCodeOut])
async def list_vendors_by_erp_code(
    erp_codes: List[str] = Query(
        ...,
        description=(
            "One or more product codes (stock.product_code). "
            "Matched against vendor_items.erp_code, company_code, "
            "or vendor_product_code (case-insensitive)."
        ),
    ),
    product_names: Optional[List[str]] = Query(
        None,
        description=(
            "Optional stock.product_name values parallel to erp_codes. "
            "Used to find all vendors carrying the same product by name."
        ),
    ),
    company_id: Optional[int] = Query(
        None,
        description="Buyer company scope (forced to the deployment company).",
    ),
    tenant_id: Optional[int] = Query(None, description="Optional tenant scope."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List approved vendors related to each product code for the stock page.

    Match: ``stock.product_code`` → ``vendor_items`` (erp_code / company_code /
    vendor_product_code), then expand to sibling vendors sharing the same
    internal (erp_code, company_code) pair. Only ACTIVE/APPROVED vendors in the
    current company are returned.
    """
    _ = current_user
    scoped_company_id = resolve_company_id(company_id)

    # Preserve request order while normalizing empties / duplicates.
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
        if tenant_id is not None:
            q = q.filter(Vendor.tenant_id == int(tenant_id))
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


@router.get(
    "/directory/{vendor_id}/registration",
    response_model=VendorRegistrationReviewOut,
)
async def get_vendor_registration_snapshot(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Full registration-shaped payload for a vendor (onboarding eye → complete registration).
    Same auth as directory: any authenticated user with a valid token.
    """
    v = (
        db.query(Vendor)
        .options(
            joinedload(Vendor.banks),
            joinedload(Vendor.items).joinedload(VendorItem.rates),
            joinedload(Vendor.documents),
        )
        .filter(Vendor.vendor_id == vendor_id)
        .first()
    )
    if not v:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )

    banks_out = []
    for b in v.banks or []:
        try:
            banks_out.append(VendorBankSchema.model_validate(b))
        except Exception:
            continue

    items_out = []
    for it in v.items or []:
        try:
            items_out.append(VendorItemResponse.model_validate(it))
        except Exception:
            continue

    docs_out = []
    for d in v.documents or []:
        try:
            docs_out.append(VendorDocumentSchema.model_validate(d))
        except Exception:
            continue

    return VendorRegistrationReviewOut(
        vendor=VendorProfileOut.model_validate(v),
        banks=banks_out,
        items=items_out,
        documents=docs_out,
    )


@router.get(
    "/catalog/with-products-rates",
    response_model=List[VendorWithProductsAndRatesOut],
)
async def list_vendors_with_products_and_rates(
    vendor_id: Optional[int] = Query(
        None, description="Return only this vendor_id"
    ),
    status: Optional[str] = Query(None, description="Filter by vendors.status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    List vendors with nested **vendor_items** and each item's **vendor_rates**.

    **No authentication required.** Optional `vendor_id` and `status` filters, plus pagination.
    """
    q = db.query(Vendor).options(
        joinedload(Vendor.items).joinedload(VendorItem.rates),
    )

    if vendor_id is not None:
        q = q.filter(Vendor.vendor_id == vendor_id)

    if status:
        q = q.filter(Vendor.status == status)

    vendors = (
        q.order_by(Vendor.vendor_id.asc()).offset(skip).limit(limit).all()
    )
    return vendors


@router.get(
    "/catalog/with-banks-products-rates",
    response_model=List[VendorWithBanksProductsAndRatesOut],
)
async def list_vendors_with_banks_products_and_rates(
    vendor_id: Optional[int] = Query(
        None, description="Return only this vendor_id"
    ),
    status: Optional[str] = Query(None, description="Filter by vendors.status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """
    List vendors with nested **vendor_banks**, **vendor_items** and each item's
    **vendor_rates**.

    **No authentication required.** Optional `vendor_id` and `status` filters, plus pagination.
    """
    q = db.query(Vendor).options(
        joinedload(Vendor.banks),
        joinedload(Vendor.items).joinedload(VendorItem.rates),
    )

    if vendor_id is not None:
        q = q.filter(Vendor.vendor_id == vendor_id)

    if status:
        q = q.filter(Vendor.status == status)

    vendors = (
        q.order_by(Vendor.vendor_id.asc()).offset(skip).limit(limit).all()
    )
    return vendors


@router.get("/applications", response_model=List[VendorProfileOut])
async def get_vendor_applications(
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    query = db.query(Vendor)
    if status:
        query = query.filter(Vendor.status == status)
    
    return query.all()

@router.get("/rfq-workbench", response_model=List[VendorRFQWorkbench])
async def get_vendor_rfq_workbench(
    request: Request,
    db: Session = Depends(get_db)
):
    # Log the request URL and payload
    url = str(request.url)
    query_params = dict(request.query_params)
    headers = dict(request.headers)
    
    print("=" * 80)
    print(f"[RFQ-WORKBENCH API CALL]")
    print(f"URL: {url}")
    print(f"Query Parameters: {query_params}")
    print(f"Method: {request.method}")
    print(f"Authorization Header: {headers.get('authorization', 'Not provided')[:50]}...")
    print("-" * 80)
    print(f"[DATABASE INFORMATION]")
    print(f"Database Type: PostgreSQL")
    print(f"Database Host: {settings.db_host}")
    print(f"Database Port: {settings.db_port}")
    print(f"Database Name: {settings.db_name}")
    print(f"Database User: {settings.db_user}")
    print(f"Database URL: postgresql+psycopg2://{settings.db_user}:***@{settings.db_host}:{settings.db_port}/{settings.db_name}")
    print("-" * 80)
    print(f"[TABLES BEING QUERIED]")
    print(f"1. rfq_header (RFQHeader) - Main RFQ header information")
    print(f"2. rfq_vendor (RFQVendor) - RFQ to Vendor mapping")
    print(f"3. vendors (Vendor) - Vendor information")
    print(f"4. rfq_line (RFQLine) - RFQ line items (LEFT OUTER JOIN)")
    print(f"5. users (User) - Buyer/User information")
    print("-" * 80)
    print(f"[TABLE DATA COUNTS]")
    try:
        rfq_header_count = db.query(func.count(RFQHeader.rfq_no)).scalar()
        rfq_vendor_count = db.query(func.count(RFQVendor.vendor_id)).scalar()
        vendor_count = db.query(func.count(Vendor.vendor_id)).scalar()
        user_count = db.query(func.count(User.user_id)).scalar()
        rfq_line_count = db.query(func.count(RFQLine.line_no)).scalar()
        
        print(f"rfq_header table: {rfq_header_count} records")
        print(f"rfq_vendor table: {rfq_vendor_count} records")
        print(f"vendors table: {vendor_count} records")
        print(f"users table: {user_count} records")
        print(f"rfq_line table: {rfq_line_count} records")
        
        # Show sample data from each table to debug JOIN issues
        print("-" * 80)
        print(f"[SAMPLE DATA FROM TABLES]")
        
        # Sample rfq_header data
        sample_rfq = db.query(RFQHeader).first()
        if sample_rfq:
            print(f"Sample RFQ Header:")
            print(f"  tenant_id: {sample_rfq.tenant_id}")
            print(f"  company_id: {sample_rfq.company_id}")
            print(f"  document_type: {sample_rfq.document_type}")
            print(f"  rfq_no: {sample_rfq.rfq_no}")
            print(f"  version_no: {sample_rfq.version_no}")
            print(f"  requester_id: {sample_rfq.requester_id}")
        
        # Sample rfq_vendor data
        sample_rfq_vendor = db.query(RFQVendor).first()
        if sample_rfq_vendor:
            print(f"\nSample RFQ Vendor:")
            print(f"  tenant_id: {sample_rfq_vendor.tenant_id}")
            print(f"  company_id: {sample_rfq_vendor.company_id}")
            print(f"  document_type: {sample_rfq_vendor.document_type}")
            print(f"  rfq_no: {sample_rfq_vendor.rfq_no}")
            print(f"  version_no: {sample_rfq_vendor.version_no}")
            print(f"  vendor_id: {sample_rfq_vendor.vendor_id}")
        
        # Sample vendor data
        sample_vendor = db.query(Vendor).first()
        if sample_vendor:
            print(f"\nSample Vendor:")
            print(f"  vendor_id: {sample_vendor.vendor_id}")
            print(f"  company_name: {sample_vendor.company_name}")
        
        # Sample user data
        sample_user = db.query(User).first()
        if sample_user:
            print(f"\nSample User:")
            print(f"  user_id: {sample_user.user_id}")
            print(f"  email: {sample_user.email}")
        
        # Check if JOIN conditions match
        print("-" * 80)
        print(f"[JOIN CONDITION CHECK]")
        if sample_rfq and sample_rfq_vendor:
            print(f"RFQ Header -> RFQ Vendor JOIN:")
            print(f"  tenant_id match: {sample_rfq.tenant_id == sample_rfq_vendor.tenant_id}")
            print(f"  company_id match: {sample_rfq.company_id == sample_rfq_vendor.company_id}")
            print(f"  document_type match: {sample_rfq.document_type == sample_rfq_vendor.document_type}")
            print(f"  rfq_no match: {sample_rfq.rfq_no == sample_rfq_vendor.rfq_no}")
            print(f"  version_no match: {sample_rfq.version_no == sample_rfq_vendor.version_no}")
        
        if sample_rfq_vendor and sample_vendor:
            print(f"\nRFQ Vendor -> Vendor JOIN:")
            print(f"  vendor_id match: {sample_rfq_vendor.vendor_id == sample_vendor.vendor_id}")
        
        if sample_rfq and sample_user:
            print(f"\nRFQ Header -> User JOIN:")
            print(f"  requester_id match: {sample_rfq.requester_id == sample_user.user_id}")
        
    except Exception as e:
        print(f"Error checking data: {str(e)}")
        import traceback
        traceback.print_exc()
    print("=" * 80)
    
    rows = (
        db.query(
            RFQHeader.rfq_no,
            RFQHeader.version_no,
            RFQHeader.rfq_date,
            func.max(RFQLine.required_date).label("due_date"),
            RFQHeader.document_number,
            RFQHeader.status,
            Vendor.company_name.label("vendor_name"),
            User.email.label("buyer_name"),
        )
        .join(
            RFQVendor,
            (RFQHeader.tenant_id == RFQVendor.tenant_id) &
            (RFQHeader.company_id == RFQVendor.company_id) &
            (RFQHeader.document_type == RFQVendor.document_type) &
            (RFQHeader.rfq_no == RFQVendor.rfq_no) &
            (RFQHeader.version_no == RFQVendor.version_no)
        )
        .join(
            Vendor,
            Vendor.vendor_id == RFQVendor.vendor_id
        )
        .join(
            User,
            User.user_id == RFQHeader.requester_id
        )
        .outerjoin(
            RFQLine,
            (RFQLine.tenant_id == RFQHeader.tenant_id) &
            (RFQLine.company_id == RFQHeader.company_id) &
            (RFQLine.document_type == RFQHeader.document_type) &
            (RFQLine.rfq_no == RFQHeader.rfq_no) &
            (RFQLine.version_no == RFQHeader.version_no)
        )
        .group_by(
            RFQHeader.rfq_no,
            RFQHeader.version_no,
            RFQHeader.rfq_date,
            RFQHeader.document_number,
            RFQHeader.status,
            Vendor.company_name,
            User.email
        )
        .order_by(RFQHeader.rfq_date.desc())
        .all()
    )
    
    # Log query results
    print("-" * 80)
    print(f"[QUERY RESULTS]")
    print(f"Total rows returned: {len(rows)}")
    if len(rows) > 0:
        print(f"Sample row: {rows[0]}")
    else:
        print("⚠️  WARNING: No rows returned from query!")
        print("Possible reasons:")
        print("  1. No data in rfq_header table")
        print("  2. No matching records in rfq_vendor table (INNER JOIN)")
        print("  3. No matching vendors in vendors table (INNER JOIN)")
        print("  4. No matching users in users table for requester_id (INNER JOIN)")
    print("=" * 80)

    return [
        {
            "rfq_no": row.rfq_no,
            "version_no": row.version_no,
            "rfq_date": row.rfq_date,
            "due_date": row.due_date,
            "document_number": row.document_number,
            "status": row.status,
            "vendor_name": row.vendor_name,
            "buyer_name": row.buyer_name,
        }
        for row in rows
    ]
@router.get(
    "/rfq/{rfq_no}/{version_no}/lines",
    response_model=List[RFQLineResponse]
)
async def get_rfq_lines(
    rfq_no: str,
    version_no: int,
    db: Session = Depends(get_db)
):
    # Get RFQ header first
    header = db.query(RFQHeader).filter(
        RFQHeader.rfq_no == rfq_no,
        RFQHeader.version_no == version_no
    ).first()

    if not header:
        raise HTTPException(status_code=404, detail="RFQ not found")

    # Fetch lines using header info internally
    lines = db.query(RFQLine).filter(
        RFQLine.tenant_id == header.tenant_id,
        RFQLine.company_id == header.company_id,
        RFQLine.document_type == header.document_type,
        RFQLine.rfq_no == rfq_no,
        RFQLine.version_no == version_no
    ).order_by(RFQLine.line_no).all()

    return lines


@router.patch("/{vendor_id}/status", response_model=VendorProfileOut)
async def update_vendor_status(
    vendor_id: int,
    body: VendorStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update a vendor's lifecycle status (e.g. pending → ACTIVE).
    Used by onboarding / complete-registration Approve.

    On ACTIVE/approved: mint ``vendor_code`` from next numbering (once).
    """
    _ = current_user
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )

    new_status = (body.status or "").strip()
    if not new_status:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="status is required",
        )

    new_upper = new_status.upper()
    current_upper = (vendor.status or "").strip().upper()
    if new_upper in ("ACTIVE", "APPROVED") and current_upper in ("ACTIVE", "APPROVED"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vendor is already approved.",
        )

    if new_upper in ("ACTIVE", "APPROVED"):
        # The approval workflow, not the caller, decides whether this may happen.
        assert_vendor_activation_allowed(
            vendor_id=vendor_id,
            vendor_status=vendor.status,
            bearer_token=_bearer_token_from_request(request),
        )
        try:
            assign_vendor_code_on_approval(db, vendor)
        except (RuntimeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(e),
            )

    vendor.status = new_status
    vendor.modified_on_a = datetime.utcnow()
    db.commit()
    db.refresh(vendor)

    # Same pattern as PO submit/handoff: best-effort WhatsApp; never fail the API.
    if new_upper in ("ACTIVE", "APPROVED"):
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

    return vendor


@router.get("/{vendor_id}", response_model=VendorDetailOut)
async def get_vendor(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    vendor = db.query(Vendor).options(
        joinedload(Vendor.banks),
        joinedload(Vendor.documents),
        joinedload(Vendor.items),
    ).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found"
        )
    
    # Role gate removed — permissions API owns access.
    _ = current_user
    return vendor

@router.delete("/{vendor_id}", status_code=status.HTTP_200_OK)
async def delete_vendor(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Delete a vendor and related onboarding data (banks, items, documents, users, POs).
    Intended for buyer onboarding cleanup — vendor-role users cannot call this.
    """
    try:
        return delete_vendor_and_related(
            db,
            vendor_id=vendor_id,
            current_user=current_user,
        )
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not delete vendor: {exc}",
        ) from exc


@router.post("/{vendor_id}/approve")
async def approve_vendor(
    vendor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    _ = current_user
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found"
        )

    current_upper = (vendor.status or "").strip().upper()
    if current_upper in ("ACTIVE", "APPROVED"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vendor is already approved.",
        )

    assert_vendor_activation_allowed(
        vendor_id=vendor_id,
        vendor_status=vendor.status,
        bearer_token=_bearer_token_from_request(request),
    )

    try:
        assign_vendor_code_on_approval(db, vendor)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )

    # Keep one activation vocabulary with PATCH /status (ACTIVE).
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
        "vendor_code": vendor.vendor_code,
        "vendor_registration_no": vendor.vendor_registration_no,
    }

@router.post("/{vendor_id}/reject")
async def reject_vendor(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found"
        )
    
    vendor.status = "rejected"
    db.commit()
    
    return {"message": "Vendor rejected successfully"}

@router.post("/login")
async def vendor_login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    print("=" * 80)
    print(f"[VENDOR LOGIN ATTEMPT]")
    print(f"Email/Username: {form_data.username}")
    print(f"Password provided: {'Yes' if form_data.password else 'No'}")
    
    # Prefer Nest user-context based login (buyer/vendor) and permissions.
    # If Nest is unavailable or user-context cannot be resolved, fall back to local vendor auth.
    user = None
    
    session_payload: Optional[Dict[str, Any]] = None
    user_context_payload: Optional[Dict[str, Any]] = None
    permissions_payload: Optional[Any] = None
    session_error: Optional[str] = None

    # Helpers to parse Nest user-context payload
    def _pick_int(source: Dict[str, Any], *keys: str, default: int = 0) -> int:
        for k in keys:
            v = source.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return default

    def _pick_bool(source: Dict[str, Any], *keys: str, default: bool = False) -> bool:
        for k in keys:
            v = source.get(k)
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                if v.lower() in ("true", "1", "yes"):
                    return True
                if v.lower() in ("false", "0", "no"):
                    return False
        return default

    def _pick_role_ids(source: Dict[str, Any]) -> List[int]:
        raw_role_ids = source.get("role_ids", source.get("roleIds"))
        if isinstance(raw_role_ids, list):
            out = []
            for r in raw_role_ids:
                try:
                    out.append(int(r))
                except (TypeError, ValueError):
                    continue
            if out:
                return out

        role_id = source.get("role_id", source.get("roleId"))
        if role_id is not None:
            try:
                return [int(role_id)]
            except (TypeError, ValueError):
                pass
        return []

    # 1) Prefer Foundation auth (password-verified login), then user-context.
    #    Local DB may be Foundation's `users` schema (no `email` column) — never 500 on that.
    nest_access_from_login: Optional[str] = None
    try:
        nest_base = (settings.nestjs_api_url or "").strip().rstrip("/")
        print("[LOGIN] Trying Nest Foundation auth first")
        print(f"[LOGIN] Nest base URL: {nest_base or '<unset>'}")

        if not nest_base:
            raise RuntimeError("NESTJS_API_URL is not configured")

        login_url = f"{nest_base}/auth/login"
        print(f"[LOGIN API] POST {login_url}")
        try:
            login_resp = requests.post(
                login_url,
                json={
                    "email": form_data.username,
                    "password": form_data.password,
                },
                headers={"Accept": "application/json"},
                timeout=_NEST_LOGIN_TIMEOUT_S,
            )
            print(f"[LOGIN] Nest /auth/login status: {login_resp.status_code}")
            if login_resp.ok:
                login_body = login_resp.json() if login_resp.content else {}
                if isinstance(login_body, dict):
                    nest_access_from_login = (
                        login_body.get("accessToken")
                        or login_body.get("access_token")
                    )
            elif login_resp.status_code in (401, 404):
                # Invalid credentials or unknown user on Foundation — stop early
                # with a clean 401 instead of falling into a schema-mismatched local DB.
                print("[LOGIN] Nest login rejected credentials")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid email or password",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            elif login_resp.status_code >= 500:
                # Foundation DB/pooler errors used to cascade into more Nest calls
                # and hit the frontend 30s axios timeout. Fall back to local auth.
                print(
                    f"[LOGIN] Nest login upstream error status={login_resp.status_code} "
                    f"body={(login_resp.text or '')[:300]}"
                )
                raise RuntimeError("nest_login_upstream_5xx")
            else:
                print(
                    f"[LOGIN] Nest login unexpected status={login_resp.status_code}; "
                    "falling back to local auth"
                )
                raise RuntimeError("nest_login_unexpected_status")
        except HTTPException:
            raise
        except requests.Timeout as login_exc:
            print(f"[LOGIN] Nest /auth/login timeout: {login_exc}")
            raise RuntimeError("nest_login_timeout")
        except RuntimeError:
            raise
        except Exception as login_exc:
            print(f"[LOGIN] Nest /auth/login error: {login_exc}")
            raise RuntimeError("nest_login_unavailable") from login_exc

        if not nest_access_from_login:
            print("[LOGIN] Nest login returned no access token; falling back to local auth")
            raise RuntimeError("nest_login_no_token")

        # Prefer Foundation-issued token. Only mint RS256 locally when Nest
        # did not return an access token (JWT_PRIVATE_KEY not required otherwise).
        bootstrap_token = nest_access_from_login
        user_context_url = f"{nest_base}/auth/user-context/by-email"
        print(f"[LOGIN] Nest user-context URL: {user_context_url} email={form_data.username}")
        print(f"[LOGIN API] GET {user_context_url}?email={form_data.username}")
        try:
            ctx_resp = requests.get(
                user_context_url,
                params={"email": form_data.username},
                headers={
                    "Authorization": f"Bearer {bootstrap_token}",
                    "Accept": "application/json",
                },
                timeout=_NEST_CONTEXT_TIMEOUT_S,
            )
        except Exception as ctx_exc:
            print(f"[LOGIN] Nest user-context error after successful login: {ctx_exc}")
            # Password already verified — return token immediately; permissions load on the client.
            return {
                "access_token": nest_access_from_login,
                "token_type": "bearer",
                "user_context": None,
                "session": None,
                "permissions": None,
                "session_error": f"user_context_fetch_error: {ctx_exc}",
                "user": {
                    "id": 1,
                    "email": form_data.username,
                    "role": "vendor",
                    "vendor_id": None,
                },
            }
        print("ctx_resp", ctx_resp.json() if ctx_resp.content else None)
        print(f"[LOGIN] Nest user-context status: {ctx_resp.status_code}")
        if ctx_resp.ok:
            user_context_payload = ctx_resp.json() if ctx_resp.content else {}
            ctx = user_context_payload if isinstance(user_context_payload, dict) else {}

            role_ids = _pick_role_ids(ctx)
            user_id_for_sub = _pick_int(ctx, "user_id", "userId", "id", default=0)
            tenant_id = _pick_int(ctx, "tenant_id", "tenantId", default=1)
            company_id = _pick_int(ctx, "company_id", "companyId", default=3)
            is_super_admin = _pick_bool(ctx, "is_super_admin", "isSuperAdmin", default=False)

            roles_from_ctx = ctx.get("roles")
            roles_payload = roles_from_ctx if isinstance(roles_from_ctx, list) else None
            if roles_payload is None:
                # Map Nest user-context shape: roleAssignments / roleNames
                role_assignments = ctx.get("roleAssignments")
                if isinstance(role_assignments, list) and role_assignments:
                    roles_payload = role_assignments
                else:
                    role_names = ctx.get("roleNames")
                    if isinstance(role_names, list) and role_names:
                        roles_payload = [
                            {"roleId": (role_ids[idx] if idx < len(role_ids) else None), "roleName": rn}
                            for idx, rn in enumerate(role_names)
                        ]
            if roles_payload is None:
                if role_ids:
                    roles_payload = [{"roleId": role_ids[0], "roleName": "user"}]
                else:
                    roles_payload = []
            resolved_role_name = (roles_payload[0].get("roleName") if roles_payload else "user")

            def _ctx_is_vendor_role() -> bool:
                if (resolved_role_name or "").strip().lower() == "vendor":
                    return True
                for rn in ctx.get("roleNames") or []:
                    if str(rn).strip().lower() == "vendor":
                        return True
                for r in roles_payload or []:
                    if isinstance(r, dict) and str(r.get("roleName", "")).strip().lower() == "vendor":
                        return True
                return False

            response_vendor_id: Optional[int] = None
            if _ctx_is_vendor_role():
                # Prefer local vendor link by email — soft-fail if schema mismatches.
                try:
                    u_for_vendor = (
                        db.query(User)
                        .filter(User.email == form_data.username)
                        .first()
                    )
                    if u_for_vendor and u_for_vendor.vendor_id is not None:
                        response_vendor_id = int(u_for_vendor.vendor_id)
                except Exception as vendor_lookup_exc:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    print(f"[LOGIN] local vendor lookup skipped: {vendor_lookup_exc}")

                # Fallback: Foundation users often have no external-sourcing users.vendor_id.
                # Resolve vendor_id from vendors.email so OCR / complete-registration work.
                if response_vendor_id is None:
                    try:
                        from services.po_vendor_scope import find_vendor_by_email

                        vendor_by_email = find_vendor_by_email(
                            db, form_data.username
                        )
                        if vendor_by_email is not None:
                            response_vendor_id = int(vendor_by_email.vendor_id)
                            print(
                                f"[LOGIN] vendor_id resolved from vendors.email="
                                f"{form_data.username} -> {response_vendor_id}"
                            )
                    except Exception as vendor_email_lookup_exc:
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        print(
                            f"[LOGIN] vendors.email lookup skipped: {vendor_email_lookup_exc}"
                        )
                print(f"[LOGIN] vendor role: vendor_id={response_vendor_id}")

            # Ensure token subject exists in local DB when schema allows it.
            # Soft-fail: Nest login must succeed even if local users table is Foundation-shaped.
            try:
                local_user = (
                    db.query(User)
                    .filter((User.user_id == int(user_id_for_sub or 1)) | (User.email == form_data.username))
                    .first()
                )
                if local_user is None:
                    local_user = create_user(
                        db,
                        user_id=int(user_id_for_sub or 1),
                        email=form_data.username,
                        hashed_password="",
                        role=resolved_role_name,
                        is_active=True,
                        vendor_id=None,
                    )
                else:
                    local_user.user_id = int(user_id_for_sub or local_user.user_id or 1)
                    local_user.email = form_data.username
                    local_user.role = resolved_role_name
                    if local_user.is_active is False or local_user.is_active is None:
                        local_user.is_active = True
                    db.commit()
                if response_vendor_id is not None:
                    local_user.vendor_id = response_vendor_id
                    local_user.role = "vendor"
                    db.commit()
            except Exception as local_sync_exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                print(f"[LOGIN] local user sync skipped (schema/DB): {local_sync_exc}")

            # Prefer Foundation-issued access token when login succeeded; else mint RS256 context token.
            token_role = (
                "vendor" if response_vendor_id is not None else resolved_role_name
            )
            nest_user_id = int(user_id_for_sub or 1)
            if nest_access_from_login:
                access_token = nest_access_from_login
                nest_token = nest_access_from_login
            else:
                nest_signing_key = _nest_rs256_private_key()
                nest_payload: Dict[str, Any] = {
                    "sub": str(nest_user_id),
                    "user_id": nest_user_id,
                    "tenant_id": int(tenant_id),
                    "company_id": int(company_id),
                    "is_super_admin": bool(is_super_admin),
                    "role_ids": role_ids,
                    "role_id": (role_ids[0] if role_ids else None),
                    "roles": roles_payload,
                    "email": form_data.username,
                    "role": token_role,
                    "application_id": 2,
                }
                print("nest_payload", nest_payload)
                access_token = jwt.encode(nest_payload, nest_signing_key, algorithm="RS256")
                nest_token = access_token

            # Permissions/session load on the client after login (loadPermissions).
            # Avoid blocking login on slow Nest /me/permissions + /auth/session hops.
            permissions_payload = None
            session_payload = None
            session_error = None

            print(f"User found via Nest user-context: True")
            print(f"  Nest userId: {user_id_for_sub}")
            print(f"  Nest roleIds: {role_ids}")
            print("=" * 80)

            return {
                "access_token": access_token,
                "token_type": "bearer",
                "user_context": user_context_payload,
                "session": session_payload,
                "permissions": permissions_payload,
                "session_error": session_error,
                "user": {
                    "id": int(user_id_for_sub or 1),
                    "email": form_data.username,
                    "role": token_role,
                    "vendor_id": response_vendor_id,
                },
            }
        else:
            # Nest password already verified — don't fall through to local auth.
            print(
                f"[LOGIN] Nest user-context failed status={ctx_resp.status_code}; "
                "returning Nest token without full context"
            )
            return {
                "access_token": nest_access_from_login,
                "token_type": "bearer",
                "user_context": None,
                "session": None,
                "permissions": None,
                "session_error": f"user_context_fetch_failed: {ctx_resp.status_code}",
                "user": {
                    "id": 1,
                    "email": form_data.username,
                    "role": "vendor",
                    "vendor_id": None,
                },
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[LOGIN] Nest user-context/session error: {e}")
        session_error = f"user_context_fetch_error: {str(e)}"
        # Password already verified by Foundation — do not fall through to
        # local DB (often Foundation-shaped, missing users.email).
        if nest_access_from_login:
            print(
                "[LOGIN] Nest password verified; returning Foundation token "
                "without full context (skipping local DB fallback)"
            )
            return {
                "access_token": nest_access_from_login,
                "token_type": "bearer",
                "user_context": None,
                "session": None,
                "permissions": None,
                "session_error": session_error,
                "user": {
                    "id": 1,
                    "email": form_data.username,
                    "role": "vendor",
                    "vendor_id": None,
                },
            }

    # 2) Fall back to local vendor auth (original behavior)
    print("[LOGIN] Falling back to local DB authenticate_vendor")
    user = authenticate_vendor(db, form_data.username, form_data.password)
    print(f"User found (local DB): {user is not None}")
    if user:
        print(f"  User ID: {user.user_id}")
        print(f"  Email: {user.email}")
        print(f"  Role: {user.role}")
        print(f"  Is Active: {user.is_active}")
        print(f"  Vendor ID: {user.vendor_id}")
        print(f"  Has vendor relationship: {user.vendor is not None}")
    else:
        try:
            existing_user = db.query(User).filter(User.email == form_data.username).first()
            if existing_user:
                print(f"  User exists but authentication failed:")
                print(f"    Role: {existing_user.role}")
                print(f"    Is Active: {existing_user.is_active}")
            else:
                print(f"  User with email {form_data.username} not found in database")
        except Exception as lookup_exc:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"  Local user lookup skipped (schema/DB): {lookup_exc}")

    if not user:
        print("=" * 80)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Vendor-only checks remain for local login
    if not user.vendor_id:
        print(f"  ERROR: User has no vendor_id")
        print("=" * 80)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vendor profile not found. User has no vendor_id.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    vendor = db.query(Vendor).filter(Vendor.vendor_id == user.vendor_id).first()
    if not vendor:
        print(f"  ERROR: Vendor record not found for vendor_id: {user.vendor_id}")
        print("=" * 80)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Vendor profile not found in database",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        print(f"  ERROR: User account is not active")
        print("=" * 80)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive vendor account"
        )

    # Prefer a Nest-compatible RS256 token (sub = user id) so frontend
    # /me/permissions calls succeed. Fall back to local HS/RS token if Nest mint fails.
    nest_access_token: Optional[str] = None
    user_context_payload = None
    session_payload = None
    permissions_payload = None
    session_error: Optional[str] = None
    try:
        nest_signing_key = _nest_rs256_private_key()
        bootstrap_token = jwt.encode(
            {"sub": str(int(user.user_id)), "email": user.email},
            nest_signing_key,
            algorithm="RS256",
        )
        user_context_url = f"{settings.nestjs_api_url}/auth/user-context/by-email"
        print(f"[LOGIN API] GET {user_context_url}?email={user.email}")
        ctx_resp = requests.get(
            user_context_url,
            params={"email": user.email},
            headers={
                "Authorization": f"Bearer {bootstrap_token}",
                "Accept": "application/json",
            },
            timeout=_NEST_CONTEXT_TIMEOUT_S,
        )
        if ctx_resp.ok:
            user_context_payload = ctx_resp.json() if ctx_resp.content else {}
            ctx = user_context_payload if isinstance(user_context_payload, dict) else {}
            role_ids = _pick_role_ids(ctx)
            roles_from_ctx = ctx.get("roles")
            roles_payload = (
                roles_from_ctx
                if isinstance(roles_from_ctx, list)
                else [
                    {
                        "roleId": (role_ids[0] if role_ids else 1),
                        "roleName": user.role or "vendor",
                    }
                ]
            )
            nest_user_id = _pick_int(
                ctx, "user_id", "userId", "id", default=user.user_id
            )
            nest_payload: Dict[str, Any] = {
                "sub": str(nest_user_id),
                "user_id": int(nest_user_id),
                "tenant_id": _pick_int(ctx, "tenant_id", "tenantId", default=1),
                "company_id": _pick_int(ctx, "company_id", "companyId", default=3),
                "is_super_admin": _pick_bool(
                    ctx, "is_super_admin", "isSuperAdmin", default=False
                ),
                "role_ids": role_ids,
                "role_id": (role_ids[0] if role_ids else None),
                "roles": roles_payload,
                "email": user.email,
                "role": (user.role or "vendor"),
                "application_id": 2,
            }
            nest_access_token = jwt.encode(
                nest_payload, nest_signing_key, algorithm="RS256"
            )
            session_error = None
        else:
            session_error = f"user_context_fetch_failed: {ctx_resp.status_code}"
    except Exception as e:
        session_error = f"session_fetch_error: {str(e)}"

    access_token = nest_access_token or create_access_token(
        data={
            "sub": str(user.user_id),
            "email": user.email,
            "role": user.role,
            "user_id": user.user_id,
        }
    )
    
    print(f"  SUCCESS: Login successful")
    print(f"  Token generated: {access_token[:50]}...")
    print("=" * 80)
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_context": user_context_payload,
        "session": session_payload,
        "permissions": permissions_payload,
        "session_error": session_error,
        "user": {
            "id": user.user_id,
            "email": user.email,
            "role": user.role,
            "vendor_id": user.vendor_id
        }
    }


@router.post("/{vendor_id}/ocr")
async def vendor_document_ocr(
    vendor_id: int,
    request: Request,
    files: List[UploadFile] = File(...),
    po_number: Optional[str] = Form(None),
    po_id: Optional[int] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Backward-compatible wrapper. Prefer POST /ocr/vendor/{vendor_id}."""
    # Role gate removed — permissions API owns access.

    job_id, job_dir, saved_files, received = await save_upload_files(files)
    try:
        return process_vendor_documents(
            db,
            vendor_id=vendor_id,
            saved_files=saved_files,
            received_files=received,
            tenant_id=int(getattr(current_user, "token_tenant_id", None) or 1),
            company_id=int(getattr(current_user, "token_company_id", None) or 3),
            created_by=str(current_user.user_id),
            po_number=po_number,
            po_id=po_id,
            bearer_token=_bearer_token_from_request(request),
        )
    finally:
        cleanup_job_dir(job_dir, saved_files)

