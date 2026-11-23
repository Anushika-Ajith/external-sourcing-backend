from __future__ import annotations

from datetime import date, datetime
import json
from typing import Any, Dict, List, Optional

import requests

from config import settings
from services.whatsapp_notify_service import notify_grn_created_whatsapp


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _line_payload(
    line: Dict[str, Any],
    *,
    idx: int,
    header: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    line_no = _int_or_none(line.get("line_no")) or idx
    po_line_no = _int_or_none(line.get("po_line_no")) or line_no

    payload: Dict[str, Any] = {
        "lineNo": line_no,
        "poLineNo": po_line_no,
    }

    mappings = {
        "itemId": ("item_id", _int_or_none),
        "description": ("description", str),
        "uom": ("uom", str),
        "qtyReceived": ("qty_received", _float_or_none),
        "qtyAccepted": ("qty_accepted", _float_or_none),
        "qtyRejected": ("qty_rejected", _float_or_none),
        "warehouseId": ("warehouse_id", _int_or_none),
        "binLocation": ("bin_location", str),
    }
    for out_key, (in_key, caster) in mappings.items():
        raw = line.get(in_key)
        if not _present(raw):
            continue
        val = caster(raw)
        if val is not None:
            payload[out_key] = val

    # These are useful for traceability if the GRN API accepts them; omit if absent.
    inherited = {
        "poNumber": line.get("po_number") or header.get("po_number"),
        "deliveryNoteNo": line.get("delivery_note_no") or header.get("delivery_note_no"),
        "deliveryLineNo": line.get("delivery_line_no"),
        "poId": line.get("po_id") or header.get("po_id"),
    }
    for out_key, raw in inherited.items():
        if not _present(raw):
            continue
        if out_key in {"deliveryLineNo", "poId"}:
            val = _int_or_none(raw)
        else:
            val = str(raw)
        if val is not None:
            payload[out_key] = val

    return payload


def apply_po_context_to_grn_raw_text(
    raw_text: Any,
    *,
    po_number: Optional[str] = None,
    po_id: Optional[int] = None,
) -> Any:
    """Merge PO context from the upload request into OCR GRN extraction."""
    if not isinstance(raw_text, dict):
        return raw_text
    if not _present(po_number) and po_id is None:
        return raw_text

    grn = raw_text.get("grn")
    if not isinstance(grn, dict):
        grn = {}
        raw_text["grn"] = grn
    header = grn.get("header")
    if not isinstance(header, dict):
        header = {}
        grn["header"] = header

    if _present(po_number):
        header["po_number"] = str(po_number).strip()
    if po_id is not None:
        header["po_id"] = int(po_id)

    return raw_text


def build_grn_with_lines_payload(
    raw_text: Dict[str, Any],
    *,
    tenant_id: int,
    company_id: int,
    fallback_vendor_id: int,
    created_by: int,
    request_po_number: Optional[str] = None,
    request_po_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    raw_text = apply_po_context_to_grn_raw_text(
        raw_text,
        po_number=request_po_number,
        po_id=request_po_id,
    )
    grn = raw_text.get("grn") if isinstance(raw_text, dict) else None
    if not isinstance(grn, dict):
        return None

    header = grn.get("header")
    if not isinstance(header, dict):
        return None

    grn_number = header.get("grn_number")
    po_number = header.get("po_number")
    grn_date = header.get("grn_date")
    if not (_present(grn_number) and _present(po_number) and _present(grn_date)):
        return None

    vendor_id = _int_or_none(header.get("vendor_id")) or int(fallback_vendor_id)
    payload: Dict[str, Any] = {
        "tenantId": int(tenant_id),
        "companyId": int(company_id),
        "documentType": str(header.get("document_type") or "GRN"),
        "grnNumber": str(grn_number),
        "versionNo": _int_or_none(header.get("version_no")) or 1,
        "vendorId": vendor_id,
        "poNumber": str(po_number),
        "grnDate": _jsonable(grn_date),
        "departmentId": 3,
        "status": "DRAFT",
        "createdBy": int(created_by),
        "lines": [],
    }

    optional_header = {
        "poId": ("po_id", _int_or_none),
        "deliveryNoteNo": ("delivery_note_no", str),
        "receivedBy": ("received_by", _int_or_none),
        "inspectionStatus": ("inspection_status", str),
    }
    for out_key, (in_key, caster) in optional_header.items():
        raw = header.get(in_key)
        if not _present(raw):
            continue
        val = caster(raw)
        if val is not None:
            payload[out_key] = val

    lines = grn.get("lines")
    if isinstance(lines, list):
        for idx, line in enumerate(lines, start=1):
            if not isinstance(line, dict):
                continue
            mapped = _line_payload(line, idx=idx, header=header)
            if mapped is not None:
                payload["lines"].append(mapped)

    if not payload["lines"]:
        return None

    return _jsonable(payload)


def post_grn_with_lines(payload: Dict[str, Any]) -> Dict[str, Any]:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("INTERNAL_API_URL is not configured")
    url = f"{base}/grn-header/with-lines"
    print(f"[GRN OCR INSERT] API: POST {url}")
    print(
        "[GRN OCR INSERT] Payload:",
        json.dumps(payload, default=str, indent=2),
    )
    try:
        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"[GRN OCR INSERT] Request failed: {exc}")
        return {
            "status": "failed",
            "url": url,
            "error": str(exc),
            "payload": payload,
        }

    body: Any
    try:
        body = response.json()
    except ValueError:
        body = response.text
    print(f"[GRN OCR INSERT] Response status: {response.status_code}")
    print(f"[GRN OCR INSERT] Response body: {body}")
    if not response.ok:
        return {
            "status": "failed",
            "url": url,
            "status_code": response.status_code,
            "response": body,
            "payload": payload,
        }
    return {
        "status": "inserted",
        "url": url,
        "status_code": response.status_code,
        "response": body,
        "payload": payload,
    }


def insert_ocr_grn(
    raw_text: Dict[str, Any],
    *,
    tenant_id: int,
    company_id: int,
    fallback_vendor_id: int,
    created_by: int,
    request_po_number: Optional[str] = None,
    request_po_id: Optional[int] = None,
) -> Dict[str, Any]:
    payload = build_grn_with_lines_payload(
        raw_text,
        tenant_id=tenant_id,
        company_id=company_id,
        fallback_vendor_id=fallback_vendor_id,
        created_by=created_by,
        request_po_number=request_po_number,
        request_po_id=request_po_id,
    )
    if payload is None:
        grn = raw_text.get("grn") if isinstance(raw_text, dict) else None
        print("[GRN OCR INSERT] Skipped: incomplete GRN payload")
        print(
            "[GRN OCR INSERT] Extracted grn:",
            json.dumps(grn, default=str, indent=2),
        )
        return {
            "status": "skipped",
            "reason": (
                "No complete GRN payload extracted. Required: grn.header.grn_number, "
                "grn.header.po_number, grn.header.grn_date, and at least one line."
            ),
        }
    result = post_grn_with_lines(payload)
    if result.get("status") == "inserted":
        try:
            line_count = len(payload.get("lines") or [])
            notify_grn_created_whatsapp(
                grn_number=str(payload.get("grnNumber") or ""),
                po_number=str(payload.get("poNumber") or request_po_number or ""),
                po_id=request_po_id or payload.get("poId"),
                line_count=line_count,
            )
        except Exception as notify_exc:
            print(f"[GRN-WHATSAPP] unexpected notify error (ignored): {notify_exc}")
    return result
