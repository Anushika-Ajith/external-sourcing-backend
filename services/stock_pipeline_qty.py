"""Pipeline qty = open PO qty − GRN received qty, grouped by stock product_code."""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Optional, Sequence, Tuple

import requests
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import settings
from models import PurchaseOrderLine

logger = logging.getLogger(__name__)

PoLineKey = Tuple[str, int]


def _to_decimal(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _normalize_product_code(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def _internal_api_base() -> Optional[str]:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    return base or None


def fetch_grn_received_by_po_line(
    *,
    tenant_id: int,
    company_id: int,
) -> Dict[PoLineKey, Decimal]:
    """Sum ERP GRN qty_received by (po_number, po_line_no)."""
    base = _internal_api_base()
    if not base:
        return {}

    url = f"{base}/grn-header/with-lines"
    params = {
        "tenantId": str(tenant_id),
        "companyId": str(company_id),
        "limit": "500",
        "page": "1",
    }

    try:
        response = requests.get(url, params=params, timeout=60)
        if response.status_code >= 400:
            logger.warning(
                "[PIPELINE] GRN fetch failed status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return {}
        payload = response.json()
    except Exception as exc:
        logger.warning("[PIPELINE] GRN fetch error: %s", exc)
        return {}

    items = payload if isinstance(payload, list) else payload.get("items") or []
    totals: Dict[PoLineKey, Decimal] = defaultdict(lambda: Decimal("0"))

    for header in items:
        if not isinstance(header, dict):
            continue
        po_number = str(header.get("poNumber") or "").strip().upper()
        if not po_number:
            continue
        for line in header.get("lines") or []:
            if not isinstance(line, dict):
                continue
            po_line_no = line.get("poLineNo")
            if po_line_no is None:
                continue
            try:
                line_no = int(po_line_no)
            except (TypeError, ValueError):
                continue
            received = _to_decimal(line.get("qtyReceived"))
            totals[(po_number, line_no)] += received

    return dict(totals)


def compute_pipeline_qty_by_product_codes(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    product_codes: Sequence[str],
) -> Dict[str, Decimal]:
    """
    Return pipeline qty keyed by the original product_code string from stock.

    Matches PO lines on item_no (case-insensitive) and subtracts GRN received
    (max of PO line qty_received and ERP GRN totals).
    """
    code_lookup: Dict[str, str] = {}
    for code in product_codes:
        normalized = _normalize_product_code(code)
        if normalized and normalized not in code_lookup:
            code_lookup[normalized] = code.strip()

    if not code_lookup:
        return {}

    normalized_codes = list(code_lookup.keys())
    po_lines = (
        db.query(PurchaseOrderLine)
        .filter(
            PurchaseOrderLine.tenant_id == tenant_id,
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrderLine.item_no.isnot(None),
            func.upper(func.trim(PurchaseOrderLine.item_no)).in_(normalized_codes),
        )
        .all()
    )

    grn_by_po_line = fetch_grn_received_by_po_line(
        tenant_id=tenant_id,
        company_id=company_id,
    )

    totals: Dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for line in po_lines:
        normalized_item = _normalize_product_code(line.item_no)
        original_code = code_lookup.get(normalized_item)
        if not original_code:
            continue

        po_qty = _to_decimal(line.qty_ordered)
        received = _to_decimal(line.qty_received)
        po_number = str(line.po_number or "").strip().upper()
        try:
            po_line_no = int(line.line_no)
        except (TypeError, ValueError):
            continue
        erp_received = grn_by_po_line.get((po_number, po_line_no), Decimal("0"))
        received = max(received, erp_received)
        pipeline = po_qty - received
        if pipeline > 0:
            totals[original_code] += pipeline

    return dict(totals)


def enrich_stock_quantities(
    *,
    available_stock: object,
    reorder_level: object,
    pipeline_qty: object,
) -> Dict[str, Decimal | bool]:
    available = _to_decimal(available_stock)
    reorder = _to_decimal(reorder_level)
    pipeline = _to_decimal(pipeline_qty)
    stock_with_pipeline = available + pipeline
    needs_reorder = reorder > 0 and stock_with_pipeline < reorder
    return {
        "pipeline_qty": pipeline.quantize(Decimal("0.001")),
        "stock_with_pipeline": stock_with_pipeline.quantize(Decimal("0.001")),
        "needs_reorder": needs_reorder,
    }
