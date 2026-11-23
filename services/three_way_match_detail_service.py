"""
Server-side twin of UI `loadThreeWayMatchDetail` / `buildMatchSummary`
(external-sourcing-frontend/.../three-way-match-data.ts).

Loads local PO + ERP invoices/GRNs/dispatch notes and returns a WhatsApp-friendly
match summary. Does not change the UI queue or frontend match logic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config import settings
from models import PurchaseOrderHeader, PurchaseOrderLine
from services.company_scope import resolve_company_id


def _erp_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_API_URL is not configured",
        )
    return base


def _erp_error_detail(resp: requests.Response, fallback: str) -> str:
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            msg = payload.get("message") or payload.get("detail") or payload.get("error")
            if msg:
                return str(msg)
        return str(payload)[:300]
    except Exception:
        text = (resp.text or "").strip()
        return text[:300] if text else fallback


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


def _to_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _format_amount(value: Any) -> str:
    num = _to_number(value)
    if num is None:
        return "—" if value in (None, "") else str(value)
    return f"{num:,.2f}"


def _format_qty(value: Any) -> str:
    num = _to_number(value)
    if num is None:
        return "—"
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.4f}".rstrip("0").rstrip(".")


def _sum_qty(values: List[Any]) -> float:
    total = 0.0
    for value in values:
        n = _to_number(value)
        if n is not None:
            total += n
    return total


def _erp_list_by_po(path: str, *, po_number: str, tenant_id: int, company_id: int) -> List[Dict[str, Any]]:
    url = f"{_erp_base()}{path}"
    params = {
        "poNumber": po_number,
        "tenantId": tenant_id,
        "companyId": company_id,
    }
    resp = requests.get(url, params=params, timeout=60)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=_erp_error_detail(resp, f"ERP list failed for {path}"),
        )
    data = resp.json()
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        raw = data.get("items")
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)]
    return []


def list_invoices_for_po(*, po_number: str, tenant_id: int, company_id: int) -> List[Dict[str, Any]]:
    return _erp_list_by_po(
        "/purchase-order-header/invoices/by-po-number",
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
    )


def list_grns_for_po(*, po_number: str, tenant_id: int, company_id: int) -> List[Dict[str, Any]]:
    url = f"{_erp_base()}/grn-header/with-lines"
    params = {
        "poNumber": po_number,
        "tenantId": tenant_id,
        "companyId": company_id,
        "limit": "500",
        "page": "1",
    }
    resp = requests.get(url, params=params, timeout=60)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=_erp_error_detail(resp, "List GRNs by PO failed"),
        )
    data = resp.json()
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        raw = data.get("items")
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)]
    return []


def list_dispatch_notes_for_po(*, po_number: str, tenant_id: int, company_id: int) -> List[Dict[str, Any]]:
    return _erp_list_by_po(
        "/purchase-order-header/dispatch-notes/by-po-number",
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
    )


def _load_po_full(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    po_number: str,
) -> Optional[Dict[str, Any]]:
    header = (
        db.query(PurchaseOrderHeader)
        .filter(
            PurchaseOrderHeader.tenant_id == tenant_id,
            PurchaseOrderHeader.company_id == company_id,
            PurchaseOrderHeader.po_number == po_number,
        )
        .order_by(PurchaseOrderHeader.version_no.desc())
        .first()
    )
    if header is None:
        # Fallback: any tenant/company match on po_number.
        header = (
            db.query(PurchaseOrderHeader)
            .filter(PurchaseOrderHeader.po_number == po_number)
            .order_by(PurchaseOrderHeader.version_no.desc())
            .first()
        )
    if header is None:
        return None

    lines = (
        db.query(PurchaseOrderLine)
        .filter(
            PurchaseOrderLine.tenant_id == header.tenant_id,
            PurchaseOrderLine.company_id == header.company_id,
            PurchaseOrderLine.document_type == header.document_type,
            PurchaseOrderLine.po_number == header.po_number,
            PurchaseOrderLine.version_no == header.version_no,
        )
        .order_by(PurchaseOrderLine.line_no.asc())
        .all()
    )

    return {
        "tenant_id": header.tenant_id,
        "company_id": header.company_id,
        "document_type": header.document_type,
        "po_number": header.po_number,
        "version_no": header.version_no,
        "po_id": header.po_id,
        "vendor_id": header.vendor_id,
        "vendor_name_snapshot": header.vendor_name_snapshot,
        "currency_code": header.currency_code,
        "delivery_terms": header.delivery_terms,
        "expected_delivery_date": (
            header.expected_delivery_date.isoformat()
            if header.expected_delivery_date is not None
            else None
        ),
        "status": header.status,
        "approval_status": header.approval_status,
        "po_total_amount": header.po_total_amount,
        "tax_total_amount": header.tax_total_amount,
        "net_total_amount": header.net_total_amount,
        "created_at": header.created_at.isoformat() if header.created_at else None,
        "lines": [
            {
                "line_no": line.line_no,
                "line_id": line.line_id,
                "item_id": line.item_id,
                "item_no": line.item_no,
                "description": line.description,
                "qty_ordered": line.qty_ordered,
                "unit_price": line.unit_price,
                "tax_percent": line.tax_percent,
                "tax_amount": line.tax_amount,
                "total_amount": line.total_amount,
            }
            for line in lines
        ],
    }


def _item_key(value: Any) -> str:
    if value is None or value == "":
        return ""
    return str(value).strip()


def _find_po_line_for_grn(line: Dict[str, Any], po: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not po:
        return None
    po_lines = po.get("lines") if isinstance(po.get("lines"), list) else []
    po_line_no = _pick_int(line, "poLineNo", "po_line_no")
    if po_line_no is not None:
        for row in po_lines:
            if isinstance(row, dict) and _pick_int(row, "line_no", "lineNo") == po_line_no:
                return row
    item = _item_key(line.get("itemId") or line.get("item_id"))
    if not item:
        return None
    for row in po_lines:
        if not isinstance(row, dict):
            continue
        if _item_key(row.get("item_id") or row.get("item_no")) == item:
            return row
    return None


def _resolve_grn_unit_rate(line: Dict[str, Any], po: Optional[Dict[str, Any]]) -> Optional[float]:
    from_grn = _to_number(line.get("rate"))
    if from_grn is not None:
        return from_grn
    po_line = _find_po_line_for_grn(line, po)
    if po_line:
        return _to_number(po_line.get("unit_price"))
    return None


def _gst_multiplier_for_po(po: Optional[Dict[str, Any]]) -> float:
    if not po:
        return 1.0
    lines = po.get("lines") if isinstance(po.get("lines"), list) else []
    agreement_from_lines = _sum_qty(
        [
            (_to_number(row.get("qty_ordered")) or 0) * (_to_number(row.get("unit_price")) or 0)
            for row in lines
            if isinstance(row, dict)
        ]
    )
    agreement = _to_number(po.get("po_total_amount"))
    if agreement is None:
        agreement = agreement_from_lines
    gst = _to_number(po.get("tax_total_amount")) or 0.0
    if agreement and agreement > 0 and gst > 0:
        return 1.0 + (gst / agreement)
    return 1.0


def _compute_grn_line_amount(line: Dict[str, Any], po: Optional[Dict[str, Any]]) -> float:
    qty = _to_number(line.get("qtyAccepted") or line.get("qty_accepted") or line.get("qtyReceived") or line.get("qty_received")) or 0.0
    rate = _resolve_grn_unit_rate(line, po) or 0.0
    taxable = qty * rate
    if taxable == 0:
        return 0.0

    po_line = _find_po_line_for_grn(line, po)
    tax_pct = _to_number(po_line.get("tax_percent")) if po_line else None
    if tax_pct is not None:
        return taxable * (1.0 + tax_pct / 100.0)

    ordered_qty = _to_number(po_line.get("qty_ordered")) if po_line else None
    line_tax = _to_number(po_line.get("tax_amount")) if po_line else None
    if ordered_qty and ordered_qty > 0 and line_tax is not None:
        return taxable + (line_tax * qty) / ordered_qty

    return taxable * _gst_multiplier_for_po(po)


def _compute_grn_amount_total(grns: List[Dict[str, Any]], po: Optional[Dict[str, Any]]) -> float:
    total = 0.0
    for header in grns:
        lines = header.get("lines") if isinstance(header.get("lines"), list) else []
        for line in lines:
            if isinstance(line, dict):
                total += _compute_grn_line_amount(line, po)
    return total


def _compute_invoice_amount_total(
    po: Optional[Dict[str, Any]],
    invoices: List[Dict[str, Any]],
) -> Dict[str, Any]:
    lines = (po.get("lines") if po and isinstance(po.get("lines"), list) else []) or []
    agreement_from_lines = _sum_qty(
        [
            (_to_number(row.get("qty_ordered")) or 0) * (_to_number(row.get("unit_price")) or 0)
            for row in lines
            if isinstance(row, dict)
        ]
    )
    agreement_amt = _to_number(po.get("po_total_amount")) if po else None
    if agreement_amt is None:
        agreement_amt = agreement_from_lines if agreement_from_lines > 0 else 0.0

    gst_amt = _to_number(po.get("tax_total_amount")) if po else None
    if gst_amt is None:
        gst_amt = _sum_qty(
            [row.get("tax_amount") for row in lines if isinstance(row, dict)]
        )

    invoice_from_docs = _sum_qty(
        [
            inv.get("invoiceAmount") or inv.get("invoice_amount")
            for inv in invoices
        ]
    )
    if invoices and invoice_from_docs > 0:
        return {
            "agreementAmt": agreement_amt or 0.0,
            "gstAmt": gst_amt or 0.0,
            "invoiceAmt": invoice_from_docs,
            "source": "invoice",
        }

    net_from_po = _to_number(po.get("net_total_amount")) if po else None
    return {
        "agreementAmt": agreement_amt or 0.0,
        "gstAmt": gst_amt or 0.0,
        "invoiceAmt": net_from_po if net_from_po is not None else (agreement_amt or 0.0) + (gst_amt or 0.0),
        "source": "po",
    }


def _build_match_summary(
    po: Optional[Dict[str, Any]],
    grns: List[Dict[str, Any]],
    delivery_notes: List[Dict[str, Any]],
    invoices: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    if not po:
        return [{"kind": "warn", "text": "No purchase order found for this match."}]

    po_lines = po.get("lines") if isinstance(po.get("lines"), list) else []
    po_qty = _sum_qty([row.get("qty_ordered") for row in po_lines if isinstance(row, dict)])

    grn_qty_vals: List[Any] = []
    for header in grns:
        for line in header.get("lines") or []:
            if isinstance(line, dict):
                grn_qty_vals.append(
                    line.get("qtyAccepted")
                    or line.get("qty_accepted")
                    or line.get("qtyReceived")
                    or line.get("qty_received")
                )
    grn_qty = _sum_qty(grn_qty_vals)

    dn_qty = _sum_qty(
        [
            line.get("qtyInvoiced") or line.get("qty_invoiced")
            for header in delivery_notes
            for line in (header.get("lines") or [])
            if isinstance(line, dict)
        ]
    )
    inv_qty = _sum_qty(
        [
            line.get("qtyInvoiced") or line.get("qty_invoiced")
            for header in invoices
            for line in (header.get("lines") or [])
            if isinstance(line, dict)
        ]
    )

    lines: List[Dict[str, str]] = []
    amounts = _compute_invoice_amount_total(po, invoices)
    agreement_amt = float(amounts["agreementAmt"])
    gst_amt = float(amounts["gstAmt"])
    invoice_amt = float(amounts["invoiceAmt"])
    expected_invoice_amt = agreement_amt + gst_amt

    if abs(expected_invoice_amt - invoice_amt) < 0.01:
        lines.append(
            {
                "kind": "ok",
                "text": (
                    f"Invoice Amount = Agreement + GST "
                    f"(₹ {_format_amount(agreement_amt)} + ₹ {_format_amount(gst_amt)} "
                    f"= ₹ {_format_amount(invoice_amt)})"
                ),
            }
        )
    else:
        lines.append(
            {
                "kind": "warn",
                "text": (
                    f"Invoice Amount should be Agreement + GST : expected "
                    f"₹ {_format_amount(expected_invoice_amt)} vs invoice "
                    f"₹ {_format_amount(invoice_amt)}"
                ),
            }
        )

    if not grns:
        lines.append({"kind": "warn", "text": "No GRN found against this PO."})
    else:
        grn_amt = _compute_grn_amount_total(grns, po)
        if abs(invoice_amt - grn_amt) < 0.01:
            lines.append(
                {
                    "kind": "ok",
                    "text": (
                        f"GRN Amount Match : Invoice ₹ {_format_amount(invoice_amt)} "
                        f"= GRN ₹ {_format_amount(grn_amt)}"
                    ),
                }
            )
        else:
            lines.append(
                {
                    "kind": "warn",
                    "text": (
                        f"GRN Amount Difference : Invoice (Agreement + GST) "
                        f"₹ {_format_amount(invoice_amt)} vs GRN ₹ {_format_amount(grn_amt)}"
                    ),
                }
            )

        if abs(po_qty - grn_qty) < 0.0001:
            lines.append({"kind": "ok", "text": "GRN Quantity Match"})
        else:
            lines.append(
                {
                    "kind": "warn",
                    "text": (
                        f"GRN Quantity Difference : PO {_format_qty(po_qty)} "
                        f"vs GRN {_format_qty(grn_qty)}"
                    ),
                }
            )

    if not delivery_notes:
        lines.append({"kind": "warn", "text": "No delivery note found against this PO."})
    elif abs(po_qty - dn_qty) < 0.0001:
        lines.append({"kind": "ok", "text": "Delivery Note Quantity Match"})
    else:
        lines.append(
            {
                "kind": "warn",
                "text": (
                    f"Delivery Note Quantity Difference : PO {_format_qty(po_qty)} "
                    f"vs DN {_format_qty(dn_qty)}"
                ),
            }
        )

    if not invoices:
        lines.append(
            {
                "kind": "warn",
                "text": "No invoice document yet — using PO Agreement + GST as invoice amount.",
            }
        )
    elif abs(po_qty - inv_qty) < 0.0001:
        lines.append({"kind": "ok", "text": "Invoice Quantity Match"})
    else:
        lines.append(
            {
                "kind": "warn",
                "text": (
                    f"Invoice Quantity Difference : PO {_format_qty(po_qty)} "
                    f"vs Invoice {_format_qty(inv_qty)}"
                ),
            }
        )

    lines.append(
        {
            "kind": "ok",
            "text": f"Documents matched to PO {po.get('po_number')}",
        }
    )
    return lines


def _build_variance_rows(
    po: Optional[Dict[str, Any]],
    grns: List[Dict[str, Any]],
    delivery_notes: List[Dict[str, Any]],
    invoices: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    by_item: Dict[str, Dict[str, Any]] = {}

    def ensure(key: str, description: Optional[str] = None) -> Dict[str, Any]:
        normalized = key or "UNKNOWN"
        existing = by_item.get(normalized)
        if existing:
            if description and (
                existing["description"] == "—" or existing["description"] == normalized
            ):
                existing["description"] = description
            return existing
        created = {
            "description": (description or "").strip() or normalized,
            "poQty": 0.0,
            "grnQty": 0.0,
            "dnQty": 0.0,
            "invQty": 0.0,
        }
        by_item[normalized] = created
        return created

    for line in (po.get("lines") if po else None) or []:
        if not isinstance(line, dict):
            continue
        key = _item_key(line.get("item_no") or line.get("item_id")) or f"line-{line.get('line_no')}"
        row = ensure(key, line.get("description"))
        row["poQty"] += _to_number(line.get("qty_ordered")) or 0.0

    for header in grns:
        lines = header.get("lines") if isinstance(header.get("lines"), list) else []
        for line in lines:
            if not isinstance(line, dict):
                continue
            key = (
                _item_key(line.get("itemId") or line.get("item_id"))
                or _item_key(header.get("grnNumber") or header.get("grn_number"))
                or f"grn-{header.get('grnId') or 'x'}"
            )
            row = ensure(key, line.get("description"))
            row["grnQty"] += _to_number(
                line.get("qtyAccepted")
                or line.get("qty_accepted")
                or line.get("qtyReceived")
                or line.get("qty_received")
            ) or 0.0

    for header in delivery_notes:
        lines = header.get("lines") if isinstance(header.get("lines"), list) else []
        for line in lines:
            if not isinstance(line, dict):
                continue
            key = (
                _item_key(line.get("itemId") or line.get("item_id"))
                or _item_key(header.get("invoiceNo") or header.get("invoice_no"))
                or f"dn-{header.get('invoiceId') or 'x'}"
            )
            row = ensure(key, line.get("description"))
            row["dnQty"] += _to_number(line.get("qtyInvoiced") or line.get("qty_invoiced")) or 0.0

    for header in invoices:
        lines = header.get("lines") if isinstance(header.get("lines"), list) else []
        for line in lines:
            if not isinstance(line, dict):
                continue
            key = (
                _item_key(line.get("itemId") or line.get("item_id"))
                or _item_key(header.get("invoiceNo") or header.get("invoice_no"))
                or f"inv-{header.get('invoiceId') or 'x'}"
            )
            row = ensure(key, line.get("description"))
            row["invQty"] += _to_number(line.get("qtyInvoiced") or line.get("qty_invoiced")) or 0.0

    out: List[Dict[str, str]] = []
    for idx, (item, row) in enumerate(by_item.items()):
        variance = float(row["poQty"]) - float(row["invQty"])
        out.append(
            {
                "item": item or "—",
                "description": str(row["description"] or "—"),
                "poQty": _format_qty(row["poQty"]),
                "grnQty": _format_qty(row["grnQty"]),
                "invQty": _format_qty(row["invQty"]),
                "variance": _format_qty(variance),
            }
        )
        if idx >= 19:
            break
    return out


def get_three_way_match_detail(
    db: Session,
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
) -> Dict[str, Any]:
    """Same data the UI Invoice Review detail builds for a PO."""
    normalized = (po_number or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Purchase order number is required.")

    company_id = resolve_company_id(company_id)
    po = _load_po_full(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        po_number=normalized,
    )

    effective_tenant = int(po["tenant_id"]) if po and po.get("tenant_id") is not None else tenant_id
    effective_company = (
        resolve_company_id(int(po["company_id"]))
        if po and po.get("company_id") is not None
        else company_id
    )

    invoices = list_invoices_for_po(
        po_number=normalized,
        tenant_id=effective_tenant,
        company_id=effective_company,
    )
    grns = list_grns_for_po(
        po_number=normalized,
        tenant_id=effective_tenant,
        company_id=effective_company,
    )
    delivery_notes = list_dispatch_notes_for_po(
        po_number=normalized,
        tenant_id=effective_tenant,
        company_id=effective_company,
    )

    primary_invoice = invoices[0] if invoices else None
    invoice_no = (
        _pick_str(primary_invoice, "invoiceNo", "invoice_no") if primary_invoice else None
    )
    if invoices and len(invoices) > 1 and invoice_no:
        invoice_display = f"{invoice_no} (+{len(invoices) - 1})"
    else:
        invoice_display = invoice_no or "—"

    invoice_amt = _sum_qty(
        [inv.get("invoiceAmount") or inv.get("invoice_amount") for inv in invoices]
    )
    supplier = (
        (po.get("vendor_name_snapshot") if po else None)
        or (po.get("vendor_id") if po else None)
        or (_pick_str(primary_invoice, "vendorId", "vendor_id") if primary_invoice else None)
        or "—"
    )

    match_summary = _build_match_summary(po, grns, delivery_notes, invoices)
    variance_rows = _build_variance_rows(po, grns, delivery_notes, invoices)
    warn_count = sum(1 for line in match_summary if line.get("kind") == "warn")
    ok_count = sum(1 for line in match_summary if line.get("kind") == "ok")

    return {
        "status": "ok",
        "poNumber": normalized,
        "poId": str(po["po_id"]) if po and po.get("po_id") is not None else None,
        "invoiceNo": invoice_display,
        "grnCount": len(grns),
        "invoiceCount": len(invoices),
        "deliveryNoteCount": len(delivery_notes),
        "header": {
            "supplier": str(supplier),
            "poNumber": normalized,
            "invoiceNo": invoice_display,
            "invoiceAmount": f"₹ {_format_amount(invoice_amt)}" if invoice_amt else "—",
            "paymentTerms": (po.get("delivery_terms") if po else None) or "Net 30 Days",
            "expectedDelivery": (po.get("expected_delivery_date") if po else None) or "—",
            "poStatus": (po.get("status") if po else None) or "—",
        },
        "matchSummaryTitle": "Match Summary",
        "matchSummaryLines": match_summary,
        "okCount": ok_count,
        "warnCount": warn_count,
        "varianceLineItems": variance_rows,
        "workflow": [
            {
                "title": "PO Created",
                "state": "done" if po else "pending",
                "subtitle": (po.get("created_at") or "")[:10] if po else "Awaiting PO",
            },
            {
                "title": "GRN Received",
                "state": "done" if grns else "pending",
                "subtitle": (
                    f"{len(grns)} GRN{'s' if len(grns) != 1 else ''} linked"
                    if grns
                    else "Awaiting GRN"
                ),
            },
            {
                "title": "Invoice Matched" if invoices else "Accounts Review Pending",
                "state": "done" if invoices else "pending",
                "subtitle": (
                    f"{len(invoices)} invoice{'s' if len(invoices) != 1 else ''} linked"
                    if invoices
                    else "Awaiting Verification"
                ),
            },
        ],
    }
