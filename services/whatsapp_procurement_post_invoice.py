"""
WhatsApp post-invoice procurement steps — same ERP / ES paths the UI uses:

1. Approve invoice  → confirm invoice line matches (`confirm_invoice_line_match`)
2. Update GRN qty   → create GRN from PO (`POST /grn-header/from-po-number`) +
                      PATCH line qtyReceived from invoice qtyInvoiced
3. Approve GRN      → PATCH grn-header status=CONFIRMED
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config import settings
from services.company_scope import resolve_company_id
from services.invoice_line_match_service import (
    confirm_invoice_line_match,
    delete_erp_invoice_line,
    get_invoice_match_review,
)

logger = logging.getLogger(__name__)


def _erp_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_API_URL is not configured",
        )
    return base


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


def _pick_float(row: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _erp_error_detail(resp: requests.Response, fallback: str) -> str:
    """Readable ERP failure text for WhatsApp (include HTTP status + body detail)."""
    status = getattr(resp, "status_code", None)
    prefix = f"{fallback} (HTTP {status})" if status else fallback
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            msg = payload.get("message") or payload.get("detail") or payload.get("error")
            if isinstance(msg, list):
                parts = []
                for item in msg:
                    if isinstance(item, dict):
                        parts.append(str(item.get("msg") or item.get("message") or item))
                    else:
                        parts.append(str(item))
                msg = "; ".join(p for p in parts if p)
            if msg:
                return f"{prefix}: {msg}"[:500]
        if payload is not None:
            return f"{prefix}: {str(payload)[:300]}"
    except Exception:
        pass
    text = (resp.text or "").strip()
    if text:
        return f"{prefix}: {text[:300]}"
    return prefix


def list_invoices_by_po_number(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
) -> List[Dict[str, Any]]:
    url = f"{_erp_base()}/purchase-order-header/invoices/by-po-number"
    params = {
        "poNumber": po_number,
        "tenantId": tenant_id,
        "companyId": company_id,
    }
    resp = requests.get(url, params=params, timeout=60)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=_erp_error_detail(resp, "List invoices by PO failed"),
        )
    data = resp.json()
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        raw = data.get("items")
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)]
    return []


def list_grns_by_po_number(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
) -> List[Dict[str, Any]]:
    url = f"{_erp_base()}/grn-header/with-lines"
    params = {
        "poNumber": po_number,
        "tenantId": tenant_id,
        "companyId": company_id,
        "limit": "100",
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


def create_grn_from_po_number(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    status_value: str = "DRAFT",
) -> Dict[str, Any]:
    url = f"{_erp_base()}/grn-header/from-po-number"
    payload = {
        "poNumber": po_number,
        "tenantId": tenant_id,
        "companyId": company_id,
        "status": status_value,
        "documentType": "GRN",
    }
    resp = requests.post(url, json=payload, timeout=120)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=_erp_error_detail(resp, "Create GRN from PO failed"),
        )
    body = resp.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Invalid create GRN response")
    return body


def patch_grn_line_qty(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    grn_number: str,
    version_no: int,
    line_no: int,
    qty_received: float,
    batch_no: Optional[str] = None,
    expiry_date: Optional[str] = None,
) -> Dict[str, Any]:
    url = f"{_erp_base()}/grn-line/by-key"
    params = {
        "tenantId": tenant_id,
        "companyId": company_id,
        "documentType": document_type,
        "grnNumber": grn_number,
        "versionNo": version_no,
        "lineNo": line_no,
    }
    payload: Dict[str, Any] = {
        "qtyReceived": qty_received,
        "qtyAccepted": qty_received,
        "qtyRejected": 0,
    }
    if batch_no and str(batch_no).strip():
        payload["batchNo"] = str(batch_no).strip()[:100]
    if expiry_date and str(expiry_date).strip():
        payload["expiryDate"] = str(expiry_date).strip()[:10]
    resp = requests.patch(url, params=params, json=payload, timeout=60)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=_erp_error_detail(resp, "Update GRN line qty failed"),
        )
    body = resp.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Invalid GRN line update response")
    return body


def patch_grn_header_status(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    grn_number: str,
    version_no: int,
    status_value: str,
) -> Dict[str, Any]:
    url = f"{_erp_base()}/grn-header/by-key"
    params = {
        "tenantId": tenant_id,
        "companyId": company_id,
        "documentType": document_type,
        "grnNumber": grn_number,
        "versionNo": version_no,
    }
    resp = requests.patch(url, params=params, json={"status": status_value}, timeout=60)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=_erp_error_detail(resp, "Update GRN header failed"),
        )
    body = resp.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Invalid GRN header update response")
    return body


def _resolve_invoice_for_session(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    invoice_no: Optional[str] = None,
) -> Dict[str, Any]:
    invoices = list_invoices_by_po_number(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
    )
    if not invoices:
        raise HTTPException(
            status_code=404,
            detail=f"No invoice found for PO {po_number}. Upload an invoice first.",
        )

    wanted = (invoice_no or "").strip().upper()
    if wanted:
        for inv in invoices:
            if _pick_str(inv, "invoiceNo", "invoice_no").upper() == wanted:
                return inv

    # Prefer newest / first listed invoice for the PO.
    return invoices[0]


def _sidebar_candidates(review: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Same as UI sidebarCandidates — ranked, else suggested, else allPoLines."""
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def add(opt: Any) -> None:
        if not isinstance(opt, dict):
            return
        po_line = _pick_int(opt, "poLineNo", "po_line_no")
        if po_line is None or po_line in seen:
            return
        seen.add(po_line)
        out.append(
            {
                "poLineNo": po_line,
                "itemId": _pick_int(opt, "itemId", "item_id"),
                "productName": _pick_str(opt, "productName", "product_name"),
                "description": _pick_str(opt, "description"),
                "score": opt.get("score"),
                "productFingerprint": (
                    opt.get("productFingerprint")
                    if isinstance(opt.get("productFingerprint"), dict)
                    else None
                ),
            }
        )

    ranked = review.get("rankedCandidates")
    if isinstance(ranked, list) and ranked:
        for opt in ranked:
            add(opt)
        return out

    suggested = review.get("suggested")
    if isinstance(suggested, dict):
        add(suggested)
        if out:
            return out

    all_po = review.get("allPoLines")
    if isinstance(all_po, list):
        for opt in all_po:
            add(opt)
    return out


def _classify_invoice_match_lines(
    lines: List[Any],
) -> Dict[str, Any]:
    """
    Same buckets as UI Invoice Review badges:
    CONFIRMED / AUTO (suggested) / NO_MATCH (product not on PO) / REVIEW (Select match).
    """
    already: List[Dict[str, Any]] = []
    auto_lines: List[Dict[str, Any]] = []
    no_match_lines: List[Dict[str, Any]] = []
    review_lines: List[Dict[str, Any]] = []

    for line in lines:
        if not isinstance(line, dict):
            continue
        line_no = _pick_int(line, "lineNo", "line_no")
        if line_no is None:
            continue
        match_review = (
            line.get("matchReview") if isinstance(line.get("matchReview"), dict) else {}
        )
        suggested = (
            match_review.get("suggested")
            if isinstance(match_review.get("suggested"), dict)
            else None
        )
        has_suggested = (
            isinstance(suggested, dict)
            and _pick_int(suggested, "poLineNo", "po_line_no") is not None
        )
        candidates = _sidebar_candidates(match_review)
        description = (
            _pick_str(line, "description")
            or _pick_str(match_review, "productName", "product_name")
            or f"Invoice line {line_no}"
        )
        line_status = _pick_str(line, "lineStatus", "line_status").upper()
        match_color = (
            _pick_str(line, "matchColor", "match_color")
            or _pick_str(match_review, "matchColor", "match_color")
        ).lower()
        suggested_po = (
            _pick_int(suggested, "poLineNo", "po_line_no") if has_suggested else None
        )
        suggested_name = (
            (
                _pick_str(suggested, "productName", "product_name", "description")
                if isinstance(suggested, dict)
                else ""
            )
            or ""
        )

        if line_status == "CONFIRMED":
            already.append(
                {
                    "lineNo": line_no,
                    "description": description,
                    "poLineNo": _pick_int(line, "poLineNo", "po_line_no"),
                    "badge": "CONFIRMED",
                }
            )
            continue

        if line_status == "NO_MATCH" or match_color == "red":
            if not candidates:
                existing_po = _pick_int(line, "poLineNo", "po_line_no")
                if existing_po is not None:
                    candidates = [
                        {
                            "poLineNo": existing_po,
                            "itemId": _pick_int(line, "itemId", "item_id"),
                            "productName": description,
                            "description": description,
                            "score": None,
                            "productFingerprint": None,
                        }
                    ]
            no_match_lines.append(
                {
                    "lineNo": line_no,
                    "description": description,
                    "badge": "NO_MATCH",
                    "noMatch": True,
                    "candidates": candidates,
                    "canDelete": True,
                }
            )
            continue

        if has_suggested:
            auto_lines.append(
                {
                    "lineNo": line_no,
                    "description": description,
                    "poLineNo": suggested_po,
                    "matchedName": suggested_name or None,
                    "badge": "AUTO",
                    "suggested": suggested,
                    "candidates": candidates,
                }
            )
            continue

        if not candidates:
            existing_po = _pick_int(line, "poLineNo", "po_line_no")
            if existing_po is not None:
                candidates = [
                    {
                        "poLineNo": existing_po,
                        "itemId": _pick_int(line, "itemId", "item_id"),
                        "productName": description,
                        "description": description,
                        "score": None,
                        "productFingerprint": None,
                    }
                ]

        review_lines.append(
            {
                "lineNo": line_no,
                "description": description,
                "badge": "REVIEW",
                "candidates": candidates,
            }
        )

    return {
        "already": already,
        "autoLines": auto_lines,
        "noMatchLines": no_match_lines,
        "reviewLines": review_lines,
    }


def preview_invoice_match_for_whatsapp(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    invoice_no: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Read-only Invoice Review summary for WhatsApp after OCR upload.
    Does not confirm any lines — same AUTO / REVIEW badges as the portal.
    """
    company_id = resolve_company_id(company_id)
    inv = _resolve_invoice_for_session(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=invoice_no,
    )
    document_type = _pick_str(inv, "documentType", "document_type", default="IN") or "IN"
    resolved_invoice_no = _pick_str(inv, "invoiceNo", "invoice_no")
    version_no = _pick_int(inv, "versionNo", "version_no", default=1) or 1
    if not resolved_invoice_no:
        raise HTTPException(status_code=502, detail="Invoice missing invoiceNo")

    review = get_invoice_match_review(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=resolved_invoice_no,
        version_no=version_no,
    )
    lines = review.get("lines") if isinstance(review.get("lines"), list) else []
    classified = _classify_invoice_match_lines(lines)
    auto_lines = classified["autoLines"]
    no_match_lines = classified["noMatchLines"]
    review_lines = classified["reviewLines"]
    already = classified["already"]

    line_summaries: List[Dict[str, Any]] = []
    for row in already:
        line_summaries.append(
            {
                "lineNo": row["lineNo"],
                "description": row["description"],
                "badge": "CONFIRMED",
                "poLineNo": row.get("poLineNo"),
            }
        )
    for row in auto_lines:
        line_summaries.append(
            {
                "lineNo": row["lineNo"],
                "description": row["description"],
                "badge": "AUTO",
                "poLineNo": row.get("poLineNo"),
                "matchedName": row.get("matchedName"),
            }
        )
    for row in no_match_lines:
        line_summaries.append(
            {
                "lineNo": row["lineNo"],
                "description": row["description"],
                "badge": "NO_MATCH",
                "noMatch": True,
                "canDelete": True,
                "needsSelectMatch": True,
                "candidateCount": len(row.get("candidates") or []),
            }
        )
    for row in review_lines:
        line_summaries.append(
            {
                "lineNo": row["lineNo"],
                "description": row["description"],
                "badge": "REVIEW",
                "needsSelectMatch": True,
                "candidateCount": len(row.get("candidates") or []),
            }
        )

    pending_manual = [*no_match_lines, *review_lines]
    needs_manual = len(pending_manual) > 0
    logger.info(
        "[whatsapp-post-invoice] preview_invoice po=%s invoice=%s auto=%s no_match=%s review=%s confirmed=%s",
        po_number,
        resolved_invoice_no,
        len(auto_lines),
        len(no_match_lines),
        len(review_lines),
        len(already),
    )
    return {
        "status": "ok",
        "poNumber": po_number,
        "invoiceNo": resolved_invoice_no,
        "documentType": document_type,
        "versionNo": version_no,
        "lineCount": len(line_summaries),
        "autoCount": len(auto_lines),
        "noMatchCount": len(no_match_lines),
        "reviewCount": len(review_lines),
        "alreadyConfirmedCount": len(already),
        "needsManualMatch": needs_manual,
        "lines": line_summaries,
        "pendingLines": [
            {
                "lineNo": row["lineNo"],
                "description": row["description"],
                "badge": row.get("badge") or "REVIEW",
                "noMatch": bool(row.get("noMatch")),
                "canDelete": bool(row.get("canDelete") or row.get("noMatch")),
                "candidates": row.get("candidates") or [],
            }
            for row in pending_manual
        ],
    }


def approve_invoice_for_whatsapp(
    db: Session,
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    invoice_no: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Same as UI Invoice Review Confirm + sidebar rematch:
    1) Auto-confirm lines that have a primary match (UI Confirm).
    2) Return pending lines with sidebar candidates for manual pick (UI sidebar).
    """
    company_id = resolve_company_id(company_id)
    inv = _resolve_invoice_for_session(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=invoice_no,
    )
    document_type = _pick_str(inv, "documentType", "document_type", default="IN") or "IN"
    resolved_invoice_no = _pick_str(inv, "invoiceNo", "invoice_no")
    version_no = _pick_int(inv, "versionNo", "version_no", default=1) or 1
    if not resolved_invoice_no:
        raise HTTPException(status_code=502, detail="Invoice missing invoiceNo")

    review = get_invoice_match_review(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=resolved_invoice_no,
        version_no=version_no,
    )
    lines = review.get("lines") if isinstance(review.get("lines"), list) else []
    classified = _classify_invoice_match_lines(lines)
    auto_lines = classified["autoLines"]
    no_match_lines = classified["noMatchLines"]
    pending_lines = [*no_match_lines, *classified["reviewLines"]]
    already_rows = classified["already"]
    already = [int(row["lineNo"]) for row in already_rows]

    confirmed: List[Dict[str, Any]] = []
    for row in auto_lines:
        line_no = int(row["lineNo"])
        suggested = row.get("suggested") if isinstance(row.get("suggested"), dict) else {}
        selected_po_line = int(
            _pick_int(suggested, "poLineNo", "po_line_no") or row.get("poLineNo") or 0
        )
        selected_item_id = _pick_int(suggested, "itemId", "item_id")
        fingerprint = (
            suggested.get("productFingerprint")
            if isinstance(suggested.get("productFingerprint"), dict)
            else None
        )
        result = confirm_invoice_line_match(
            db,
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=document_type,
            invoice_no=resolved_invoice_no,
            version_no=version_no,
            line_no=line_no,
            selected_po_line_no=selected_po_line,
            selected_item_id=selected_item_id,
            selected_product_fingerprint=fingerprint,
            create_alias=True,
        )
        confirmed.append(
            {
                "lineNo": line_no,
                "poLineNo": selected_po_line,
                "status": result.get("status"),
                "auto": True,
            }
        )

    if not confirmed and not already and not pending_lines:
        raise HTTPException(
            status_code=409,
            detail="No invoice lines found to confirm for this invoice.",
        )

    # NO_MATCH lines can be deleted even without PO options; only block when
    # REVIEW lines have no candidates and no delete path.
    blocked_review = [
        p
        for p in pending_lines
        if not (p.get("candidates") or []) and not p.get("canDelete")
    ]
    if pending_lines and blocked_review and len(blocked_review) == len(pending_lines):
        raise HTTPException(
            status_code=409,
            detail=(
                "Invoice lines need a PO match (Select match), but no PO line options "
                "are available. Check the PO lines, then try again."
            ),
        )

    # Any REVIEW / NO_MATCH line blocks full approval.
    needs_manual = len(pending_lines) > 0
    interactive = [
        p for p in pending_lines if (p.get("candidates") or []) or p.get("canDelete")
    ]
    logger.info(
        "[whatsapp-post-invoice] approve_invoice po=%s invoice=%s confirmed=%s already=%s pending=%s no_match=%s",
        po_number,
        resolved_invoice_no,
        len(confirmed),
        len(already),
        len(pending_lines),
        len(no_match_lines),
    )
    return {
        "status": "ok",
        "poNumber": po_number,
        "invoiceNo": resolved_invoice_no,
        "documentType": document_type,
        "versionNo": version_no,
        "confirmedCount": len(confirmed),
        "alreadyConfirmedCount": len(already),
        "pendingCount": len(pending_lines),
        "noMatchCount": len(no_match_lines),
        "needsManualMatch": needs_manual,
        "pendingLines": [
            {
                "lineNo": p["lineNo"],
                "description": p["description"],
                "badge": p.get("badge") or "REVIEW",
                "noMatch": bool(p.get("noMatch")),
                "canDelete": bool(p.get("canDelete") or p.get("noMatch")),
                "candidates": p.get("candidates") or [],
            }
            for p in interactive
        ],
        "confirmed": confirmed,
        "reviewLinesWithoutOptions": [
            {
                "lineNo": p["lineNo"],
                "description": p["description"],
                "noMatch": bool(p.get("noMatch")),
            }
            for p in pending_lines
            if not (p.get("candidates") or []) and not p.get("canDelete")
        ],
    }


def confirm_invoice_line_for_whatsapp(
    db: Session,
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    invoice_no: str,
    document_type: str,
    version_no: int,
    line_no: int,
    selected_po_line_no: int,
    selected_item_id: Optional[int] = None,
    selected_product_fingerprint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Same as UI sidebar confirm for one invoice line."""
    company_id = resolve_company_id(company_id)
    result = confirm_invoice_line_match(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type or "IN",
        invoice_no=invoice_no,
        version_no=version_no or 1,
        line_no=line_no,
        selected_po_line_no=selected_po_line_no,
        selected_item_id=selected_item_id,
        selected_product_fingerprint=selected_product_fingerprint,
        create_alias=True,
    )
    logger.info(
        "[whatsapp-post-invoice] confirm_line po=%s invoice=%s line=%s poLine=%s",
        po_number,
        invoice_no,
        line_no,
        selected_po_line_no,
    )
    return {
        "status": "ok",
        "poNumber": po_number,
        "invoiceNo": invoice_no,
        "documentType": document_type or "IN",
        "versionNo": version_no or 1,
        "lineNo": line_no,
        "poLineNo": selected_po_line_no,
        "confirmed": result,
    }


def delete_invoice_line_for_whatsapp(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    invoice_no: str,
    document_type: str,
    version_no: int,
    line_no: int,
) -> Dict[str, Any]:
    """Remove an unmatched invoice line (product not on PO)."""
    company_id = resolve_company_id(company_id)
    result = delete_erp_invoice_line(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type or "IN",
        invoice_no=invoice_no,
        version_no=version_no or 1,
        line_no=line_no,
    )
    logger.info(
        "[whatsapp-post-invoice] delete_line po=%s invoice=%s line=%s",
        po_number,
        invoice_no,
        line_no,
    )
    return {
        "status": "ok",
        "poNumber": po_number,
        "invoiceNo": invoice_no,
        "lineNo": line_no,
        "deleted": True,
        "result": result,
    }


def _invoice_qty_by_po_line(inv: Dict[str, Any]) -> Dict[int, float]:
    qty_map: Dict[int, float] = {}
    lines = inv.get("lines") if isinstance(inv.get("lines"), list) else []
    for line in lines:
        if not isinstance(line, dict):
            continue
        po_line = _pick_int(line, "poLineNo", "po_line_no")
        if po_line is None:
            continue
        qty = _pick_float(line, "qtyInvoiced", "qty_invoiced", "quantity", "qty")
        if qty is None:
            continue
        qty_map[po_line] = qty
    return qty_map


def _pick_active_grn(
    grns: List[Dict[str, Any]],
    preferred_grn_number: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    wanted = (preferred_grn_number or "").strip().upper()
    if wanted:
        for grn in grns:
            if _pick_str(grn, "grnNumber", "grn_number").upper() == wanted:
                return grn

    # Prefer non-confirmed GRN (still editable), else latest.
    drafts: List[Dict[str, Any]] = []
    for grn in grns:
        st = _pick_str(grn, "status").upper()
        if st != "CONFIRMED":
            drafts.append(grn)
    if drafts:
        return drafts[0]
    return grns[0] if grns else None


def _ensure_draft_grn_for_po(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    grn_number: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Return (grn_header_with_lines, created)."""
    grns = list_grns_by_po_number(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
    )
    created = False
    grn = _pick_active_grn(grns, grn_number)

    if grn is None or _pick_str(grn, "status").upper() == "CONFIRMED":
        if grn is not None and _pick_str(grn, "status").upper() == "CONFIRMED" and not grn_number:
            grn = None
        if grn is None:
            created_body = create_grn_from_po_number(
                po_number=po_number,
                tenant_id=tenant_id,
                company_id=company_id,
                status_value="DRAFT",
            )
            created = True
            grns = list_grns_by_po_number(
                po_number=po_number,
                tenant_id=tenant_id,
                company_id=company_id,
            )
            created_no = _pick_str(created_body, "grnNumber", "grn_number")
            grn = _pick_active_grn(grns, created_no or grn_number)
            if grn is None and isinstance(created_body, dict):
                grn = created_body

    if grn is None:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not create or load a GRN for PO {po_number}. "
                "Check ERP connectivity, then reply *update grn qty* again."
            ),
        )

    grn_doc = _pick_str(grn, "documentType", "document_type", default="GRN") or "GRN"
    resolved_grn_no = _pick_str(grn, "grnNumber", "grn_number")
    version_no = _pick_int(grn, "versionNo", "version_no", default=1) or 1
    if not resolved_grn_no:
        raise HTTPException(
            status_code=502,
            detail="ERP returned a GRN without a grnNumber. Please try again.",
        )

    if _pick_str(grn, "status").upper() == "CONFIRMED":
        raise HTTPException(
            status_code=409,
            detail=(
                f"GRN {resolved_grn_no} is already confirmed. "
                "Create a new receipt if needed."
            ),
        )

    if _pick_str(grn, "status").upper() != "DRAFT":
        patch_grn_header_status(
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=grn_doc,
            grn_number=resolved_grn_no,
            version_no=version_no,
            status_value="DRAFT",
        )
        grn = {**grn, "status": "DRAFT"}

    return grn, created


def prepare_grn_for_whatsapp(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    invoice_no: Optional[str] = None,
    grn_number: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create/load draft GRN for the PO and return line items with suggested qty
    (from invoice). Does not PATCH qty — WhatsApp asks the user first.
    """
    company_id = resolve_company_id(company_id)
    inv = _resolve_invoice_for_session(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
        invoice_no=invoice_no,
    )
    resolved_invoice_no = _pick_str(inv, "invoiceNo", "invoice_no")
    qty_by_po_line = _invoice_qty_by_po_line(inv)

    grn, created = _ensure_draft_grn_for_po(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
        grn_number=grn_number,
    )
    grn_doc = _pick_str(grn, "documentType", "document_type", default="GRN") or "GRN"
    resolved_grn_no = _pick_str(grn, "grnNumber", "grn_number")
    version_no = _pick_int(grn, "versionNo", "version_no", default=1) or 1

    lines_out: List[Dict[str, Any]] = []
    for line in grn.get("lines") if isinstance(grn.get("lines"), list) else []:
        if not isinstance(line, dict):
            continue
        line_no = _pick_int(line, "lineNo", "line_no")
        if line_no is None:
            continue
        po_line = _pick_int(line, "poLineNo", "po_line_no")
        suggested = qty_by_po_line.get(po_line) if po_line is not None else None
        if suggested is None:
            suggested = _pick_float(line, "qtyReceived", "qty_received")
        lines_out.append(
            {
                "lineNo": line_no,
                "poLineNo": po_line,
                "itemId": _pick_int(line, "itemId", "item_id"),
                "description": _pick_str(line, "description") or f"Line {line_no}",
                "uom": _pick_str(line, "uom"),
                "suggestedQty": suggested,
                "currentQty": _pick_float(line, "qtyReceived", "qty_received"),
            }
        )

    if not lines_out:
        raise HTTPException(
            status_code=409,
            detail=f"GRN {resolved_grn_no} has no lines to set quantity on.",
        )

    logger.info(
        "[whatsapp-post-invoice] prepare_grn po=%s grn=%s created=%s lines=%s",
        po_number,
        resolved_grn_no,
        created,
        len(lines_out),
    )
    return {
        "status": "ok",
        "poNumber": po_number,
        "invoiceNo": resolved_invoice_no,
        "grnNumber": resolved_grn_no,
        "documentType": grn_doc,
        "versionNo": version_no,
        "created": created,
        "lineCount": len(lines_out),
        "lines": lines_out,
        "grnStatus": "DRAFT",
        "awaitingQty": True,
    }


def apply_grn_qtys_for_whatsapp(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    line_qtys: List[Dict[str, Any]],
    invoice_no: Optional[str] = None,
    grn_number: Optional[str] = None,
) -> Dict[str, Any]:
    """
    PATCH grn-line qtyReceived for user-entered quantities
    (same as UI save GRN qty).
    """
    company_id = resolve_company_id(company_id)
    if not line_qtys:
        raise HTTPException(status_code=400, detail="line_qtys is required")

    inv = None
    resolved_invoice_no: Optional[str] = None
    try:
        inv = _resolve_invoice_for_session(
            po_number=po_number,
            tenant_id=tenant_id,
            company_id=company_id,
            invoice_no=invoice_no,
        )
        resolved_invoice_no = _pick_str(inv, "invoiceNo", "invoice_no")
    except HTTPException:
        resolved_invoice_no = invoice_no

    grn, _created = _ensure_draft_grn_for_po(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
        grn_number=grn_number,
    )
    grn_doc = _pick_str(grn, "documentType", "document_type", default="GRN") or "GRN"
    resolved_grn_no = _pick_str(grn, "grnNumber", "grn_number")
    version_no = _pick_int(grn, "versionNo", "version_no", default=1) or 1

    updated_lines: List[Dict[str, Any]] = []
    for entry in line_qtys:
        if not isinstance(entry, dict):
            continue
        line_no = _pick_int(entry, "lineNo", "line_no")
        qty = _pick_float(entry, "qtyReceived", "qty_received", "qty")
        if line_no is None or qty is None:
            continue
        if qty < 0:
            raise HTTPException(
                status_code=400,
                detail=f"Quantity for line {line_no} cannot be negative.",
            )
        batch_no = _pick_str(entry, "batchNo", "batch_no") or None
        expiry_date = _pick_str(entry, "expiryDate", "expiry_date") or None
        patch_grn_line_qty(
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=grn_doc,
            grn_number=resolved_grn_no,
            version_no=version_no,
            line_no=line_no,
            qty_received=float(qty),
            batch_no=batch_no,
            expiry_date=expiry_date,
        )
        updated_lines.append(
            {
                "lineNo": line_no,
                "poLineNo": _pick_int(entry, "poLineNo", "po_line_no"),
                "qtyReceived": float(qty),
                "description": _pick_str(entry, "description") or None,
                "batchNo": batch_no,
                "expiryDate": expiry_date,
            }
        )

    if not updated_lines:
        raise HTTPException(status_code=400, detail="No valid line quantities to apply.")

    logger.info(
        "[whatsapp-post-invoice] apply_grn_qtys po=%s grn=%s lines=%s",
        po_number,
        resolved_grn_no,
        len(updated_lines),
    )
    return {
        "status": "ok",
        "poNumber": po_number,
        "invoiceNo": resolved_invoice_no,
        "grnNumber": resolved_grn_no,
        "documentType": grn_doc,
        "versionNo": version_no,
        "created": False,
        "updatedLineCount": len(updated_lines),
        "lines": updated_lines,
        "grnStatus": "DRAFT",
    }


def update_grn_qty_for_whatsapp(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    invoice_no: Optional[str] = None,
    grn_number: Optional[str] = None,
    line_qtys: Optional[List[Dict[str, Any]]] = None,
    prepare_only: bool = False,
) -> Dict[str, Any]:
    """
    Create GRN if needed. If prepare_only / no line_qtys: return lines for
    WhatsApp to ask qty. If line_qtys provided: apply those quantities.
    """
    if prepare_only or not line_qtys:
        return prepare_grn_for_whatsapp(
            po_number=po_number,
            tenant_id=tenant_id,
            company_id=company_id,
            invoice_no=invoice_no,
            grn_number=grn_number,
        )
    return apply_grn_qtys_for_whatsapp(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
        line_qtys=line_qtys,
        invoice_no=invoice_no,
        grn_number=grn_number,
    )


def approve_grn_for_whatsapp(
    *,
    po_number: str,
    tenant_id: int,
    company_id: int,
    grn_number: Optional[str] = None,
) -> Dict[str, Any]:
    """Set GRN header status to CONFIRMED — same as UI Confirm GRN."""
    company_id = resolve_company_id(company_id)
    grns = list_grns_by_po_number(
        po_number=po_number,
        tenant_id=tenant_id,
        company_id=company_id,
    )
    if not grns:
        raise HTTPException(
            status_code=404,
            detail=f"No GRN found for PO {po_number}. Reply *update grn qty* first.",
        )

    grn = _pick_active_grn(grns, grn_number)
    if grn is None:
        raise HTTPException(status_code=404, detail="GRN not found")

    grn_doc = _pick_str(grn, "documentType", "document_type", default="GRN") or "GRN"
    resolved_grn_no = _pick_str(grn, "grnNumber", "grn_number")
    version_no = _pick_int(grn, "versionNo", "version_no", default=1) or 1
    if not resolved_grn_no:
        raise HTTPException(status_code=502, detail="GRN missing grnNumber")

    current = _pick_str(grn, "status").upper()
    if current == "CONFIRMED":
        return {
            "status": "ok",
            "alreadyConfirmed": True,
            "poNumber": po_number,
            "grnNumber": resolved_grn_no,
            "documentType": grn_doc,
            "versionNo": version_no,
            "grnStatus": "CONFIRMED",
        }

    patch_grn_header_status(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=grn_doc,
        grn_number=resolved_grn_no,
        version_no=version_no,
        status_value="CONFIRMED",
    )
    logger.info(
        "[whatsapp-post-invoice] approve_grn po=%s grn=%s",
        po_number,
        resolved_grn_no,
    )
    return {
        "status": "ok",
        "alreadyConfirmed": False,
        "poNumber": po_number,
        "grnNumber": resolved_grn_no,
        "documentType": grn_doc,
        "versionNo": version_no,
        "grnStatus": "CONFIRMED",
    }
