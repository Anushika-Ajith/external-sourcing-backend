"""Shared WhatsApp list scoping (PO / invoice / GRN by phone + role)."""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

import requests
from sqlalchemy.orm import Session

from config import settings
from models import Vendor
from services.vendor_phone_lookup import find_vendor_by_phone

logger = logging.getLogger(__name__)

# Vendor statuses that mean "this phone is a real vendor portal user".
_VENDOR_ACTIVE_STATUSES = frozenset(
    {"APPROVED", "ACTIVE", "ENABLED", "REGISTERED"}
)

# Foundation / app roles that should see company-wide document lists.
_BUYER_LIKE_ROLES = frozenset(
    {
        "BUYER",
        "ADMIN",
        "SUPERADMIN",
        "SUPER_ADMIN",
        "STOREKEEPER",
        "ODIN_ADMIN",
        "INDIVIDUAL_WHATSAPP_USER",
    }
)

_VENDOR_LIKE_ROLES = frozenset({"VENDOR", "SUPPLIER"})


def _normalize_role(role: Optional[str]) -> str:
    return (
        str(role or "")
        .strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )


def is_buyer_like_role(role: Optional[str]) -> bool:
    r = _normalize_role(role)
    if not r:
        return False
    if r in _BUYER_LIKE_ROLES:
        return True
    if "VENDOR" in r or "SUPPLIER" in r:
        return False
    if "BUYER" in r or "ADMIN" in r:
        return True
    return False


def is_vendor_like_role(role: Optional[str]) -> bool:
    r = _normalize_role(role)
    if not r:
        return False
    if r in _VENDOR_LIKE_ROLES:
        return True
    return "VENDOR" in r or "SUPPLIER" in r


def lookup_whatsapp_role_by_phone(mobile: str) -> Optional[str]:
    """
    Same Foundation path WhatsApp uses:
    GET {NESTJS_API_URL}/auth/user-roles/by-phone/internal?phoneNumber=...
    """
    base = (getattr(settings, "nestjs_api_url", None) or "").strip().rstrip("/")
    if not base:
        return None
    digits = re.sub(r"\D", "", str(mobile or ""))
    if not digits:
        return None
    url = f"{base}/auth/user-roles/by-phone/internal"
    try:
        resp = requests.get(url, params={"phoneNumber": digits}, timeout=8)
        if resp.status_code >= 400:
            logger.info(
                "[whatsapp-scope] role lookup failed status=%s phone=%s",
                resp.status_code,
                digits,
            )
            return None
        payload = resp.json() if resp.content else {}
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        role = (
            payload.get("role")
            or data.get("role")
            or payload.get("userRole")
            or data.get("userRole")
            or (payload.get("roleNames") or [None])[0]
            or (data.get("roleNames") or [None])[0]
        )
        if role is None:
            assignments = payload.get("roleAssignments") or data.get("roleAssignments")
            if isinstance(assignments, list) and assignments:
                first = assignments[0]
                if isinstance(first, dict):
                    role = first.get("roleName") or first.get("role")
        return str(role).strip() if role else None
    except Exception as exc:
        logger.info(
            "[whatsapp-scope] role lookup error phone=%s: %s",
            digits,
            exc,
        )
        return None


def resolve_whatsapp_vendor_scope(
    db: Session,
    mobile: str,
    scope: str = "auto",
    role: Optional[str] = None,
) -> Tuple[Optional[int], str]:
    """
    Decide vendor_id filter for WhatsApp document lists.

    Returns ``(vendor_id_or_None, reason)``:
    - ``vendor_id`` set → filter to that vendor's documents
    - ``vendor_id is None`` → no vendor filter (buyer / company-wide list)
    - special: caller should return ``[]`` when scope=vendor and no vendor matched
      (``reason == "vendor_required_missing"``)

    BUYER (and buyer-like) roles always list all documents in tenant/company.
    """
    scope_norm = (scope or "auto").strip().lower() or "auto"
    role_norm = _normalize_role(role)
    if not role_norm:
        role_norm = _normalize_role(lookup_whatsapp_role_by_phone(mobile))

    # Explicit buyer role → always company-wide list (ignore vendor phone match).
    if is_buyer_like_role(role_norm):
        return None, f"role_{role_norm or 'BUYER'}"

    if scope_norm == "all":
        return None, "scope_all"

    vendor: Optional[Vendor] = find_vendor_by_phone(db, mobile)

    if scope_norm == "vendor" or is_vendor_like_role(role_norm):
        if not vendor or vendor.vendor_id is None:
            return None, "vendor_required_missing"
        return int(vendor.vendor_id), (
            f"role_{role_norm}" if is_vendor_like_role(role_norm) else "scope_vendor"
        )

    # auto (no buyer role resolved)
    if not vendor or vendor.vendor_id is None:
        return None, "auto_no_vendor_match"

    status = str(getattr(vendor, "status", None) or "").strip().upper()
    # Draft / pending vendor rows often share a buyer's phone during registration.
    if status and status not in _VENDOR_ACTIVE_STATUSES:
        return None, f"auto_vendor_status_{status or 'blank'}"

    return int(vendor.vendor_id), "auto_approved_vendor"
