"""Confirm invoice line product matches and update ERP invoice_line rows."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config import settings
from services.invoice_product_matching_engine import (
    LINE_STATUS_CONFIRMED,
    build_item_fingerprint_with_match_review,
)
from services.ocr_alias_dictionary import create_product_name_alias

logger = logging.getLogger(__name__)


def _erp_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_API_URL is not configured",
        )
    return base


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def get_erp_invoice_line(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int,
    line_no: int,
) -> Dict[str, Any]:
    url = f"{_erp_base()}/invoice-line/by-key"
    params = {
        "tenantId": tenant_id,
        "companyId": company_id,
        "documentType": document_type,
        "invoiceNo": invoice_no,
        "versionNo": version_no,
        "lineNo": line_no,
    }
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Invoice line not found")
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"ERP invoice line fetch failed: {(resp.text or '')[:200]}",
        )
    body = resp.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Invalid ERP invoice line response")
    return body


def list_erp_invoice_lines_for_header(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int,
) -> List[Dict[str, Any]]:
    url = f"{_erp_base()}/invoice-line"
    params = {
        "tenantId": tenant_id,
        "companyId": company_id,
        "documentType": document_type,
        "invoiceNo": invoice_no,
        "versionNo": version_no,
        "limit": 500,
    }
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"ERP invoice line list failed: {(resp.text or '')[:200]}",
        )
    body = resp.json()
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict):
        items = body.get("items")
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    return []


def patch_erp_invoice_line(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int,
    line_no: int,
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    url = f"{_erp_base()}/invoice-line/by-key"
    params = {
        "tenantId": tenant_id,
        "companyId": company_id,
        "documentType": document_type,
        "invoiceNo": invoice_no,
        "versionNo": version_no,
        "lineNo": line_no,
    }
    resp = requests.patch(
        url,
        params=params,
        json=updates,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"ERP invoice line update failed: {(resp.text or '')[:200]}",
        )
    body = resp.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Invalid ERP invoice line update response")
    return body


def delete_erp_invoice_line(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int,
    line_no: int,
) -> Dict[str, Any]:
    """Delete one invoice line by composite key (ERP DELETE /invoice-line/by-key)."""
    url = f"{_erp_base()}/invoice-line/by-key"
    params = {
        "tenantId": tenant_id,
        "companyId": company_id,
        "documentType": document_type,
        "invoiceNo": invoice_no,
        "versionNo": version_no,
        "lineNo": line_no,
    }
    resp = requests.delete(url, params=params, timeout=30)
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Invoice line not found")
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"ERP invoice line delete failed: {(resp.text or '')[:200]}",
        )
    if not (resp.text or "").strip():
        return {
            "status": "deleted",
            "invoiceNo": invoice_no,
            "lineNo": line_no,
        }
    try:
        body = resp.json()
    except ValueError:
        return {
            "status": "deleted",
            "invoiceNo": invoice_no,
            "lineNo": line_no,
        }
    if isinstance(body, dict):
        return body
    return {
        "status": "deleted",
        "invoiceNo": invoice_no,
        "lineNo": line_no,
    }


def extract_match_review(line: Dict[str, Any]) -> Dict[str, Any]:
    fp = line.get("itemFingerprint") or line.get("item_fingerprint")
    if isinstance(fp, dict):
        review = fp.get("matchReview")
        if isinstance(review, dict):
            return review
    return {}


def build_invoice_line_review_response(line: Dict[str, Any]) -> Dict[str, Any]:
    review = extract_match_review(line)
    return {
        "lineId": line.get("lineId"),
        "lineNo": line.get("lineNo"),
        "description": line.get("description"),
        "itemId": line.get("itemId"),
        "poLineNo": line.get("poLineNo"),
        "uom": line.get("uom"),
        "lineStatus": line.get("lineStatus"),
        "itemFingerprint": line.get("itemFingerprint"),
        "matchReview": review,
    }


def get_invoice_match_review(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int,
    line_no: Optional[int] = None,
) -> Dict[str, Any]:
    if line_no is not None:
        line = get_erp_invoice_line(
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=document_type,
            invoice_no=invoice_no,
            version_no=version_no,
            line_no=line_no,
        )
        return {
            "invoiceNo": invoice_no,
            "versionNo": version_no,
            "lines": [build_invoice_line_review_response(line)],
        }

    lines = list_erp_invoice_lines_for_header(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=invoice_no,
        version_no=version_no,
    )
    lines.sort(key=lambda row: int(row.get("lineNo") or 0))
    return {
        "invoiceNo": invoice_no,
        "versionNo": version_no,
        "lines": [build_invoice_line_review_response(line) for line in lines],
    }


def confirm_invoice_line_match(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int,
    line_no: int,
    selected_po_line_no: int,
    selected_item_id: Optional[int] = None,
    selected_product_fingerprint: Optional[Dict[str, Any]] = None,
    create_alias: bool = True,
) -> Dict[str, Any]:
    """
    User confirms invoice line product mapping.

    Updates ERP invoice_line (itemId, poLineNo, itemFingerprint, lineStatus=CONFIRMED).
    When the user corrected the mapping, stores a new PRODUCT_NAME alias.
    """
    _ = db
    logger.info(
        "[INVOICE MATCH] step=confirm_start invoice_no=%s line_no=%s "
        "selected_po_line_no=%s selected_item_id=%s",
        invoice_no,
        line_no,
        selected_po_line_no,
        selected_item_id,
    )
    current = get_erp_invoice_line(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=invoice_no,
        version_no=version_no,
        line_no=line_no,
    )
    review = extract_match_review(current)
    product_name = (
        _clean(review.get("productName"))
        or _clean(current.get("description"))
        or ""
    )
    match_type = _clean(review.get("matchType")) or "manual"
    logger.info(
        "[INVOICE MATCH] step=confirm_loaded invoice_no=%s line_no=%s "
        "product_name=%r prior_match_type=%s prior_status=%s",
        invoice_no,
        line_no,
        product_name,
        match_type,
        current.get("lineStatus") or current.get("line_status"),
    )

    suggested = review.get("suggested") if isinstance(review.get("suggested"), dict) else {}
    suggested_po_line_no = suggested.get("poLineNo")
    suggested_item_id = suggested.get("itemId")

    selected_option: Optional[Dict[str, Any]] = None
    for bucket_key in ("rankedCandidates", "allPoLines"):
        options = review.get(bucket_key)
        if not isinstance(options, list):
            continue
        for opt in options:
            if not isinstance(opt, dict):
                continue
            if int(opt.get("poLineNo") or 0) == int(selected_po_line_no):
                selected_option = opt
                break
        if selected_option is not None:
            break

    resolved_item_id = (
        selected_item_id
        if selected_item_id is not None
        else (
            selected_option.get("itemId")
            if selected_option
            else suggested_item_id
        )
    )
    resolved_fingerprint = (
        selected_product_fingerprint
        if isinstance(selected_product_fingerprint, dict)
        else (
            selected_option.get("productFingerprint")
            if selected_option and isinstance(selected_option.get("productFingerprint"), dict)
            else current.get("itemFingerprint")
        )
    )
    canonical_name = (
        _clean(selected_option.get("productName") if selected_option else None)
        or _clean(selected_option.get("description") if selected_option else None)
        or product_name
    )

    user_corrected = (
        match_type in ("manual", "multiple")
        or (
            suggested_po_line_no is not None
            and int(suggested_po_line_no) != int(selected_po_line_no)
        )
        or (
            suggested_item_id is not None
            and resolved_item_id is not None
            and int(suggested_item_id) != int(resolved_item_id)
        )
    )

    alias_created = None
    if create_alias and user_corrected and product_name and canonical_name and resolved_item_id:
        logger.info(
            "[INVOICE MATCH] step=confirm_create_alias invoice_no=%s line_no=%s "
            "alias=%r canonical=%r item_id=%s",
            invoice_no,
            line_no,
            product_name,
            canonical_name,
            resolved_item_id,
        )
        alias_created = create_product_name_alias(
            alias_value=product_name,
            canonical_value=canonical_name,
            item_id=int(resolved_item_id),
            remarks=(
                f"Invoice {invoice_no} line {line_no} user confirmation "
                f"({match_type})"
            ),
        )

    confirmed_fp = build_item_fingerprint_with_match_review(
        resolved_fingerprint if isinstance(resolved_fingerprint, dict) else {},
        product_name=product_name,
        match_type=match_type,
        line_status=LINE_STATUS_CONFIRMED,
        suggested={
            **(suggested or {}),
            "confirmedPoLineNo": selected_po_line_no,
            "confirmedItemId": resolved_item_id,
        },
        ranked_candidates=review.get("rankedCandidates")
        if isinstance(review.get("rankedCandidates"), list)
        else None,
        all_po_lines=review.get("allPoLines")
        if isinstance(review.get("allPoLines"), list)
        else None,
        extra={
            "confirmedAt": "user",
            "userCorrected": user_corrected,
            "aliasCreated": alias_created is not None,
        },
    )

    updates: Dict[str, Any] = {
        "poLineNo": int(selected_po_line_no),
        "lineStatus": LINE_STATUS_CONFIRMED,
        "itemFingerprint": confirmed_fp,
    }
    if resolved_item_id is not None:
        updates["itemId"] = int(resolved_item_id)

    logger.info(
        "[INVOICE MATCH] step=confirm_patch_erp invoice_no=%s line_no=%s "
        "item_id=%s po_line_no=%s user_corrected=%s",
        invoice_no,
        line_no,
        resolved_item_id,
        selected_po_line_no,
        user_corrected,
    )
    updated = patch_erp_invoice_line(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=invoice_no,
        version_no=version_no,
        line_no=line_no,
        updates=updates,
    )

    logger.info(
        "[INVOICE MATCH] step=confirm_done invoice_no=%s line_no=%s "
        "line_status=%s alias_created=%s",
        invoice_no,
        line_no,
        LINE_STATUS_CONFIRMED,
        alias_created is not None,
    )
    return {
        "status": "confirmed",
        "lineStatus": LINE_STATUS_CONFIRMED,
        "invoiceLine": updated,
        "userCorrected": user_corrected,
        "aliasCreated": alias_created,
    }
