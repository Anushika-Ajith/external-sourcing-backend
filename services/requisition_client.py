"""Client for internal sourcing requisition creation.

Maps PO lines with a positive RFQ quantity into requisition lines, builds the
create-full payload and posts it with the caller's JWT. Failures are surfaced
as ``RequisitionClientError`` so callers can log them without failing a PO.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Mapping, Sequence

import requests

from config import settings
from services.object_reservation_client import _auth_headers

DEFAULT_DEPARTMENT_ID = 3  # TODO: make dynamic


class RequisitionClientError(Exception):
    """Network, HTTP or response-JSON failure from the requisition API."""


def map_po_lines_to_requisition_lines(lines: Sequence[Any]) -> List[Dict[str, Any]]:
    """Pass through PO lines with ``rfq_qty > 0`` as requisition lines."""
    requisition_lines: List[Dict[str, Any]] = []
    for line in lines:
        rfq_qty = line.rfq_qty
        if rfq_qty is None or rfq_qty <= 0:
            continue
        requisition_lines.append(
            {
                "itemId": line.item_id,
                "itemNo": line.item_no,
                "description": line.description,
                "departmentId": DEFAULT_DEPARTMENT_ID,
                "qtyRequested": int(rfq_qty),
            }
        )
    return requisition_lines


def build_requisition_payload(
    *,
    tenant_id: int,
    company_id: int,
    requester_id: int,
    created_by: int,
    requisition_date: date,
    lines: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Build the create-full body. ``lines`` are already-mapped requisition lines."""
    return {
        "tenantId": tenant_id,
        "companyId": company_id,
        "requesterId": requester_id,
        "createdBy": created_by,
        "requisitionDate": requisition_date.strftime("%Y-%m-%d"),
        "documentType": "REQ",
        "lines": list(lines),
    }


def create_requisition(
    *,
    payload: Dict[str, Any],
    bearer_token: str,
    timeout: int = 30,
) -> Dict[str, Any]:
    """POST the payload to internal sourcing and return the parsed response."""
    url = settings.internal_sourcing_api_url.rstrip("/") + "/requisition-header/create-full"
    try:
        resp = requests.post(
            url,
            headers=_auth_headers(bearer_token),
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RequisitionClientError(f"Requisition API request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise RequisitionClientError(
            f"Requisition API returned HTTP {resp.status_code}"
        )

    try:
        body = resp.json()
    except (ValueError, TypeError) as exc:
        raise RequisitionClientError("Requisition API returned invalid JSON") from exc

    if not isinstance(body, dict):
        raise RequisitionClientError(
            "Requisition API returned unexpected JSON (expected an object)"
        )
    return body
