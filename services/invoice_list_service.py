"""List invoice headers via ERP internal API."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from config import settings

logger = logging.getLogger(__name__)

PENDING_INVOICE_STATUS = "DRAFT"


def _internal_api_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("INTERNAL_API_URL is not configured")
    return base


def _pick_int(row: Dict[str, Any], *keys: str, default: Optional[int] = None) -> Optional[int]:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def _pick_str(row: Dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def normalize_invoice_header(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize ERP camelCase invoice header into snake_case."""
    return {
        "tenant_id": _pick_int(row, "tenant_id", "tenantId", default=0) or 0,
        "company_id": _pick_int(row, "company_id", "companyId", default=0) or 0,
        "document_type": _pick_str(row, "document_type", "documentType", default="IN"),
        "invoice_no": _pick_str(row, "invoice_no", "invoiceNo"),
        "version_no": _pick_int(row, "version_no", "versionNo", default=1) or 1,
        "vendor_id": _pick_int(row, "vendor_id", "vendorId", default=0) or 0,
        "po_number": _pick_str(row, "po_number", "poNumber"),
        "po_id": _pick_int(row, "po_id", "poId"),
        "invoice_date": row.get("invoice_date") or row.get("invoiceDate"),
        "invoice_amount": row.get("invoice_amount") or row.get("invoiceAmount"),
        "currency_code": row.get("currency_code") or row.get("currencyCode"),
        "status": row.get("status"),
        "remarks": row.get("remarks"),
        "attachment_path": row.get("attachment_path") or row.get("attachmentPath"),
    }


def list_invoice_headers(
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    document_type: str = "IN",
    status: Optional[str] = None,
    vendor_id: Optional[int] = None,
    page: int = 1,
    limit: int = 100,
) -> Dict[str, Any]:
    """
    GET {INTERNAL_API_URL}/invoice-header

    Returns ``{ items, total, take, skip }`` with snake_case items.
    """
    url = f"{_internal_api_base()}/invoice-header"
    params: Dict[str, str] = {
        "page": str(max(1, page)),
        "limit": str(max(1, min(limit, 1000))),
        "documentType": document_type,
    }
    if tenant_id is not None:
        params["tenantId"] = str(tenant_id)
    if company_id is not None:
        params["companyId"] = str(company_id)
    if status:
        params["status"] = status
    if vendor_id is not None:
        params["vendorId"] = str(vendor_id)

    response = requests.get(url, params=params, timeout=60)
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
            detail or f"List invoices failed ({response.status_code})"
        )

    data = response.json()
    if isinstance(data, list):
        items_raw: List[Any] = data
        total = len(items_raw)
        take = limit
        skip = max(0, (page - 1) * limit)
    elif isinstance(data, dict):
        raw = data.get("items")
        items_raw = raw if isinstance(raw, list) else []
        total = int(data.get("total") or len(items_raw))
        take = int(data.get("take") or limit)
        skip = int(data.get("skip") or max(0, (page - 1) * limit))
    else:
        items_raw = []
        total = 0
        take = limit
        skip = max(0, (page - 1) * limit)

    items = [
        normalize_invoice_header(row)
        for row in items_raw
        if isinstance(row, dict)
    ]
    return {"items": items, "total": total, "take": take, "skip": skip}
