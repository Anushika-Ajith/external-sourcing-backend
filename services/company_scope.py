"""Canonical company scope for external-sourcing-backend APIs."""

from __future__ import annotations

from typing import Any, Optional

from config import settings

# Hard default for this deployment (stock / PO / OCR / RFQ callers).
_FALLBACK_COMPANY_ID = 3


def default_company_id() -> int:
    """Configured company id used across APIs (defaults to 3)."""
    try:
        value = int(getattr(settings, "default_company_id", _FALLBACK_COMPANY_ID))
    except (TypeError, ValueError):
        value = _FALLBACK_COMPANY_ID
    return value if value > 0 else _FALLBACK_COMPANY_ID


def resolve_company_id(value: Any = None) -> int:
    """
    Always return the deployment company id (3).

    Client/JWT/query values are ignored so list/create paths stay consistent
    (e.g. stock uploaded under company 3 is visible on stock list).
    """
    _ = value  # intentionally unused — force single-company scope
    return default_company_id()
