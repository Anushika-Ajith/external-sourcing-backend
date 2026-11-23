"""Delete invoice headers and linked GRN records via ERP internal API."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from config import settings

logger = logging.getLogger(__name__)


def _internal_api_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("INTERNAL_API_URL is not configured")
    return base


def delete_invoice_with_linked_grn(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int = 1,
) -> Dict[str, Any]:
    """Delete one invoice and GRNs referenced on its lines."""
    url = f"{_internal_api_base()}/invoice-header/by-key/with-linked-grn"
    params = {
        "tenantId": str(tenant_id),
        "companyId": str(company_id),
        "documentType": document_type,
        "invoiceNo": invoice_no,
        "versionNo": str(version_no),
    }

    response = requests.delete(url, params=params, timeout=60)
    if response.status_code >= 400:
        detail: Optional[str]
        try:
            payload = response.json()
            detail = (
                payload.get("message")
                or payload.get("detail")
                or str(payload)
            )
        except Exception:
            detail = response.text.strip() or None
        raise RuntimeError(
            detail or f"Delete invoice failed ({response.status_code})"
        )

    data = response.json()
    if not isinstance(data, dict):
        return {"invoiceNo": invoice_no, "deletedGrnNumbers": []}
    return data
