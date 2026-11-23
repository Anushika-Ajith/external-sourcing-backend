"""
Handoff-token utilities for the Foundation/Auth API.

A handoff token is a short-lived JWT (RS256 — same key as access tokens)
that carries the **identity** of the authenticated user plus the **target
application** they should be handed off to. Frontends never mint these
tokens; they ask the Foundation API to create one for them while presenting
their existing access token.

To enforce one-time use we record the handoff JWT's `jti` claim in Redis
(`SET NX EX`) on exchange. If a second exchange attempt arrives, the SET
fails and we reject the request.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

import redis  # synchronous client, fine inside a sync auth handler

from config import settings
from services.auth_utils import _jwt_signing_key, _jwt_verification_key


logger = logging.getLogger(__name__)

# Default TTL: `Settings.handoff_token_ttl_seconds` (env `HANDOFF_TOKEN_TTL_SECONDS`).
HANDOFF_TOKEN_TYPE = "handoff"

# Redis key prefix that records handoff JTIs already exchanged.
_HANDOFF_USED_PREFIX = "auth:handoff:used:"


def _redis_client() -> Optional[redis.Redis]:
    """Best-effort sync Redis client. Returns None if Redis is unavailable."""
    url = settings.redis_url
    if not url:
        return None
    try:
        client = redis.Redis.from_url(url, decode_responses=True)
        # Ping to fail fast if Redis is unreachable.
        client.ping()
        return client
    except Exception as exc:  # pragma: no cover - depends on env
        print(f"[handoff_tokens] Redis unavailable: {exc}")
        return None


def _utc_epoch_seconds() -> int:
    """UTC unix timestamp (must match jose JWT exp validation, which uses UTC)."""
    return int(datetime.now(timezone.utc).timestamp())


def create_handoff_token(
    *,
    user_id: int,
    email: Optional[str],
    role: Optional[str],
    target_app: str,
    return_to: Optional[str] = None,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    ttl_seconds: Optional[int] = None,
) -> tuple[str, int]:
    """
    Mint a short-lived handoff JWT for `user_id` targeting `target_app`.

    Optional ``tenant_id`` / ``company_id`` are embedded in the JWT (snake_case and
    camelCase aliases) and forwarded into the access token on exchange.

    Returns (token, expires_in_seconds).
    """
    effective_ttl = (
        int(ttl_seconds)
        if ttl_seconds is not None
        else int(settings.handoff_token_ttl_seconds)
    )
    iat = _utc_epoch_seconds()
    exp = iat + int(effective_ttl)
    jti = secrets.token_urlsafe(16)

    payload = {
        "sub": str(user_id),
        "user_id": int(user_id),
        "email": email,
        "role": role,
        "targetApp": target_app,
        "target_app": target_app,  # snake_case alias for convenience
        "return_to": return_to,
        "typ": HANDOFF_TOKEN_TYPE,
        "jti": jti,
        "iat": iat,
        "exp": exp,
    }

    if tenant_id is not None:
        tid = int(tenant_id)
        payload["tenant_id"] = tid
        payload["tenantId"] = tid
    if company_id is not None:
        cid = int(company_id)
        payload["company_id"] = cid
        payload["companyId"] = cid

    token = jwt.encode(payload, _jwt_signing_key(), algorithm=settings.ALGORITHM)
    return token, effective_ttl


def _unverified_claims_snapshot(token: str) -> dict[str, Any]:
    """Parse JWT payload without verifying (diagnostics only)."""
    try:
        raw = jwt.get_unverified_claims(token)
        return dict(raw) if isinstance(raw, dict) else {}
    except Exception:
        return {}


def consume_handoff_token(handoff_token: str, *, expected_target_app: Optional[str] = None) -> dict:
    """
    Validate a handoff JWT, atomically mark its `jti` as used in Redis, and
    return its decoded payload.

    Raises HTTPException(401) on any failure (invalid signature, expired,
    replay, wrong token type, target_app mismatch).
    """
    snap = _unverified_claims_snapshot(handoff_token)
    now_ts = _utc_epoch_seconds()
    exp_ts = int(snap.get("exp") or 0)

    decode_options = {
        "verify_signature": True,
        "verify_exp": True,
        "leeway": int(settings.handoff_jwt_leeway_seconds),
    }

    try:
        payload = jwt.decode(
            handoff_token,
            _jwt_verification_key(),
            algorithms=[settings.ALGORITHM],
            options=decode_options,
        )
    except ExpiredSignatureError as exc:
        logger.warning(
            "handoff consume: JWT signature valid but expired | jti=%r typ=%r exp=%s iat=%s "
            "now_utc=%s seconds_past_exp=%s sub=%r",
            snap.get("jti"),
            snap.get("typ"),
            snap.get("exp"),
            snap.get("iat"),
            now_ts,
            now_ts - exp_ts if exp_ts else None,
            snap.get("sub"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid handoff token: {exc}",
        ) from exc
    except JWTError as exc:
        logger.warning(
            "handoff consume: JWT verification failed | exc_type=%s exc=%s | "
            "unverified jti=%r typ=%r exp=%s iat=%s sub=%r (payload may be tampered)",
            type(exc).__name__,
            exc,
            snap.get("jti"),
            snap.get("typ"),
            snap.get("exp"),
            snap.get("iat"),
            snap.get("sub"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid handoff token: {exc}",
        ) from exc

    if payload.get("typ") != HANDOFF_TOKEN_TYPE:
        logger.warning(
            "handoff consume: wrong typ | got=%r want=%r jti=%r",
            payload.get("typ"),
            HANDOFF_TOKEN_TYPE,
            payload.get("jti"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a handoff token",
        )

    target_app = payload.get("target_app") or payload.get("targetApp")
    if not target_app:
        logger.warning(
            "handoff consume: missing target_app | jti=%r keys=%s",
            payload.get("jti"),
            list(payload.keys()),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Handoff token missing target_app",
        )

    if expected_target_app and target_app != expected_target_app:
        logger.warning(
            "handoff consume: target_app mismatch | expected=%r got=%r jti=%r",
            expected_target_app,
            target_app,
            payload.get("jti"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Handoff token target_app mismatch "
                f"(expected {expected_target_app!r}, got {target_app!r})"
            ),
        )

    jti = payload.get("jti")
    if not jti:
        logger.warning(
            "handoff consume: missing jti | sub=%r typ=%r",
            payload.get("sub"),
            payload.get("typ"),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Handoff token missing jti",
        )

    # One-time-use enforcement via Redis. If Redis is unreachable we fall back
    # to "best effort" (still safe because the JWT is signed + short-lived),
    # but we log it loudly.
    client = _redis_client()
    if client is not None:
        # SET key value NX EX ttl — returns True only if the key did not exist.
        # We keep the marker for a few seconds after exp to cover clock skew.
        exp_ts = int(payload.get("exp", 0))
        ttl = max(60, exp_ts - _utc_epoch_seconds() + 30)
        try:
            ok = client.set(f"{_HANDOFF_USED_PREFIX}{jti}", "1", nx=True, ex=ttl)
        except Exception as exc:  # pragma: no cover
            print(f"[handoff_tokens] Redis SET failed: {exc}")
            ok = True
        if not ok:
            logger.warning(
                "handoff consume: replay — jti already exchanged | jti=%r",
                jti,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Handoff token has already been used",
            )
    else:
        print(
            "[handoff_tokens] WARNING: Redis unavailable; "
            "handoff replay-protection disabled for jti="
            f"{jti}"
        )

    return payload
