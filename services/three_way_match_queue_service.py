"""Build invoice-review 3-way match PO queue (PO must have invoice + GRN)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models import PurchaseOrderHeader
from services.grn_list_service import list_all_grn_headers, list_all_invoice_headers


def _clean_po_number(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_timestamp(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).timestamp()
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _format_date(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if len(text) >= 10 else text or "—"


def _format_amount(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    try:
        num = float(str(value).replace(",", ""))
        return f"{num:,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _sum_invoice_amounts(invoices: List[Dict[str, Any]]) -> Any:
    total = Decimal("0")
    has_value = False
    for inv in invoices:
        raw = inv.get("invoice_amount")
        if raw is None or raw == "":
            continue
        try:
            total += Decimal(str(raw).replace(",", ""))
            has_value = True
        except Exception:
            continue
    return total if has_value else None


def _sort_by_latest(items: List[Dict[str, Any]], *keys: str) -> List[Dict[str, Any]]:
    def sort_key(row: Dict[str, Any]) -> float:
        for key in keys:
            ts = _to_timestamp(row.get(key))
            if ts:
                return ts
        return 0.0

    return sorted(items, key=sort_key, reverse=True)


def _group_by_po(rows: List[Dict[str, Any]], po_key: str = "po_number") -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        po = _clean_po_number(row.get(po_key))
        if not po:
            continue
        grouped.setdefault(po, []).append(row)
    return grouped


def _load_po_headers(
    db: Optional[Session],
    *,
    tenant_id: int,
    company_id: int,
    po_numbers: List[str],
) -> Dict[str, PurchaseOrderHeader]:
    if db is None or not po_numbers:
        return {}
    headers = (
        db.query(PurchaseOrderHeader)
        .filter(
            PurchaseOrderHeader.tenant_id == tenant_id,
            PurchaseOrderHeader.company_id == company_id,
            PurchaseOrderHeader.po_number.in_(po_numbers),
        )
        .all()
    )
    return {header.po_number: header for header in headers if header.po_number}


def list_three_way_match_queue(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str = "IN",
    status: Optional[str] = "DRAFT",
    vendor_id: Optional[int] = None,
    page: int = 1,
    limit: int = 100,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """
    Return POs eligible for 3-way match review: must have at least one invoice
    (matching document_type/status) and at least one GRN for the same PO number.
    """
    invoices = list_all_invoice_headers(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        status=status,
        vendor_id=vendor_id,
    )
    grns = list_all_grn_headers(
        tenant_id=tenant_id,
        company_id=company_id,
        vendor_id=vendor_id,
    )

    invoices_by_po = _group_by_po(invoices)
    grns_by_po = _group_by_po(grns)

    eligible_po_numbers = sorted(
        set(invoices_by_po.keys()) & set(grns_by_po.keys()),
        key=lambda po: max(
            _to_timestamp(_sort_by_latest(invoices_by_po[po], "invoice_date")[0].get("invoice_date")),
            _to_timestamp(_sort_by_latest(grns_by_po[po], "grn_date")[0].get("grn_date")),
        ),
        reverse=True,
    )

    po_headers = _load_po_headers(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        po_numbers=eligible_po_numbers,
    )

    queue_rows: List[Dict[str, Any]] = []
    for po_number in eligible_po_numbers:
        po_invoices = _sort_by_latest(invoices_by_po[po_number], "invoice_date")
        po_grns = _sort_by_latest(grns_by_po[po_number], "grn_date")
        primary_invoice = po_invoices[0]
        primary_grn = po_grns[0]
        po_header = po_headers.get(po_number)

        invoice_no = str(primary_invoice.get("invoice_no") or "").strip()
        grn_no = str(primary_grn.get("grn_number") or "").strip()
        invoice_count = len(po_invoices)
        grn_count = len(po_grns)

        display_invoice_no = invoice_no or "—"
        if invoice_count > 1 and invoice_no:
            display_invoice_no = f"{invoice_no} (+{invoice_count - 1})"

        display_grn_no = grn_no or "—"
        if grn_count > 1 and grn_no:
            display_grn_no = f"{grn_no} (+{grn_count - 1})"

        po_id = (
            primary_invoice.get("po_id")
            or primary_grn.get("po_id")
            or (po_header.po_id if po_header is not None else None)
        )
        vendor = (
            primary_invoice.get("vendor_id")
            or primary_grn.get("vendor_id")
            or (po_header.vendor_id if po_header is not None else None)
        )

        queue_rows.append(
            {
                "id": po_number,
                "po_number": po_number,
                "po_id": str(po_id) if po_id is not None else "—",
                "vendor_id": str(vendor) if vendor is not None else "—",
                "invoice_no": display_invoice_no,
                "invoice_count": invoice_count,
                "grn_no": display_grn_no,
                "grn_count": grn_count,
                "invoice_date": _format_date(primary_invoice.get("invoice_date")),
                "grn_date": _format_date(primary_grn.get("grn_date")),
                "invoice_amount": _format_amount(
                    _sum_invoice_amounts(po_invoices) or primary_invoice.get("invoice_amount")
                ),
                "currency_code": (
                    primary_invoice.get("currency_code")
                    or (po_header.currency_code if po_header is not None else None)
                    or "—"
                ),
                "status": (
                    (po_header.status if po_header is not None else None)
                    or primary_invoice.get("status")
                    or primary_grn.get("status")
                    or "—"
                ),
            }
        )

    total = len(queue_rows)
    skip = max(0, (max(1, page) - 1) * limit)
    take = max(1, min(limit, 1000))
    page_items = queue_rows[skip : skip + take]

    return {
        "items": page_items,
        "total": total,
        "take": take,
        "skip": skip,
    }
