"""
Short-lived password-reset tokens (JWT + optional Redis one-time JTI).

Mirrors the handoff-token pattern: RS256 JWT with typ=password_reset.
Redis marks jti as used on consume when available; without Redis, JWT expiry alone applies.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from fastapi import HTTPException, status
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

import redis

from config import settings
from services.auth_utils import _jwt_signing_key, _jwt_verification_key

logger = logging.getLogger(__name__)

PASSWORD_RESET_TOKEN_TYPE = "password_reset"
_RESET_USED_PREFIX = "auth:password_reset:used:"
_DEFAULT_TTL_SECONDS = 30 * 60  # 30 minutes


def _utc_epoch_seconds() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _redis_client() -> Optional[redis.Redis]:
    url = settings.redis_url
    if not url:
        return None
    try:
        client = redis.Redis.from_url(url, decode_responses=True)
        client.ping()
        return client
    except Exception as exc:  # pragma: no cover - depends on env
        logger.warning("[password-reset] Redis unavailable: %s", exc)
        return None


def password_reset_ttl_seconds() -> int:
    raw = getattr(settings, "password_reset_token_ttl_seconds", None)
    try:
        ttl = int(raw) if raw is not None else _DEFAULT_TTL_SECONDS
    except (TypeError, ValueError):
        ttl = _DEFAULT_TTL_SECONDS
    return max(60, ttl)


def create_password_reset_token(
    *,
    user_id: int,
    email: str,
    vendor_id: Optional[int] = None,
    ttl_seconds: Optional[int] = None,
) -> tuple[str, int]:
    """Mint a password-reset JWT. Returns (token, expires_in_seconds)."""
    effective_ttl = int(ttl_seconds) if ttl_seconds is not None else password_reset_ttl_seconds()
    iat = _utc_epoch_seconds()
    exp = iat + effective_ttl
    jti = secrets.token_urlsafe(16)
    normalized_email = (email or "").strip().lower()

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "user_id": int(user_id),
        "email": normalized_email,
        "typ": PASSWORD_RESET_TOKEN_TYPE,
        "jti": jti,
        "iat": iat,
        "exp": exp,
    }
    if vendor_id is not None:
        payload["vendor_id"] = int(vendor_id)

    token = jwt.encode(payload, _jwt_signing_key(), algorithm=settings.ALGORITHM)
    return token, effective_ttl


def build_password_reset_url(token: str) -> str:
    base = (settings.vendor_frontend_url or "http://localhost:3002").strip().rstrip("/")
    return f"{base}/vendor/reset-password?token={quote(token, safe='')}"


def consume_password_reset_token(token: str) -> dict[str, Any]:
    """
    Verify and consume a password-reset JWT.
    Raises HTTPException on invalid / expired / already-used tokens.
    """
    raw = (token or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token is required",
        )

    try:
        payload = jwt.decode(
            raw,
            _jwt_verification_key(),
            algorithms=[settings.ALGORITHM],
        )
    except ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset link has expired. Please request a new one.",
        ) from exc
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset link.",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset link.",
        )

    if (payload.get("typ") or "").strip() != PASSWORD_RESET_TOKEN_TYPE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset link.",
        )

    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid reset link.",
        )

    jti = (payload.get("jti") or "").strip()
    if jti:
        client = _redis_client()
        if client is not None:
            key = f"{_RESET_USED_PREFIX}{jti}"
            try:
                # SET NX EX — fail if already used
                ttl = max(60, int(payload.get("exp") or 0) - _utc_epoch_seconds())
                was_set = client.set(key, "1", nx=True, ex=ttl)
                if not was_set:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="This reset link has already been used.",
                    )
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning("[password-reset] Redis mark-used failed: %s", exc)

    return payload
