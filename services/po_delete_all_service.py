"""Delete all purchase orders and related ERP GRN/invoice records."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import requests
from sqlalchemy.orm import Session

from config import settings
from models import (
    DocumentItemEmbedding,
    PurchaseOrderHeader,
    PurchaseOrderHeaderHistory,
    PurchaseOrderLine,
    PurchaseOrderLineHistory,
    User,
)
from services.company_scope import resolve_company_id

logger = logging.getLogger(__name__)


def _internal_api_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("INTERNAL_API_URL is not configured")
    return base


def _delete_erp_by_po_scope(
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    po_numbers: Sequence[str],
    po_ids: Sequence[int],
) -> Dict[str, Any]:
    params: Dict[str, str] = {}
    if tenant_id is not None:
        params["tenantId"] = str(tenant_id)
    if company_id is not None:
        params["companyId"] = str(company_id)
    if po_numbers:
        params["poNumbers"] = ",".join(po_numbers)
    if po_ids:
        params["poIds"] = ",".join(str(value) for value in po_ids)

    base = _internal_api_base()
    grn_url = f"{base}/grn-header/by-po-scope"
    invoice_url = f"{base}/invoice-header/by-po-scope"

    grn_result: Dict[str, Any] = {"deletedHeaders": 0, "deletedLines": 0}
    invoice_result: Dict[str, Any] = {"deletedHeaders": 0, "deletedLines": 0}

    try:
        grn_response = requests.delete(grn_url, params=params, timeout=60)
        if grn_response.ok:
            grn_result = grn_response.json()
        else:
            logger.warning(
                "GRN bulk delete failed status=%s body=%s",
                grn_response.status_code,
                grn_response.text,
            )
    except requests.RequestException as exc:
        logger.warning("GRN bulk delete request failed: %s", exc)

    try:
        invoice_response = requests.delete(invoice_url, params=params, timeout=60)
        if invoice_response.ok:
            invoice_result = invoice_response.json()
        else:
            logger.warning(
                "Invoice bulk delete failed status=%s body=%s",
                invoice_response.status_code,
                invoice_response.text,
            )
    except requests.RequestException as exc:
        logger.warning("Invoice bulk delete request failed: %s", exc)

    return {
        "grn": grn_result,
        "invoice": invoice_result,
    }


def delete_all_purchase_orders(
    db: Session,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    vendor_id: Optional[int],
    current_user: User,
) -> Dict[str, int]:
    company_id = resolve_company_id(company_id)

    _ = current_user
    header_q = db.query(PurchaseOrderHeader)
    if tenant_id is not None:
        header_q = header_q.filter(PurchaseOrderHeader.tenant_id == tenant_id)
    if company_id is not None:
        header_q = header_q.filter(PurchaseOrderHeader.company_id == company_id)
    if vendor_id is not None:
        header_q = header_q.filter(PurchaseOrderHeader.vendor_id == vendor_id)

    headers: List[PurchaseOrderHeader] = header_q.all()
    if not headers:
        return {
            "deleted": 0,
            "deleted_po_lines": 0,
            "deleted_grn_headers": 0,
            "deleted_grn_lines": 0,
            "deleted_invoice_headers": 0,
            "deleted_invoice_lines": 0,
        }

    po_numbers = sorted({str(row.po_number) for row in headers if row.po_number})
    po_ids = sorted({int(row.po_id) for row in headers if row.po_id is not None})

    erp_result = _delete_erp_by_po_scope(
        tenant_id=tenant_id,
        company_id=company_id,
        po_numbers=po_numbers,
        po_ids=po_ids,
    )

    line_q = db.query(PurchaseOrderLine)
    if tenant_id is not None:
        line_q = line_q.filter(PurchaseOrderLine.tenant_id == tenant_id)
    if company_id is not None:
        line_q = line_q.filter(PurchaseOrderLine.company_id == company_id)
    if po_numbers:
        line_q = line_q.filter(PurchaseOrderLine.po_number.in_(po_numbers))
    deleted_po_lines = line_q.delete(synchronize_session=False)

    embedding_q = db.query(DocumentItemEmbedding)
    if tenant_id is not None:
        embedding_q = embedding_q.filter(DocumentItemEmbedding.tenant_id == tenant_id)
    if company_id is not None:
        embedding_q = embedding_q.filter(DocumentItemEmbedding.company_id == company_id)
    if po_numbers:
        embedding_q = embedding_q.filter(
            DocumentItemEmbedding.source_document_no.in_(po_numbers)
        )
    embedding_q.delete(synchronize_session=False)

    line_history_q = db.query(PurchaseOrderLineHistory)
    if tenant_id is not None:
        line_history_q = line_history_q.filter(
            PurchaseOrderLineHistory.tenant_id == tenant_id
        )
    if company_id is not None:
        line_history_q = line_history_q.filter(
            PurchaseOrderLineHistory.company_id == company_id
        )
    if po_numbers:
        line_history_q = line_history_q.filter(
            PurchaseOrderLineHistory.po_number.in_(po_numbers)
        )
    line_history_q.delete(synchronize_session=False)

    header_history_q = db.query(PurchaseOrderHeaderHistory)
    if tenant_id is not None:
        header_history_q = header_history_q.filter(
            PurchaseOrderHeaderHistory.tenant_id == tenant_id
        )
    if company_id is not None:
        header_history_q = header_history_q.filter(
            PurchaseOrderHeaderHistory.company_id == company_id
        )
    if po_numbers:
        header_history_q = header_history_q.filter(
            PurchaseOrderHeaderHistory.po_number.in_(po_numbers)
        )
    header_history_q.delete(synchronize_session=False)

    deleted_headers = header_q.delete(synchronize_session=False)
    db.commit()

    grn = erp_result.get("grn") or {}
    invoice = erp_result.get("invoice") or {}

    return {
        "deleted": int(deleted_headers or 0),
        "deleted_po_lines": int(deleted_po_lines or 0),
        "deleted_grn_headers": int(grn.get("deletedHeaders") or 0),
        "deleted_grn_lines": int(grn.get("deletedLines") or 0),
        "deleted_invoice_headers": int(invoice.get("deletedHeaders") or 0),
        "deleted_invoice_lines": int(invoice.get("deletedLines") or 0),
    }
