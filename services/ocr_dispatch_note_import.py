from __future__ import annotations

from datetime import date, datetime
import json
from typing import Any, Dict, Optional

import requests

from config import settings


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


def apply_po_context_to_dispatch_note_raw_text(
    raw_text: Any,
    *,
    po_number: Optional[str] = None,
    po_id: Optional[int] = None,
) -> Any:
    if not isinstance(raw_text, dict):
        return raw_text
    if not _present(po_number) and po_id is None:
        return raw_text

    dispatch_note = raw_text.get("dispatch_note")
    if not isinstance(dispatch_note, dict):
        dispatch_note = {}
        raw_text["dispatch_note"] = dispatch_note
    header = dispatch_note.get("header")
    if not isinstance(header, dict):
        header = {}
        dispatch_note["header"] = header

    if _present(po_number):
        header["po_number"] = str(po_number).strip()
    if po_id is not None:
        header["po_id"] = int(po_id)

    return raw_text


def _line_payload(
    line: Dict[str, Any],
    *,
    idx: int,
    header: Dict[str, Any],
) -> Dict[str, Any]:
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
        # The 3003 API reuses invoice fields for dispatch quantity.
        "qtyInvoiced": ("qty_dispatched", _float_or_none),
        "unitPrice": ("unit_price", _float_or_none),
        "totalAmount": ("total_amount", _float_or_none),
    }
    for out_key, (in_key, caster) in mappings.items():
        raw = line.get(in_key)
        if not _present(raw):
            continue
        val = caster(raw)
        if val is not None:
            payload[out_key] = val

    if "totalAmount" not in payload:
        qty = payload.get("qtyInvoiced")
        unit_price = payload.get("unitPrice")
        if qty is not None and unit_price is not None:
            payload["totalAmount"] = float(qty) * float(unit_price)

    inherited = {
        "poNumber": line.get("po_number") or header.get("po_number"),
        "poId": line.get("po_id") or header.get("po_id"),
    }
    for out_key, raw in inherited.items():
        if not _present(raw):
            continue
        val = _int_or_none(raw) if out_key == "poId" else str(raw)
        if val is not None:
            payload[out_key] = val

    return payload


def build_dispatch_note_with_lines_payload(
    raw_text: Dict[str, Any],
    *,
    tenant_id: int,
    company_id: int,
    fallback_vendor_id: int,
    created_by: int,
    request_po_number: Optional[str] = None,
    request_po_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    raw_text = apply_po_context_to_dispatch_note_raw_text(
        raw_text,
        po_number=request_po_number,
        po_id=request_po_id,
    )
    dispatch_note = raw_text.get("dispatch_note") if isinstance(raw_text, dict) else None
    if not isinstance(dispatch_note, dict):
        return None

    header = dispatch_note.get("header")
    if not isinstance(header, dict):
        return None

    po_number = header.get("po_number")
    dispatch_date = header.get("dispatch_date")
    if not _present(po_number):
        return None
    if not _present(dispatch_date):
        dispatch_date = date.today()
        print(
            "[DISPATCH NOTE OCR INSERT] dispatch_date missing; "
            f"defaulting invoiceDate to {dispatch_date.isoformat()}"
        )

    vendor_id = _int_or_none(header.get("vendor_id")) or int(fallback_vendor_id)
    payload: Dict[str, Any] = {
        "tenantId": int(tenant_id),
        "companyId": int(company_id),
        "departmentId": 3,
        "documentType": "DISPATCH_NOTE",
        "vendorId": vendor_id,
        "poNumber": str(po_number),
        # 3003 currently expects invoiceDate even for DISPATCH_NOTE.
        "invoiceDate": _jsonable(dispatch_date),
        "status": "DRAFT",
        "createdBy": int(created_by),
        "lines": [],
    }

    optional_header = {
        "poId": ("po_id", _int_or_none),
        "invoiceAmount": ("dispatch_amount", _float_or_none),
        "currencyCode": ("currency_code", str),
        "remarks": ("remarks", str),
    }
    for out_key, (in_key, caster) in optional_header.items():
        raw = header.get(in_key)
        if not _present(raw):
            continue
        val = caster(raw)
        if val is not None:
            payload[out_key] = val

    dispatch_no = header.get("dispatch_note_no")
    if _present(dispatch_no) and not _present(payload.get("remarks")):
        payload["remarks"] = f"Dispatch note {str(dispatch_no).strip()}"

    lines = dispatch_note.get("lines")
    if isinstance(lines, list):
        for idx, line in enumerate(lines, start=1):
            if not isinstance(line, dict):
                continue
            payload["lines"].append(_line_payload(line, idx=idx, header=header))

    if not payload["lines"]:
        return None

    return _jsonable(payload)


def post_dispatch_note_with_lines(payload: Dict[str, Any]) -> Dict[str, Any]:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("INTERNAL_API_URL is not configured")
    url = f"{base}/invoice-header/with-lines"
    print(f"[DISPATCH NOTE OCR INSERT] API: POST {url}")
    print(
        "[DISPATCH NOTE OCR INSERT] Payload:",
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
        print(f"[DISPATCH NOTE OCR INSERT] Request failed: {exc}")
        return {
            "status": "failed",
            "url": url,
            "error": str(exc),
            "payload": payload,
        }

    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    print(f"[DISPATCH NOTE OCR INSERT] Response status: {response.status_code}")
    print(f"[DISPATCH NOTE OCR INSERT] Response body: {body}")
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


def insert_ocr_dispatch_note(
    raw_text: Dict[str, Any],
    *,
    tenant_id: int,
    company_id: int,
    fallback_vendor_id: int,
    created_by: int,
    request_po_number: Optional[str] = None,
    request_po_id: Optional[int] = None,
) -> Dict[str, Any]:
    payload = build_dispatch_note_with_lines_payload(
        raw_text,
        tenant_id=tenant_id,
        company_id=company_id,
        fallback_vendor_id=fallback_vendor_id,
        created_by=created_by,
        request_po_number=request_po_number,
        request_po_id=request_po_id,
    )
    if payload is None:
        dispatch_note = raw_text.get("dispatch_note") if isinstance(raw_text, dict) else None
        print("[DISPATCH NOTE OCR INSERT] Skipped: incomplete dispatch note payload")
        print(
            "[DISPATCH NOTE OCR INSERT] Extracted dispatch_note:",
            json.dumps(dispatch_note, default=str, indent=2),
        )
        return {
            "status": "skipped",
            "reason": (
                "No complete dispatch note payload extracted. Required: "
            "dispatch_note.header.po_number and at least one line. "
            "dispatch_date is optional and defaults to today."
            ),
        }
    return post_dispatch_note_with_lines(payload)
