"""List GRN headers via ERP internal API."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from config import settings
from services.invoice_list_service import normalize_invoice_header

logger = logging.getLogger(__name__)


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


def _pick_float(row: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def normalize_grn_line(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one ERP GRN line for WhatsApp / UI summaries."""
    line_no = _pick_int(row, "line_no", "lineNo", default=0) or 0
    item_name = _pick_str(
        row,
        "description",
        "item_name",
        "itemName",
        "product_name",
        "productName",
        "item_no",
        "itemNo",
        default=f"Line {line_no}" if line_no else "Line",
    )
    return {
        "line_no": line_no,
        "item_name": item_name,
        "item_no": _pick_str(row, "item_no", "itemNo") or None,
        "qty": _pick_float(
            row,
            "qty_received",
            "qtyReceived",
            "qty_accepted",
            "qtyAccepted",
            "qty_ordered",
            "qtyOrdered",
        ),
        "uom": _pick_str(row, "uom", "Uom", "UOM") or None,
        "unit_price": _pick_float(
            row, "unit_price", "unitPrice", "po_item_rate", "poItemRate"
        ),
        "line_amount": _pick_float(
            row, "total_amount", "totalAmount", "line_amount", "lineAmount"
        ),
    }


def normalize_grn_header(
    row: Dict[str, Any],
    *,
    include_lines: bool = False,
) -> Dict[str, Any]:
    """Normalize ERP camelCase GRN header into snake_case."""
    out: Dict[str, Any] = {
        "tenant_id": _pick_int(row, "tenant_id", "tenantId", default=0) or 0,
        "company_id": _pick_int(row, "company_id", "companyId", default=0) or 0,
        "document_type": _pick_str(row, "document_type", "documentType", default="GRN"),
        "grn_number": _pick_str(row, "grn_number", "grnNumber"),
        "version_no": _pick_int(row, "version_no", "versionNo", default=1) or 1,
        "vendor_id": _pick_int(row, "vendor_id", "vendorId", default=0) or 0,
        "po_number": _pick_str(row, "po_number", "poNumber"),
        "po_id": _pick_int(row, "po_id", "poId"),
        "grn_date": row.get("grn_date") or row.get("grnDate"),
        "status": row.get("status"),
        "inspection_status": row.get("inspection_status") or row.get("inspectionStatus"),
    }
    if include_lines:
        raw_lines = row.get("lines") or row.get("grn_lines") or row.get("grnLines") or []
        lines = [
            normalize_grn_line(line)
            for line in raw_lines
            if isinstance(line, dict)
        ]
        out["lines"] = lines
        out["line_count"] = len(lines)
    return out


def _fetch_paginated(url: str, params: Dict[str, str], *, max_items: int = 5000) -> List[Dict[str, Any]]:
    page = 1
    limit = min(1000, max_items)
    collected: List[Dict[str, Any]] = []

    while len(collected) < max_items:
        page_params = {**params, "page": str(page), "limit": str(limit)}
        response = requests.get(url, params=page_params, timeout=60)
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
            raise RuntimeError(detail or f"ERP list failed ({response.status_code})")

        data = response.json()
        if isinstance(data, list):
            batch = [row for row in data if isinstance(row, dict)]
            total = len(batch)
        elif isinstance(data, dict):
            raw = data.get("items")
            batch = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
            total = int(data.get("total") or len(batch))
        else:
            batch = []
            total = 0

        if not batch:
            break

        collected.extend(batch)
        if len(batch) < limit or len(collected) >= total:
            break
        page += 1

    return collected[:max_items]


def list_grn_headers(
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    document_type: str = "GRN",
    po_number: Optional[str] = None,
    vendor_id: Optional[int] = None,
    page: int = 1,
    limit: int = 100,
    include_lines: bool = False,
) -> Dict[str, Any]:
    """
    GET {INTERNAL_API_URL}/grn-header/with-lines

    Returns ``{ items, total, take, skip }`` with snake_case items.
    """
    url = f"{_internal_api_base()}/grn-header/with-lines"
    params: Dict[str, str] = {
        "page": str(max(1, page)),
        "limit": str(max(1, min(limit, 1000))),
        "documentType": document_type,
    }
    if tenant_id is not None:
        params["tenantId"] = str(tenant_id)
    if company_id is not None:
        params["companyId"] = str(company_id)
    if po_number:
        params["poNumber"] = po_number

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
        raise RuntimeError(detail or f"List GRNs failed ({response.status_code})")

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
        normalize_grn_header(row, include_lines=include_lines)
        for row in items_raw
        if isinstance(row, dict)
    ]
    if vendor_id is not None:
        items = [row for row in items if row.get("vendor_id") == vendor_id]

    return {"items": items, "total": total, "take": take, "skip": skip}


def list_all_grn_headers(
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    document_type: str = "GRN",
    vendor_id: Optional[int] = None,
    max_items: int = 5000,
) -> List[Dict[str, Any]]:
    """Fetch all GRN headers (paginated ERP calls) for queue building."""
    url = f"{_internal_api_base()}/grn-header/with-lines"
    params: Dict[str, str] = {"documentType": document_type}
    if tenant_id is not None:
        params["tenantId"] = str(tenant_id)
    if company_id is not None:
        params["companyId"] = str(company_id)

    rows = _fetch_paginated(url, params, max_items=max_items)
    items = [normalize_grn_header(row) for row in rows]
    if vendor_id is not None:
        items = [row for row in items if row.get("vendor_id") == vendor_id]
    return items


def list_all_invoice_headers(
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    document_type: str = "IN",
    status: Optional[str] = None,
    vendor_id: Optional[int] = None,
    max_items: int = 5000,
) -> List[Dict[str, Any]]:
    """Fetch all invoice headers (paginated ERP calls) for queue building."""
    url = f"{_internal_api_base()}/invoice-header"
    params: Dict[str, str] = {"documentType": document_type}
    if tenant_id is not None:
        params["tenantId"] = str(tenant_id)
    if company_id is not None:
        params["companyId"] = str(company_id)
    if status:
        params["status"] = status

    rows = _fetch_paginated(url, params, max_items=max_items)
    items = [normalize_invoice_header(row) for row in rows]
    if vendor_id is not None:
        items = [row for row in items if row.get("vendor_id") == vendor_id]
    return items
