"""Purchase-order OCR extraction."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from services.document_ocr.constants import PO_OCR_UPLOAD_DIR
from services.document_ocr.pipeline import run_ocr_pipeline


def _pick_str(*values: Any) -> Optional[str]:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


from services.po_number_extract import extract_po_number_from_ocr_raw
def _map_items_to_lines(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        lines.append(
            {
                "line_no": idx,
                "item_no": _pick_str(
                    item.get("erp_code"),
                    item.get("vendor_product_code"),
                    item.get("item_id"),
                ),
                "description": _pick_str(item.get("item_name"), item.get("description")),
                "uom": _pick_str(item.get("uom")),
                "qty_ordered": item.get("qty_ordered")
                or item.get("quantity")
                or item.get("quantity_balance"),
                "unit_price": item.get("unit_price") or item.get("rate"),
                "po_item_rate": item.get("unit_price") or item.get("rate"),
                "delivery_date": _parse_date(item.get("delivery_date") or item.get("valid_from")),
            }
        )
    return lines


def extract_po_business_from_ocr_raw(
    raw: Dict[str, Any],
    *,
    fallback_vendor_name: Optional[str] = None,
    ocr_text: Optional[str] = None,
    document_classification: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Buyer/printed PO number from the PDF (reference only). System po_number
    # is assigned from Nest next-numbering on insert.
    source_po_number = extract_po_number_from_ocr_raw(
        raw,
        ocr_text=ocr_text,
        document_classification=document_classification,
    )

    vendor_block = raw.get("vendor") if isinstance(raw.get("vendor"), dict) else {}
    vendor_name = _pick_str(
        vendor_block.get("company_name"),
        vendor_block.get("contact_person"),
        fallback_vendor_name,
    ) or ""

    items = raw.get("items")
    lines = _map_items_to_lines(items) if isinstance(items, list) else []

    po_date = _parse_date(vendor_block.get("po_date")) or date.today()

    return {
        "po_number": None,
        "source_po_number": source_po_number,
        "vendor_name": vendor_name,
        "po_date": po_date,
        "lines": lines,
        "currency_code": _pick_str(raw.get("currency_code")) or "INR",
        "remarks": _pick_str(raw.get("contract_terms"), vendor_block.get("remarks")),
    }


def run_po_ocr_on_files(saved_paths: List[str]) -> tuple[Dict[str, Any], str, Dict[str, Any]]:
    from services.document_ocr.pipeline import run_ocr_pipeline

    combined_text, document_classification, raw = run_ocr_pipeline(saved_paths)
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="OCR did not return structured PO data.",
        )
    classification = (
        document_classification if isinstance(document_classification, dict) else {}
    )
    return raw, combined_text or "", classification


def ocr_po_from_bytes(
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> tuple[Dict[str, Any], str, Dict[str, Any]]:
    job_id = str(uuid.uuid4())
    job_dir = PO_OCR_UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / (filename or "upload.pdf")
    dest.write_bytes(file_bytes)

    try:
        return run_po_ocr_on_files([str(dest)])
    finally:
        if dest.exists():
            dest.unlink(missing_ok=True)
        if job_dir.exists():
            try:
                job_dir.rmdir()
            except OSError:
                pass
