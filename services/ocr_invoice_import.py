from __future__ import annotations

from datetime import date, datetime
import json
import logging
from typing import Any, Dict, Optional

import requests
from sqlalchemy.orm import Session

from config import settings
from services.invoice_item_semantic_match import enrich_invoice_payload_with_semantic_matches
from services.invoice_line_embedding_sync import create_embeddings_for_invoice_payload
from services.invoice_po_semantic_match import (
    apply_resolved_po_to_invoice_raw_text,
    resolve_po_for_invoice_without_number,
)
from services.invoice_product_matching_engine import (
    enrich_invoice_payload_with_po_alias_matches,
)

logger = logging.getLogger(__name__)


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


def apply_po_context_to_invoice_raw_text(
    raw_text: Any,
    *,
    po_number: Optional[str] = None,
    po_id: Optional[int] = None,
) -> Any:
    """Merge PO context from upload request into OCR invoice extraction."""
    if not isinstance(raw_text, dict):
        return raw_text
    if not _present(po_number) and po_id is None:
        return raw_text

    invoice = raw_text.get("invoice")
    if not isinstance(invoice, dict):
        invoice = {}
        raw_text["invoice"] = invoice
    header = invoice.get("header")
    if not isinstance(header, dict):
        header = {}
        invoice["header"] = header

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
        "qtyInvoiced": ("qty_invoiced", _float_or_none),
        "unitPrice": ("unit_price", _float_or_none),
        "taxAmount": ("tax_amount", _float_or_none),
        "totalAmount": ("total_amount", _float_or_none),
    }
    for out_key, (in_key, caster) in mappings.items():
        raw = line.get(in_key)
        if not _present(raw):
            continue
        val = caster(raw)
        if val is not None:
            payload[out_key] = val

    # Optional extension: invoice_line.x_grn_ref_no
    for key in ("x_grn_ref_no", "xGrnRefNo", "grn_ref_no", "grnRefNo"):
        raw = line.get(key)
        if _present(raw):
            payload["xGrnRefNo"] = str(raw).strip()[:30]
            break

    if "totalAmount" not in payload:
        qty = payload.get("qtyInvoiced")
        unit_price = payload.get("unitPrice")
        tax_amount = payload.get("taxAmount") or 0
        if qty is not None and unit_price is not None:
            payload["totalAmount"] = (float(qty) * float(unit_price)) + float(tax_amount)

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

    if _present(line.get("line_status")):
        payload["lineStatus"] = str(line.get("line_status")).strip()
    elif _present(line.get("lineStatus")):
        payload["lineStatus"] = str(line.get("lineStatus")).strip()

    fp = line.get("item_fingerprint") or line.get("itemFingerprint")
    if isinstance(fp, dict) and fp:
        payload["itemFingerprint"] = fp

    return payload


def build_invoice_with_lines_payload(
    raw_text: Dict[str, Any],
    *,
    tenant_id: int,
    company_id: int,
    fallback_vendor_id: int,
    created_by: int,
    request_po_number: Optional[str] = None,
    request_po_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    raw_text = apply_po_context_to_invoice_raw_text(
        raw_text,
        po_number=request_po_number,
        po_id=request_po_id,
    )
    invoice = raw_text.get("invoice") if isinstance(raw_text, dict) else None
    if not isinstance(invoice, dict):
        return None

    header = invoice.get("header")
    if not isinstance(header, dict):
        return None

    po_number = header.get("po_number")
    invoice_date = header.get("invoice_date")
    if not _present(po_number):
        return None
    if not _present(invoice_date):
        invoice_date = date.today()
        print(
            "[INVOICE OCR INSERT] invoice_date missing; "
            f"defaulting invoiceDate to {invoice_date.isoformat()}"
        )

    vendor_id = _int_or_none(header.get("vendor_id")) or int(fallback_vendor_id)
    payload: Dict[str, Any] = {
        "tenantId": int(tenant_id),
        "companyId": int(company_id),
        "departmentId": 3,
        "documentType": str(header.get("document_type") or "IN"),
        "vendorId": vendor_id,
        "poNumber": str(po_number),
        "invoiceDate": _jsonable(invoice_date),
        "status": "DRAFT",
        "createdBy": int(created_by),
        "lines": [],
    }

    optional_header = {
        "invoiceNo": ("invoice_no", str),
        "versionNo": ("version_no", _int_or_none),
        "poId": ("po_id", _int_or_none),
        "invoiceAmount": ("invoice_amount", _float_or_none),
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

    lines = invoice.get("lines")
    if isinstance(lines, list):
        for idx, line in enumerate(lines, start=1):
            if not isinstance(line, dict):
                continue
            payload["lines"].append(_line_payload(line, idx=idx, header=header))

    if not payload["lines"]:
        return None

    return _jsonable(payload)


def post_invoice_with_lines(payload: Dict[str, Any]) -> Dict[str, Any]:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("INTERNAL_API_URL is not configured")
    url = f"{base}/invoice-header/with-lines"
    # Drop matching diagnostics before ERP POST (unknown fields may be rejected).
    erp_payload = _strip_matching_diagnostics(payload)
    print(f"[INVOICE OCR INSERT] API: POST {url}")
    print(
        "[INVOICE OCR INSERT] Payload:",
        json.dumps(erp_payload, default=str, indent=2),
    )
    try:
        response = requests.post(
            url,
            json=erp_payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        print(f"[INVOICE OCR INSERT] Request failed: {exc}")
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
    print(f"[INVOICE OCR INSERT] Response status: {response.status_code}")
    print(f"[INVOICE OCR INSERT] Response body: {body}")
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


def _strip_matching_diagnostics(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the invoice payload without internal match metadata."""
    drop_keys = {"_poAliasMatch", "semantic_matches"}
    cleaned = {
        k: v
        for k, v in payload.items()
        if k not in drop_keys and not str(k).startswith("_")
    }
    lines = cleaned.get("lines")
    if isinstance(lines, list):
        cleaned["lines"] = [
            {k: v for k, v in line.items() if not str(k).startswith("_")}
            if isinstance(line, dict)
            else line
            for line in lines
        ]
    return cleaned


def _sync_invoice_embeddings(
    db: Session,
    payload: Dict[str, Any],
    api_response: Any,
) -> None:
    try:
        create_embeddings_for_invoice_payload(
            db,
            payload,
            api_response=api_response,
        )
    except Exception as exc:
        logger.exception("Invoice embedding sync failed: %s", exc)


def _invoice_header_po_number(raw_text: Dict[str, Any]) -> Optional[str]:
    invoice = raw_text.get("invoice")
    if not isinstance(invoice, dict):
        return None
    header = invoice.get("header")
    if not isinstance(header, dict):
        return None
    po_number = header.get("po_number")
    return str(po_number).strip() if _present(po_number) else None


def insert_ocr_invoice(
    raw_text: Dict[str, Any],
    *,
    db: Session,
    tenant_id: int,
    company_id: int,
    fallback_vendor_id: int,
    created_by: int,
    request_po_number: Optional[str] = None,
    request_po_id: Optional[int] = None,
    attachment_path: Optional[str] = None,
) -> Dict[str, Any]:
    working_raw = raw_text
    po_semantic_match = None

    logger.info(
        "[INVOICE MATCH] step=start vendor_id=%s tenant_id=%s company_id=%s "
        "request_po_number=%s request_po_id=%s",
        fallback_vendor_id,
        tenant_id,
        company_id,
        request_po_number,
        request_po_id,
    )

    if isinstance(working_raw, dict) and not _present(request_po_number):
        if not _invoice_header_po_number(working_raw):
            logger.info(
                "[INVOICE MATCH] step=po_resolve_semantic reason=missing_po_number"
            )
            po_semantic_match = resolve_po_for_invoice_without_number(
                db,
                working_raw,
                vendor_id=int(fallback_vendor_id),
                tenant_id=int(tenant_id),
                company_id=int(company_id),
            )
            working_raw = apply_resolved_po_to_invoice_raw_text(
                working_raw,
                po_semantic_match,
            )
            if po_semantic_match.matched:
                print(
                    "[INVOICE OCR INSERT] Best PO match applied to invoice payload:",
                    {
                        "po_number": po_semantic_match.po_number,
                        "po_id": po_semantic_match.po_id,
                        "document_type": po_semantic_match.document_type,
                        "version_no": po_semantic_match.version_no,
                        "matched_lines": (
                            f"{po_semantic_match.matched_line_count}/"
                            f"{po_semantic_match.total_invoice_lines}"
                        ),
                        "match_ratio": po_semantic_match.match_ratio,
                        "average_distance": po_semantic_match.average_distance,
                    },
                )
                logger.info(
                    "[INVOICE MATCH] step=po_resolve_semantic result=matched "
                    "po_number=%s po_id=%s matched_lines=%s/%s",
                    po_semantic_match.po_number,
                    po_semantic_match.po_id,
                    po_semantic_match.matched_line_count,
                    po_semantic_match.total_invoice_lines,
                )
            else:
                print(
                    "[INVOICE OCR INSERT] No best PO match — semantic resolve failed:",
                    po_semantic_match.details,
                )
                logger.warning(
                    "[INVOICE MATCH] step=po_resolve_semantic result=failed details=%s",
                    po_semantic_match.details,
                )
        else:
            logger.info(
                "[INVOICE MATCH] step=po_resolve_skip reason=header_po_number_present po=%s",
                _invoice_header_po_number(working_raw),
            )
    elif _present(request_po_number):
        logger.info(
            "[INVOICE MATCH] step=po_resolve_skip reason=request_po_number po=%s",
            request_po_number,
        )

    logger.info("[INVOICE MATCH] step=build_payload")
    payload = build_invoice_with_lines_payload(
        working_raw,
        tenant_id=tenant_id,
        company_id=company_id,
        fallback_vendor_id=fallback_vendor_id,
        created_by=created_by,
        request_po_number=request_po_number,
        request_po_id=request_po_id,
    )
    if (
        payload is not None
        and po_semantic_match is not None
        and po_semantic_match.matched
        and po_semantic_match.document_type
    ):
        payload["poDocumentType"] = po_semantic_match.document_type
    if payload is not None and _present(attachment_path):
        payload["attachmentPath"] = str(attachment_path).strip()
        payload["isOcrProcessed"] = True
    if payload is None:
        invoice = raw_text.get("invoice") if isinstance(raw_text, dict) else None
        print("[INVOICE OCR INSERT] Skipped: incomplete invoice payload")
        print(
            "[INVOICE OCR INSERT] Extracted invoice:",
            json.dumps(invoice, default=str, indent=2),
        )
        logger.warning(
            "[INVOICE MATCH] step=build_payload result=skipped reason=incomplete_payload"
        )
        return {
            "status": "skipped",
            "reason": (
                "No complete invoice payload extracted. Required: "
                "invoice.header.po_number (or resolvable via pending PO semantic match) "
                "and at least one line. "
                "invoice_date is optional and defaults to today."
            ),
            "po_semantic_match": (
                {
                    "matched": po_semantic_match.matched,
                    "po_number": po_semantic_match.po_number,
                    "po_id": po_semantic_match.po_id,
                    "matched_line_count": po_semantic_match.matched_line_count,
                    "total_invoice_lines": po_semantic_match.total_invoice_lines,
                    "average_distance": po_semantic_match.average_distance,
                    "candidate_scores": po_semantic_match.candidate_scores,
                    "details": po_semantic_match.details,
                }
                if po_semantic_match is not None
                else None
            ),
        }

    line_count = len(payload.get("lines") or []) if isinstance(payload, dict) else 0
    logger.info(
        "[INVOICE MATCH] step=build_payload result=ok po_number=%s po_id=%s lines=%s",
        payload.get("poNumber") or payload.get("po_number"),
        payload.get("poId") or payload.get("po_id"),
        line_count,
    )

    logger.info("[INVOICE MATCH] step=po_product_matching_engine")
    payload = enrich_invoice_payload_with_po_alias_matches(db, payload)
    po_alias_match = payload.get("_poAliasMatch")
    if isinstance(po_alias_match, dict):
        logger.info(
            "[INVOICE MATCH] step=po_product_matching_engine result=done "
            "alias=%s auto=%s awaiting_user=%s fingerprints=%s",
            po_alias_match.get("matched_line_count"),
            po_alias_match.get("auto_matched"),
            po_alias_match.get("awaiting_user_confirm"),
            po_alias_match.get("fingerprints_created"),
        )

    logger.info("[INVOICE MATCH] step=semantic_rate_enrichment")
    payload = enrich_invoice_payload_with_semantic_matches(db, payload)
    semantic_matches = payload.get("semantic_matches") if isinstance(payload, dict) else None
    if isinstance(semantic_matches, list):
        logger.info(
            "[INVOICE MATCH] step=semantic_rate_enrichment result=done "
            "checked=%s matched=%s",
            len(semantic_matches),
            sum(1 for m in semantic_matches if m.get("matched")),
        )
    else:
        logger.info(
            "[INVOICE MATCH] step=semantic_rate_enrichment result=skipped "
            "reason=no_eligible_lines"
        )

    logger.info("[INVOICE MATCH] step=post_invoice_to_erp")
    result = post_invoice_with_lines(payload)
    logger.info(
        "[INVOICE MATCH] step=post_invoice_to_erp result=%s",
        result.get("status") if isinstance(result, dict) else type(result).__name__,
    )
    if po_alias_match is not None:
        result["po_alias_match"] = po_alias_match
    if po_semantic_match is not None:
        result["po_semantic_match"] = {
            "matched": po_semantic_match.matched,
            "po_number": po_semantic_match.po_number,
            "po_id": po_semantic_match.po_id,
            "matched_line_count": po_semantic_match.matched_line_count,
            "total_invoice_lines": po_semantic_match.total_invoice_lines,
            "average_distance": po_semantic_match.average_distance,
            "match_ratio": po_semantic_match.match_ratio,
            "candidate_scores": po_semantic_match.candidate_scores,
            "details": po_semantic_match.details,
        }
    if result.get("status") == "inserted":
        logger.info("[INVOICE MATCH] step=sync_invoice_embeddings")
        _sync_invoice_embeddings(db, payload, result.get("response"))
        if payload.get("semantic_matches"):
            result["semantic_matches"] = payload["semantic_matches"]
        logger.info("[INVOICE MATCH] step=done status=inserted")
    elif payload.get("semantic_matches"):
        result["semantic_matches"] = payload["semantic_matches"]
        logger.info(
            "[INVOICE MATCH] step=done status=%s",
            result.get("status") if isinstance(result, dict) else "unknown",
        )
    else:
        logger.info(
            "[INVOICE MATCH] step=done status=%s",
            result.get("status") if isinstance(result, dict) else "unknown",
        )
    return result
