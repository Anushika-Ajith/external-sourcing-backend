"""Resolve vendor by name and apply vendor_rates onto PO lines during upload."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import Vendor, VendorItem
from services.invoice_item_semantic_match import get_latest_vendor_rate
from services.ocr_item_rate_import import _find_existing_vendor_item
from services.product_matching_common import (
    collect_product_codes,
    evaluate_product_code_embedding_gate,
    product_code_in_candidate_codes,
)


def get_vendor_rate_for_product_code(
    db: Session,
    *,
    vendor_id: int,
    erp_code: str,
    product_name: Optional[str] = None,
) -> Dict[str, Optional[Any]]:
    """
    Resolve latest vendor_rates row for a vendor + stock product code.

    Returns ``{rate, uom, currency, valid_from, valid_to}`` (values may be None).
    ``valid_to`` is the rate-contract expiry date when present.
    """
    code = (erp_code or "").strip()
    name = (product_name or "").strip() or code
    if not code or vendor_id is None:
        return {
            "rate": None,
            "uom": None,
            "currency": None,
            "valid_from": None,
            "valid_to": None,
        }

    item = _find_existing_vendor_item(db, int(vendor_id), name, code, code, None)
    if item is None:
        upper = code.upper()
        item = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == int(vendor_id),
                or_(
                    func.upper(VendorItem.erp_code) == upper,
                    func.upper(VendorItem.company_code) == upper,
                    func.upper(VendorItem.vendor_product_code) == upper,
                ),
            )
            .order_by(VendorItem.id.asc())
            .first()
        )
    if item is None or item.id is None:
        return {
            "rate": None,
            "uom": None,
            "currency": None,
            "valid_from": None,
            "valid_to": None,
        }

    rate_row = get_latest_vendor_rate(
        db,
        vendor_item_id=int(item.id),
        vendor_id=int(vendor_id),
    )
    rate_val: Optional[float] = None
    currency: Optional[str] = None
    valid_from = None
    valid_to = None
    if rate_row is not None:
        if rate_row.rate is not None:
            try:
                rate_val = float(rate_row.rate)
            except (TypeError, ValueError):
                rate_val = None
        currency = (rate_row.currency or None) if getattr(rate_row, "currency", None) else None
        valid_from = getattr(rate_row, "valid_from", None)
        valid_to = getattr(rate_row, "valid_to", None)

    uom = (
        (item.uom or item.uom_p or item.purchase_unit or None)
        if item is not None
        else None
    )
    if isinstance(uom, str):
        uom = uom.strip() or None

    return {
        "rate": rate_val,
        "uom": uom,
        "currency": currency,
        "valid_from": valid_from,
        "valid_to": valid_to,
    }


def _normalize_name(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"\s+", " ", str(value)).strip().lower()
    # Drop common legal suffixes for looser matching.
    cleaned = re.sub(
        r"\b(pvt\.?|private|ltd\.?|limited|llc|inc\.?|corp\.?|co\.?)\b\.?",
        "",
        cleaned,
    )
    return re.sub(r"\s+", " ", cleaned).strip(" ,.-")


def resolve_vendor_by_name(
    db: Session,
    vendor_name: Optional[str],
) -> Optional[Vendor]:
    """Identify a vendor from OCR/extracted company name."""
    raw = (vendor_name or "").strip()
    if not raw:
        return None

    exact = (
        db.query(Vendor)
        .filter(func.lower(Vendor.company_name) == raw.lower())
        .order_by(Vendor.vendor_id.asc())
        .first()
    )
    if exact:
        print(
            "[PO VENDOR MATCH] Exact vendor name match:",
            {"vendor_id": exact.vendor_id, "company_name": exact.company_name},
        )
        return exact

    normalized = _normalize_name(raw)
    if not normalized:
        return None

    candidates = (
        db.query(Vendor)
        .filter(Vendor.company_name.isnot(None))
        .order_by(Vendor.vendor_id.asc())
        .all()
    )
    normalized_hits = [
        vendor
        for vendor in candidates
        if _normalize_name(vendor.company_name) == normalized
    ]
    if len(normalized_hits) == 1:
        vendor = normalized_hits[0]
        print(
            "[PO VENDOR MATCH] Normalized vendor name match:",
            {
                "queried_name": raw,
                "vendor_id": vendor.vendor_id,
                "company_name": vendor.company_name,
            },
        )
        return vendor

    contains_hits = [
        vendor
        for vendor in candidates
        if normalized in _normalize_name(vendor.company_name)
        or _normalize_name(vendor.company_name) in normalized
    ]
    if len(contains_hits) == 1:
        vendor = contains_hits[0]
        print(
            "[PO VENDOR MATCH] Partial vendor name match:",
            {
                "queried_name": raw,
                "vendor_id": vendor.vendor_id,
                "company_name": vendor.company_name,
            },
        )
        return vendor

    print(
        "[PO VENDOR MATCH] No unique vendor for name:",
        {
            "queried_name": raw,
            "normalized": normalized,
            "normalized_hits": len(normalized_hits),
            "contains_hits": len(contains_hits),
        },
    )
    return None


def _find_vendor_item_for_po_line(
    db: Session,
    *,
    vendor_id: int,
    line: Dict[str, Any],
) -> Optional[VendorItem]:
    item_id_raw = line.get("item_id")
    if item_id_raw is not None:
        try:
            item_id_int = int(item_id_raw)
            hit = (
                db.query(VendorItem)
                .filter(
                    VendorItem.vendor_id == vendor_id,
                    (VendorItem.id == item_id_int)
                    | (VendorItem.item_id == item_id_int),
                )
                .order_by(VendorItem.id.asc())
                .first()
            )
            if hit:
                return hit
        except (TypeError, ValueError):
            pass

    item_no = str(line.get("item_no") or "").strip() or None
    description = str(line.get("description") or "").strip() or None

    # Prefer reusable OCR item lookup by code / name.
    if description or item_no:
        hit = _find_existing_vendor_item(
            db,
            vendor_id,
            description or item_no or "",
            item_no,
            item_no,
            None,
        )
        if hit:
            return hit

    if item_no:
        hit = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == vendor_id,
                func.lower(VendorItem.item_name) == item_no.lower(),
            )
            .first()
        )
        if hit:
            return hit

    if description:
        # Loose contains match when exact name fails.
        hits = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == vendor_id,
                func.lower(VendorItem.item_name).like(f"%{description.lower()}%"),
            )
            .limit(2)
            .all()
        )
        if len(hits) == 1:
            return hits[0]

    return None


def _reject_vendor_item_when_code_embedding_mismatch(
    *,
    vendor_item: Optional[VendorItem],
    line: Dict[str, Any],
) -> Optional[VendorItem]:
    """Drop code-only vendor item links when OCR name embedding disagrees."""
    if vendor_item is None:
        return None
    item_no = str(line.get("item_no") or "").strip() or None
    description = str(line.get("description") or "").strip() or None
    if not item_no or not description:
        return vendor_item

    candidate_codes = collect_product_codes(
        vendor_item.erp_code,
        vendor_item.vendor_product_code,
        vendor_item.barcode,
        vendor_item.company_code,
        item_no,
    )
    if not product_code_in_candidate_codes(item_no, candidate_codes):
        return vendor_item

    catalog_name = (
        (vendor_item.product_name or "").strip()
        or (vendor_item.item_name or "").strip()
        or description
    )
    _, ok, _, stop_reason = evaluate_product_code_embedding_gate(
        product_name=description,
        product_code=item_no,
        candidate_name=catalog_name,
        candidate_codes=candidate_codes,
        log_context="po upload",
    )
    if ok:
        return vendor_item

    print(
        "[PO VENDOR MATCH] Reject code link — embedding mismatch:",
        {
            "item_no": item_no,
            "description": description,
            "vendor_item_id": vendor_item.id,
            "catalog_name": catalog_name,
            "reason": stop_reason,
        },
    )
    return None


def _to_decimal_rate(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def resolve_po_line_rate_fields(
    db: Session,
    *,
    vendor_id: int,
    line: Dict[str, Any],
    vendor_item: Optional[VendorItem] = None,
) -> Dict[str, Optional[Decimal]]:
    """
    Resolve po_item_rate, rate_contract_item_rate, and unit_price for one PO line.

    unit_price prefers rate_contract_item_rate from vendor_rates, else po_item_rate.
    """
    po_item_rate = _to_decimal_rate(
        line.get("po_item_rate")
        if line.get("po_item_rate") is not None
        else line.get("unit_price")
    )
    rate_contract_item_rate = _to_decimal_rate(line.get("rate_contract_item_rate"))

    if vendor_item is None:
        vendor_item = _find_vendor_item_for_po_line(
            db, vendor_id=vendor_id, line=line
        )

    if vendor_item is not None and rate_contract_item_rate is None:
        rate_row = get_latest_vendor_rate(
            db,
            vendor_item_id=int(vendor_item.id),
            vendor_id=int(vendor_id),
        )
        if rate_row is not None and rate_row.rate is not None:
            rate_contract_item_rate = _to_decimal_rate(rate_row.rate)

    if rate_contract_item_rate is not None:
        unit_price = rate_contract_item_rate
    else:
        unit_price = po_item_rate

    return {
        "po_item_rate": po_item_rate,
        "rate_contract_item_rate": rate_contract_item_rate,
        "unit_price": unit_price,
    }


def enrich_po_lines_with_vendor_rates(
    db: Session,
    *,
    vendor_id: int,
    lines: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    For each PO line set:
    - po_item_rate from uploaded PO / OCR rate
    - rate_contract_item_rate from vendor_rates (when vendor item is matched)
    - unit_price = rate_contract_item_rate if present else po_item_rate
    """
    enriched: List[Dict[str, Any]] = []
    matches: List[Dict[str, Any]] = []
    rates_applied = 0

    for line in lines:
        next_line = dict(line)
        vendor_item = _find_vendor_item_for_po_line(
            db, vendor_id=vendor_id, line=next_line
        )
        vendor_item = _reject_vendor_item_when_code_embedding_mismatch(
            vendor_item=vendor_item,
            line=next_line,
        )
        rate_fields = resolve_po_line_rate_fields(
            db,
            vendor_id=vendor_id,
            line=next_line,
            vendor_item=vendor_item,
        )
        po_item_rate = rate_fields["po_item_rate"]
        contract_rate = rate_fields["rate_contract_item_rate"]
        next_line["po_item_rate"] = po_item_rate
        next_line["rate_contract_item_rate"] = contract_rate
        next_line["unit_price"] = rate_fields["unit_price"]

        match_info: Dict[str, Any] = {
            "line_no": next_line.get("line_no"),
            "item_no": next_line.get("item_no"),
            "description": next_line.get("description"),
            "po_item_rate": str(po_item_rate) if po_item_rate is not None else None,
            "vendor_item_id": None,
            "buyer_item_id": None,
            "rate_contract_item_rate": (
                str(contract_rate) if contract_rate is not None else None
            ),
            "rate_applied": contract_rate is not None,
        }

        if vendor_item is not None:
            match_info["vendor_item_id"] = vendor_item.id
            match_info["buyer_item_id"] = vendor_item.item_id
            if vendor_item.item_id is not None:
                next_line["item_id"] = vendor_item.item_id
            if not next_line.get("item_no"):
                next_line["item_no"] = (
                    vendor_item.erp_code
                    or vendor_item.vendor_product_code
                    or next_line.get("item_no")
                )
            if not next_line.get("uom") and vendor_item.uom:
                next_line["uom"] = vendor_item.uom
            if contract_rate is not None:
                rates_applied += 1

        matches.append(match_info)
        enriched.append(next_line)

    summary = {
        "vendor_id": vendor_id,
        "line_count": len(lines),
        "rates_applied": rates_applied,
        "matches": matches,
    }
    print("[PO VENDOR RATES] Line rate enrichment:", summary)
    return enriched, summary


def resolve_vendor_and_enrich_po_business(
    db: Session,
    business: Dict[str, Any],
    *,
    fallback_vendor_id: int,
) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
    """
    Vendor-portal PO ownership stays with the uploading vendor (fallback_vendor_id).

    OCR vendor name is kept on the business snapshot for reference, but does not
    reassign vendor_id — otherwise the PO disappears from that vendor's list.

    Rates are looked up against the uploading vendor's vendor_items / vendor_rates.
    """
    vendor_id = int(fallback_vendor_id)
    ocr_vendor_name = (business.get("vendor_name") or "").strip() or None
    name_match = resolve_vendor_by_name(db, ocr_vendor_name)

    # Prefer the logged-in vendor's company name for the snapshot when available.
    owner = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if owner and (owner.company_name or "").strip():
        business["vendor_name"] = owner.company_name
    elif ocr_vendor_name:
        business["vendor_name"] = ocr_vendor_name

    lines = business.get("lines") if isinstance(business.get("lines"), list) else []
    enriched_lines, rate_summary = enrich_po_lines_with_vendor_rates(
        db,
        vendor_id=vendor_id,
        lines=lines,
    )
    business = {**business, "lines": enriched_lines}

    meta = {
        "vendor_id": vendor_id,
        "vendor_source": "uploading_vendor",
        "vendor_name": business.get("vendor_name"),
        "ocr_vendor_name": ocr_vendor_name,
        "ocr_name_match_vendor_id": (
            int(name_match.vendor_id) if name_match is not None else None
        ),
        "fallback_vendor_id": fallback_vendor_id,
        "rate_summary": rate_summary,
    }
    print("[PO UPLOAD] Vendor resolved + rates applied:", meta)
    return vendor_id, business, meta
