"""
When a vendor uploads a rate contract (OCR), clone RFQs only where that vendor is
**FREEZED** on ``rfq_vendor`` (``status == 'FREEZED'``) for some version of the RFQ.

**Never UPDATE** existing ``rfq_header`` / ``rfq_line`` / ``rfq_vendor`` rows (frozen
revision stays unchanged). **Only INSERT**: one new header row, new line rows for that
version, and new vendor rows — the new header uses the next ``version_no``
(``frozen_version_no + 1`` when that key is free; otherwise next available).

Uses the **latest version_no among FREEZED rows** per RFQ as the clone source.

Matching (first hit wins per RFQ line):
- RFQLine.item_id equals integer OCR erp_code when both parse; or
- Normalized OCR item_name / description overlaps RFQLine.item_description (substring / equality).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from models import RFQHeader, RFQLine, RFQVendor

logger = logging.getLogger(__name__)


def _clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _normalize_key(s: Optional[str]) -> str:
    if not s:
        return ""
    return " ".join(s.lower().split())


def _ocr_decimal_rate(row: Dict[str, Any]) -> Optional[Decimal]:
    r = row.get("rate")
    if r is None or r == "":
        return None
    try:
        return Decimal(str(r).replace(",", ""))
    except Exception:
        return None


def line_matches_ocr_item(line: RFQLine, ocr_row: Dict[str, Any]) -> bool:
    erp = _clean_str(ocr_row.get("erp_code"))
    if erp and line.item_id is not None:
        try:
            if int(erp) == int(line.item_id):
                return True
        except (ValueError, TypeError):
            pass

    name = _normalize_key(_clean_str(ocr_row.get("item_name")))
    desc_ocr = _normalize_key(_clean_str(ocr_row.get("description")))
    line_desc = _normalize_key(line.item_description or "")

    if len(name) >= 3 and name == line_desc:
        return True
    if len(name) >= 5 and (name in line_desc or line_desc in name):
        return True
    if len(desc_ocr) >= 5 and (desc_ocr in line_desc or line_desc in desc_ocr):
        return True
    return False


def _build_line_rate_updates(
    lines: List[RFQLine], ocr_items: List[Any]
) -> Dict[int, Decimal]:
    """Map RFQ line_no -> contract unit rate from first matching OCR row with a rate."""
    updates: Dict[int, Decimal] = {}
    for line in lines:
        for ocr in ocr_items:
            if not isinstance(ocr, dict):
                continue
            if not line_matches_ocr_item(line, ocr):
                continue
            rate = _ocr_decimal_rate(ocr)
            if rate is None:
                continue
            updates[line.line_no] = rate
            break
    return updates


def _truncate_remarks(base: Optional[str], extra: str, max_len: int = 500) -> str:
    suffix = extra.strip()
    if not base:
        return suffix[:max_len]
    combined = f"{base.rstrip()} | {suffix}"
    return combined[:max_len]


def clone_rfq_next_version(
    db: Session,
    src: RFQHeader,
    line_new_rates: Dict[int, Decimal],
) -> RFQHeader:
    """
    INSERT-only: does not modify ``src`` or any existing RFQ rows.

    Adds a **new** ``rfq_header`` whose ``version_no`` is ``src.version_no + 1`` when
    that composite key is unused; otherwise ``max(version_no) + 1`` for this RFQ so the
    insert cannot collide. New ``rfq_line`` / ``rfq_vendor`` rows reference that version.
    Lines in ``line_new_rates`` get updated estimated_rate / estimated_amount.
    """
    same_rfq = (
        RFQHeader.tenant_id == src.tenant_id,
        RFQHeader.company_id == src.company_id,
        RFQHeader.document_type == src.document_type,
        RFQHeader.rfq_no == src.rfq_no,
    )
    candidate_ver = src.version_no + 1
    clash = (
        db.query(RFQHeader.version_no)
        .filter(*same_rfq, RFQHeader.version_no == candidate_ver)
        .first()
    )
    if clash is not None:
        global_max_v = db.query(func.max(RFQHeader.version_no)).filter(*same_rfq).scalar()
        new_ver = int(global_max_v if global_max_v is not None else src.version_no) + 1
    else:
        new_ver = candidate_ver
    next_rfq_id = (db.query(func.max(RFQHeader.rfq_id)).scalar() or 0) + 1
    now = datetime.now(timezone.utc)

    hce = getattr(src, "current_editor", None)
    if hce is None or (isinstance(hce, str) and not hce.strip()):
        hce = "BUYER"
    elif isinstance(hce, str):
        hce = hce.strip()

    new_header = RFQHeader(
        tenant_id=src.tenant_id,
        company_id=src.company_id,
        document_type=src.document_type,
        rfq_no=src.rfq_no,
        version_no=new_ver,
        rfq_id=next_rfq_id,
        rfq_date=src.rfq_date,
        department_id=src.department_id,
        requester_id=src.requester_id,
        status=src.status,
        priority=src.priority,
        source_type=src.source_type,
        remarks=_truncate_remarks(
            src.remarks,
            f"Revision v{new_ver}: rate contract matched RFQ lines {sorted(line_new_rates.keys())}.",
        ),
        approved_by=src.approved_by,
        approved_at=src.approved_at,
        rejected_by=src.rejected_by,
        rejected_at=src.rejected_at,
        rejection_reason=src.rejection_reason,
        created_by=src.created_by,
        updated_by=src.updated_by,
        document_number=src.document_number,
        current_editor=hce,
        updated_at=now,
    )
    db.add(new_header)
    db.flush()

    lines_sorted = sorted(src.lines, key=lambda ln: ln.line_no)
    for ln in lines_sorted:
        new_rate = line_new_rates.get(ln.line_no, ln.estimated_rate)
        new_amt = ln.estimated_amount
        if new_rate is not None and ln.quantity is not None:
            try:
                new_amt = Decimal(str(ln.quantity)) * Decimal(str(new_rate))
            except Exception:
                pass

        nl = RFQLine(
            tenant_id=src.tenant_id,
            company_id=src.company_id,
            document_type=src.document_type,
            rfq_no=src.rfq_no,
            version_no=new_ver,
            line_no=ln.line_no,
            item_id=ln.item_id,
            item_description=ln.item_description,
            uom=ln.uom,
            quantity=ln.quantity,
            estimated_rate=new_rate,
            estimated_amount=new_amt,
            required_date=ln.required_date,
            remarks=ln.remarks,
            created_by=ln.created_by,
            updated_by=ln.updated_by,
            updated_at=now,
        )
        db.add(nl)

    vendors_sorted = sorted(src.vendors, key=lambda v: v.vendor_id)
    for v in vendors_sorted:
        ce = v.current_editor
        if ce is None or (isinstance(ce, str) and not ce.strip()):
            ce = "BUYER"
        elif isinstance(ce, str):
            ce = ce.strip()
        nv = RFQVendor(
            tenant_id=src.tenant_id,
            company_id=src.company_id,
            document_type=src.document_type,
            rfq_no=src.rfq_no,
            version_no=new_ver,
            vendor_id=v.vendor_id,
            status=v.status,
            current_editor=ce,
            created_by=v.created_by,
            updated_by=v.updated_by,
            updated_at=now,
        )
        db.add(nv)

    return new_header


def apply_rate_contract_rfq_revisions(
    db: Session,
    vendor_id: int,
    ocr_payload: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    For each RFQ where ``vendor_id`` has ``rfq_vendor.status == 'FREEZED'``, consider
    only the **latest FREEZED** ``version_no`` per RFQ; if any line matches a priced
    OCR item, **INSERT** (never UPDATE) a new RFQ header version plus new lines/vendors.

    Returns metadata list for API responses (empty if nothing matched).
    """
    ocr_items = ocr_payload.get("items") or []
    if not isinstance(ocr_items, list) or not ocr_items:
        return []

    rv_sub = (
        db.query(
            RFQVendor.tenant_id,
            RFQVendor.company_id,
            RFQVendor.document_type,
            RFQVendor.rfq_no,
            func.max(RFQVendor.version_no).label("mv"),
        )
        .filter(
            RFQVendor.vendor_id == vendor_id,
            RFQVendor.status == "FREEZED",
        )
        .group_by(
            RFQVendor.tenant_id,
            RFQVendor.company_id,
            RFQVendor.document_type,
            RFQVendor.rfq_no,
        )
    ).subquery()

    headers = (
        db.query(RFQHeader)
        .join(
            rv_sub,
            (RFQHeader.tenant_id == rv_sub.c.tenant_id)
            & (RFQHeader.company_id == rv_sub.c.company_id)
            & (RFQHeader.document_type == rv_sub.c.document_type)
            & (RFQHeader.rfq_no == rv_sub.c.rfq_no)
            & (RFQHeader.version_no == rv_sub.c.mv),
        )
        .options(
            joinedload(RFQHeader.lines),
            joinedload(RFQHeader.vendors),
        )
        .all()
    )

    results: List[Dict[str, Any]] = []
    for h in headers:
        updates = _build_line_rate_updates(list(h.lines), ocr_items)
        if not updates:
            continue
        try:
            new_header = clone_rfq_next_version(db, h, updates)
            results.append(
                {
                    "tenant_id": h.tenant_id,
                    "company_id": h.company_id,
                    "document_type": h.document_type,
                    "rfq_no": h.rfq_no,
                    "previous_version": h.version_no,
                    "new_version": new_header.version_no,
                    "lines_updated": sorted(updates.keys()),
                }
            )
        except Exception:
            logger.exception(
                "RFQ revision clone failed rfq_no=%s version=%s",
                h.rfq_no,
                h.version_no,
            )
            raise

    return results
