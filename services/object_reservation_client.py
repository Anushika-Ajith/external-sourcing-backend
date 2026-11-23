"""HTTP client for Foundation object_reservation (edit locks).

Vendors live in external-sourcing Postgres; reservations live in foundation_db.
Save flows verify then mutate then release (best-effort cross-DB saga — not a
single Postgres transaction).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from fastapi import HTTPException, status

from config import settings

logger = logging.getLogger(__name__)

RESOURCE_TYPE_VENDOR = "vendor"


def vendor_resource_key(vendor_id: int) -> dict[str, int]:
    return {"vendor_id": int(vendor_id)}


def _nestjs_base() -> str:
    return (settings.nestjs_api_url or "").strip().rstrip("/")


def _auth_headers(bearer_token: str) -> dict[str, str]:
    token = (bearer_token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _detail_from_response(resp: requests.Response) -> Any:
    try:
        body = resp.json()
    except ValueError:
        return (resp.text or "").strip() or f"HTTP {resp.status_code}"
    if isinstance(body, dict):
        msg = body.get("message")
        if isinstance(msg, dict):
            return msg
        if msg is not None:
            return msg
        if body.get("detail") is not None:
            return body.get("detail")
    return body


def assert_vendor_edit_reservation(
    *,
    vendor_id: int,
    bearer_token: str,
) -> dict[str, Any]:
    """
    POST /object-reservation/assert-owner

    Raises HTTP 409 when the caller does not hold an active reservation.
    """
    base = _nestjs_base()
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NESTJS_API_URL is not configured; cannot verify edit reservation",
        )
    if not (bearer_token or "").strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to save while edit locking is enabled",
        )

    url = f"{base}/object-reservation/assert-owner"
    try:
        resp = requests.post(
            url,
            headers=_auth_headers(bearer_token),
            json={
                "resourceType": RESOURCE_TYPE_VENDOR,
                "resourceKey": vendor_resource_key(vendor_id),
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.exception("assert_vendor_edit_reservation request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify edit reservation with foundation service",
        ) from exc

    if resp.status_code == 409:
        detail = _detail_from_response(resp)
        if isinstance(detail, dict):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "success": False,
                "message": str(detail)
                or "Your editing reservation is no longer valid. Please click Edit and try again.",
            },
        )

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=resp.status_code,
            detail=_detail_from_response(resp),
        )

    try:
        return resp.json() if resp.content else {}
    except ValueError:
        return {}


def release_vendor_edit_reservation(
    *,
    reservation_id: Optional[str],
    bearer_token: str,
) -> None:
    """
    DELETE /object-reservation/{id}/release — best-effort after successful save.
    """
    if not reservation_id or not (bearer_token or "").strip():
        return
    base = _nestjs_base()
    if not base:
        logger.warning("NESTJS_API_URL unset; cannot release reservation %s", reservation_id)
        return

    url = f"{base}/object-reservation/{reservation_id}/release"
    try:
        resp = requests.delete(
            url,
            headers=_auth_headers(bearer_token),
            timeout=30,
        )
        if resp.status_code >= 400 and resp.status_code not in (404,):
            logger.warning(
                "release_vendor_edit_reservation failed status=%s body=%s",
                resp.status_code,
                (resp.text or "")[:500],
            )
    except requests.RequestException as exc:
        logger.warning(
            "release_vendor_edit_reservation request error for %s: %s",
            reservation_id,
            exc,
        )


def release_vendor_edit_reservation_by_key(
    *,
    vendor_id: int,
    bearer_token: str,
) -> None:
    """
    Look up reservation for vendor and release if owned by the caller.
    Used when the save request does not include reservation_id.
    """
    base = _nestjs_base()
    if not base or not (bearer_token or "").strip():
        return

    lookup_url = f"{base}/object-reservation/lookup"
    try:
        resp = requests.post(
            lookup_url,
            headers=_auth_headers(bearer_token),
            json={
                "resourceType": RESOURCE_TYPE_VENDOR,
                "resourceKey": vendor_resource_key(vendor_id),
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.warning("lookup reservation for release failed: %s", exc)
        return

    if resp.status_code == 404:
        return
    if resp.status_code >= 400:
        logger.warning(
            "lookup reservation for release status=%s body=%s",
            resp.status_code,
            (resp.text or "")[:300],
        )
        return

    try:
        body = resp.json()
    except ValueError:
        return
    reservation_id = body.get("reservationId") or body.get("reservation_id")
    if reservation_id:
        release_vendor_edit_reservation(
            reservation_id=str(reservation_id),
            bearer_token=bearer_token,
        )
