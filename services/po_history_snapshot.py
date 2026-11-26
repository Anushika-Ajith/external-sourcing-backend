"""Snapshot current PO header/lines into history tables."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from models import (
    PurchaseOrderHeader,
    PurchaseOrderHeaderHistory,
    PurchaseOrderLine,
    PurchaseOrderLineHistory,
)


def _header_to_history_dict(header: PurchaseOrderHeader) -> dict:
    return {
        "tenant_id": header.tenant_id,
        "company_id": header.company_id,
        "document_type": header.document_type,
        "po_number": header.po_number,
        "version_no": header.version_no,
        "po_date": header.po_date,
        "vendor_id": header.vendor_id,
        "vendor_site_id": header.vendor_site_id,
        "vendor_name_snapshot": header.vendor_name_snapshot,
        "currency_code": header.currency_code,
        "exchange_rate": header.exchange_rate,
        "incoterms": header.incoterms,
        "delivery_terms": header.delivery_terms,
        "billing_address_id": header.billing_address_id,
        "shipping_address_id": header.shipping_address_id,
        "expected_delivery_date": header.expected_delivery_date,
        "status": header.status,
        "approval_status": header.approval_status,
        "approved_by": header.approved_by,
        "approved_at": header.approved_at,
        "rejected_by": header.rejected_by,
        "rejected_at": header.rejected_at,
        "rejection_reason": header.rejection_reason,
        "po_total_amount": header.po_total_amount,
        "tax_total_amount": header.tax_total_amount,
        "discount_amount": header.discount_amount,
        "net_total_amount": header.net_total_amount,
        "source_type": header.source_type,
        "source_requisition_no": header.source_requisition_no,
        "vendor_acknowledged": header.vendor_acknowledged,
        "acknowledged_at": header.acknowledged_at,
        "dispatch_status": header.dispatch_status,
        "dispatched_at": header.dispatched_at,
        "is_payment_terms_overridden": header.is_payment_terms_overridden,
        "remarks": header.remarks,
        "attachment_count": header.attachment_count,
        "created_by": header.created_by,
        "updated_by": header.updated_by,
        "created_at": header.created_at,
        "updated_at": header.updated_at,
        "document_number": header.document_number,
        "po_id": header.po_id,
        "document_hash": getattr(header, "document_hash", None),
        "business_hash": getattr(header, "business_hash", None),
        "revision_no": getattr(header, "revision_no", None) or 1,
        "current_revision": getattr(header, "current_revision", True),
    }


def _line_to_history_dict(line: PurchaseOrderLine, revision_no: int) -> dict:
    return {
        "line_id": line.line_id,
        "tenant_id": line.tenant_id,
        "company_id": line.company_id,
        "document_type": line.document_type,
        "po_number": line.po_number,
        "version_no": line.version_no,
        "line_no": line.line_no,
        "po_id": line.po_id,
        "item_id": line.item_id,
        "item_no": line.item_no,
        "description": line.description,
        "uom": line.uom,
        "rfq_qty": line.rfq_qty,
        "qty_ordered": line.qty_ordered,
        "qty_received": line.qty_received,
        "qty_invoiced": line.qty_invoiced,
        "unit_price": line.unit_price,
        "po_item_rate": getattr(line, "po_item_rate", None),
        "rate_contract_item_rate": getattr(line, "rate_contract_item_rate", None),
        "tax_percent": line.tax_percent,
        "tax_amount": line.tax_amount,
        "discount_percent": line.discount_percent,
        "discount_amount": line.discount_amount,
        "total_amount": line.total_amount,
        "delivery_date": line.delivery_date,
        "lead_time_days": line.lead_time_days,
        "warehouse_id": line.warehouse_id,
        "pr_line_id": line.pr_line_id,
        "pr_requisition_no": line.pr_requisition_no,
        "pr_version_no": line.pr_version_no,
        "pr_line_no": line.pr_line_no,
        "line_status": line.line_status,
        "remarks": line.remarks,
        "attachment_count": line.attachment_count,
        "created_by": line.created_by,
        "updated_by": line.updated_by,
        "created_at": line.created_at,
        "updated_at": line.updated_at,
        "revision_no": revision_no,
    }


def snapshot_po_to_history(
    db: Session,
    header: PurchaseOrderHeader,
    lines: Iterable[PurchaseOrderLine],
    *,
    history_action: str,
    history_reason: Optional[str],
    history_created_by: int,
) -> None:
    revision_no = getattr(header, "revision_no", None) or 1
    hist_header = PurchaseOrderHeaderHistory(
        **_header_to_history_dict(header),
        history_action=history_action,
        history_reason=history_reason,
        history_created_by=history_created_by,
        history_created_at=datetime.now(timezone.utc),
    )
    db.add(hist_header)

    for line in lines:
        db.add(
            PurchaseOrderLineHistory(
                **_line_to_history_dict(line, revision_no),
                history_action=history_action,
                history_reason=history_reason,
                history_created_by=history_created_by,
                history_created_at=datetime.now(timezone.utc),
            )
        )
