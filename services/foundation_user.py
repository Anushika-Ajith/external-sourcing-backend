"""Client for Foundation (Nest) user APIs used during registration."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException, status

from config import settings

logger = logging.getLogger(__name__)

_REGISTER_TIMEOUT_S = 20
_VENDOR_ROLE_NAME = "VENDOR"


def _nestjs_base() -> str:
    base = (settings.nestjs_api_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NESTJS_API_URL is not configured; cannot register user in foundation_db",
        )
    return base


def _extract_error_detail(body: Any, *, fallback: str) -> str:
    if isinstance(body, dict):
        msg = body.get("message") or body.get("detail") or body.get("error")
        if isinstance(msg, list):
            return "; ".join(str(m) for m in msg)
        if msg:
            return str(msg)
    if isinstance(body, str) and body.strip():
        return body.strip()
    return fallback


def _parse_json_body(resp: requests.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return (resp.text or "")[:500]


def register_user_in_foundation(
    email: str,
    password: str,
    *,
    phone_number: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a user in foundation_db via existing Nest API:
    POST {NESTJS_API_URL}/auth/register

    Returns the Nest response body (userId, email, accessToken, refreshToken).
    """
    base = _nestjs_base()

    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="password must be at least 8 characters",
        )

    url = f"{base}/auth/register"
    payload: Dict[str, Any] = {
        "email": email.strip(),
        "password": password,
    }
    if phone_number:
        payload["phoneNumber"] = phone_number

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=_REGISTER_TIMEOUT_S,
        )
    except requests.Timeout as exc:
        logger.error("[register] Foundation /auth/register timeout: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Foundation registration timed out",
        ) from exc
    except requests.RequestException as exc:
        logger.error("[register] Foundation /auth/register request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Foundation registration failed: {exc}",
        ) from exc

    body = _parse_json_body(resp)

    if resp.status_code in (200, 201) and isinstance(body, dict):
        logger.info(
            "[register] Foundation user created userId=%s email=%s",
            body.get("userId"),
            body.get("email") or email,
        )
        return body

    detail = _extract_error_detail(body, fallback=resp.text or "Foundation registration failed")

    if resp.status_code == 409:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    if resp.status_code == 400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )

    logger.error(
        "[register] Foundation /auth/register status=%s detail=%s",
        resp.status_code,
        detail,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=detail,
    )


def create_vendor_user_in_foundation(
    email: str,
    password: str,
    *,
    company_id: int,
    tenant_id: int,
    phone_number: Optional[str] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a Foundation user with the vendor role (UserRole row) via:
    POST {NESTJS_API_URL}/users  with roleNames=["VENDOR"]

    On email conflict, PATCHes the existing user to set password + vendor role.
    """
    base = _nestjs_base()

    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="password must be at least 8 characters",
        )

    normalized_email = email.strip()
    display_name = (name or normalized_email.split("@")[0] or "vendor")[:200]
    payload: Dict[str, Any] = {
        "name": display_name,
        "email": normalized_email,
        "password": password,
        "companyId": int(company_id),
        "tenantId": int(tenant_id),
        "roleNames": [_VENDOR_ROLE_NAME],
        "createdBy": "external-sourcing",
    }
    if phone_number:
        # Foundation phoneNumber: 7-15 digits, optional leading +
        digits = "".join(c for c in str(phone_number) if c.isdigit() or c == "+")
        if digits:
            payload["phoneNumber"] = digits[:15]

    url = f"{base}/users"
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=_REGISTER_TIMEOUT_S,
        )
    except requests.Timeout as exc:
        logger.error("[vendor-register] Foundation POST /users timeout: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Foundation vendor registration timed out",
        ) from exc
    except requests.RequestException as exc:
        logger.error("[vendor-register] Foundation POST /users failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Foundation vendor registration failed: {exc}",
        ) from exc

    body = _parse_json_body(resp)

    if resp.status_code in (200, 201) and isinstance(body, dict):
        user_id = body.get("id") or body.get("userId") or body.get("user_id")
        logger.info(
            "[vendor-register] Foundation vendor user created id=%s email=%s roles=%s",
            user_id,
            body.get("email") or normalized_email,
            _role_names_from_user_body(body),
        )
        return body

    detail = _extract_error_detail(
        body, fallback=resp.text or "Foundation vendor registration failed"
    )

    # Email already exists — update password + ensure vendor UserRole
    if resp.status_code == 409:
        if not _is_email_conflict(detail):
            logger.warning(
                "[vendor-register] Foundation POST /users conflict (non-email) for %s: %s — retrying once",
                normalized_email,
                detail,
            )
            try:
                retry_resp = requests.post(
                    url,
                    json=payload,
                    headers={"Accept": "application/json"},
                    timeout=_REGISTER_TIMEOUT_S,
                )
            except requests.RequestException as exc:
                logger.error(
                    "[vendor-register] Foundation POST /users retry failed for %s: %s",
                    normalized_email,
                    exc,
                )
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Foundation vendor registration failed after retry",
                ) from exc

            retry_body = _parse_json_body(retry_resp)
            if retry_resp.status_code in (200, 201) and isinstance(retry_body, dict):
                user_id = (
                    retry_body.get("id")
                    or retry_body.get("userId")
                    or retry_body.get("user_id")
                )
                logger.info(
                    "[vendor-register] Foundation vendor user created on retry id=%s email=%s",
                    user_id,
                    retry_body.get("email") or normalized_email,
                )
                return retry_body

            resp = retry_resp
            body = retry_body
            detail = _extract_error_detail(
                body,
                fallback=retry_resp.text or detail,
            )

        if _is_email_conflict(detail):
            logger.info(
                "[vendor-register] Foundation user exists for %s; ensuring vendor role",
                normalized_email,
            )
            return _ensure_existing_foundation_vendor(
                base,
                email=normalized_email,
                password=password,
                company_id=int(company_id),
                tenant_id=int(tenant_id),
                phone_number=payload.get("phoneNumber"),
                name=display_name,
            )

        logger.error(
            "[vendor-register] Foundation POST /users conflict (non-email) for %s: %s",
            normalized_email,
            detail,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )

    if resp.status_code == 400:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
        )

    logger.error(
        "[vendor-register] Foundation POST /users status=%s detail=%s",
        resp.status_code,
        detail,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=detail,
    )


def _role_names_from_user_body(body: Dict[str, Any]) -> List[str]:
    roles = body.get("roles")
    names: List[str] = []
    if isinstance(roles, list):
        for ur in roles:
            if not isinstance(ur, dict):
                continue
            role = ur.get("role") if isinstance(ur.get("role"), dict) else ur
            if isinstance(role, dict):
                name = role.get("name") or role.get("roleName")
                if name:
                    names.append(str(name))
    return names


def _is_email_conflict(detail: str) -> bool:
    """True when Foundation 409 indicates duplicate email (not other unique violations)."""
    lowered = detail.lower()
    return "email" in lowered and ("already" in lowered or "exists" in lowered)


def _user_id_from_body(body: Any) -> Optional[int]:
    if not isinstance(body, dict):
        return None
    for key in ("id", "userId", "user_id"):
        raw = body.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    return None


def _lookup_foundation_user_id_by_email(email: str) -> Optional[int]:
    """
    Resolve Foundation user id by email for vendor registration recovery.
    Prefers unauthenticated GET /users/by-email; falls back to JWT user-context.
    """
    normalized = email.strip()
    if not normalized:
        return None

    base = _nestjs_base()
    try:
        resp = requests.get(
            f"{base}/users/by-email",
            params={"email": normalized},
            headers={"Accept": "application/json"},
            timeout=_REGISTER_TIMEOUT_S,
        )
        if resp.ok:
            user_id = _user_id_from_body(_parse_json_body(resp))
            if user_id is not None:
                return user_id
        elif resp.status_code != 404:
            logger.warning(
                "[vendor-register] Foundation GET /users/by-email for %s: status=%s",
                normalized,
                resp.status_code,
            )
    except requests.RequestException as exc:
        logger.warning(
            "[vendor-register] Foundation GET /users/by-email failed for %s: %s",
            normalized,
            exc,
        )

    from services.auth_utils import _fetch_nest_user_context_by_email

    try:
        data = _fetch_nest_user_context_by_email(normalized)
    except Exception as exc:
        logger.warning(
            "[vendor-register] Foundation user-context lookup failed for %s: %s",
            normalized,
            exc,
        )
        return None

    return _user_id_from_body(data)


def _ensure_existing_foundation_vendor(
    base: str,
    *,
    email: str,
    password: str,
    company_id: int,
    tenant_id: int,
    phone_number: Optional[str] = None,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    user_id = _lookup_foundation_user_id_by_email(email)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Could not complete vendor registration in Foundation. "
                "Please retry in a moment or contact support if the problem persists."
            ),
        )

    patch: Dict[str, Any] = {
        "password": password,
        "companyId": company_id,
        "tenantId": tenant_id,
        "roleNames": [_VENDOR_ROLE_NAME],
    }
    if name:
        patch["name"] = name
    if phone_number:
        patch["phoneNumber"] = phone_number

    url = f"{base}/users/{user_id}"
    try:
        resp = requests.patch(
            url,
            json=patch,
            headers={"Accept": "application/json"},
            timeout=_REGISTER_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Foundation vendor role update failed: {exc}",
        ) from exc

    body = _parse_json_body(resp)
    if resp.status_code in (200, 201) and isinstance(body, dict):
        logger.info(
            "[vendor-register] Foundation user %s updated with vendor role",
            user_id,
        )
        return body

    detail = _extract_error_detail(
        body, fallback=resp.text or "Foundation vendor role update failed"
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=detail,
    )


def update_foundation_user_email(
    *,
    old_email: str,
    new_email: str,
) -> Optional[Dict[str, Any]]:
    """
    Update Foundation (Nest) user email via PATCH /users/{id}.
    Looks up the user by ``old_email``. Returns None when no Foundation user exists.
    """
    normalized_old = (old_email or "").strip()
    normalized_new = (new_email or "").strip()
    if not normalized_new:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New email is required",
        )
    if normalized_old.lower() == normalized_new.lower():
        return None

    user_id = _lookup_foundation_user_id_by_email(normalized_old)
    if user_id is None:
        logger.info(
            "[vendor-email] No Foundation user for %s; skipping Foundation email sync",
            normalized_old,
        )
        return None

    base = _nestjs_base()
    url = f"{base}/users/{user_id}"
    try:
        resp = requests.patch(
            url,
            json={"email": normalized_new},
            headers={"Accept": "application/json"},
            timeout=_REGISTER_TIMEOUT_S,
        )
    except requests.Timeout as exc:
        logger.error("[vendor-email] Foundation PATCH /users/%s timeout: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Foundation email update timed out",
        ) from exc
    except requests.RequestException as exc:
        logger.error(
            "[vendor-email] Foundation PATCH /users/%s failed: %s",
            user_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Foundation email update failed: {exc}",
        ) from exc

    body = _parse_json_body(resp)
    if resp.status_code in (200, 201) and isinstance(body, dict):
        logger.info(
            "[vendor-email] Foundation user %s email updated to %s",
            user_id,
            normalized_new,
        )
        return body

    detail = _extract_error_detail(
        body, fallback=resp.text or "Foundation email update failed"
    )
    if resp.status_code == 409 or _is_email_conflict(detail):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered in Foundation",
        )
    if resp.status_code == 404:
        logger.warning(
            "[vendor-email] Foundation user %s not found during email update",
            user_id,
        )
        return None

    logger.error(
        "[vendor-email] Foundation PATCH /users/%s status=%s detail=%s",
        user_id,
        resp.status_code,
        detail,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=detail,
    )


def delete_foundation_user_by_email(email: str) -> bool:
    """
    Hard-delete a Foundation user by email via DELETE /users/{id}.
    Returns True when a user was deleted, False when none exists or delete is skipped.
    """
    normalized = (email or "").strip()
    if not normalized:
        return False

    user_id = _lookup_foundation_user_id_by_email(normalized)
    if user_id is None:
        logger.info(
            "[vendor-delete] No Foundation user for %s; skipping Foundation delete",
            normalized,
        )
        return False

    base = _nestjs_base()
    url = f"{base}/users/{user_id}"
    try:
        resp = requests.delete(
            url,
            headers={"Accept": "application/json"},
            timeout=_REGISTER_TIMEOUT_S,
        )
    except requests.Timeout as exc:
        logger.error(
            "[vendor-delete] Foundation DELETE /users/%s timeout: %s",
            user_id,
            exc,
        )
        return False
    except requests.RequestException as exc:
        logger.error(
            "[vendor-delete] Foundation DELETE /users/%s failed: %s",
            user_id,
            exc,
        )
        return False

    if resp.status_code in (200, 204):
        logger.info(
            "[vendor-delete] Foundation user %s deleted (email=%s)",
            user_id,
            normalized,
        )
        return True

    if resp.status_code == 404:
        logger.info(
            "[vendor-delete] Foundation user %s already absent during delete",
            user_id,
        )
        return False

    body = _parse_json_body(resp)
    detail = _extract_error_detail(body, fallback=resp.text or "Foundation delete failed")
    logger.warning(
        "[vendor-delete] Foundation DELETE /users/%s status=%s detail=%s",
        user_id,
        resp.status_code,
        detail,
    )
    return False
