"""Serve uploaded invoice documents stored during OCR import."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from fastapi import HTTPException, status

from config import settings
from services.vendor_document_storage import resolve_document_file_path

logger = logging.getLogger(__name__)


def _internal_api_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_API_URL is not configured",
        )
    return base


def _pick_str(row: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def fetch_invoice_header(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int,
) -> Dict[str, Any]:
    url = f"{_internal_api_base()}/invoice-header/by-key"
    params = {
        "tenantId": tenant_id,
        "companyId": company_id,
        "documentType": document_type,
        "invoiceNo": invoice_no,
        "versionNo": version_no,
    }
    try:
        response = requests.get(url, params=params, timeout=30)
    except requests.RequestException as exc:
        logger.warning("[INVOICE-ATTACHMENT] ERP lookup failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not look up invoice attachment",
        ) from exc

    if response.status_code == 404:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invoice not found",
        )
    if response.status_code >= 400:
        detail: Optional[str]
        try:
            body = response.json()
            detail = (
                body.get("message")
                if isinstance(body, dict)
                else None
            ) or str(body)
        except ValueError:
            detail = response.text
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail or "Invoice lookup failed",
        )

    data = response.json()
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected invoice lookup response",
        )
    return data


def media_type_for_attachment(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".png":
        return "image/png"
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


def resolve_invoice_attachment_file(
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    version_no: int,
) -> Tuple[Path, str]:
    header = fetch_invoice_header(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        invoice_no=invoice_no,
        version_no=version_no,
    )
    attachment_path = _pick_str(header, "attachmentPath", "attachment_path")
    if not attachment_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No uploaded document for this invoice",
        )

    try:
        file_path = resolve_document_file_path(attachment_path)
    except FileNotFoundError as exc:
        # Legacy PO-style absolute / cwd-relative paths.
        candidate = Path(attachment_path)
        if not candidate.is_absolute():
            candidate = Path.cwd() / attachment_path
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Uploaded document file is missing on the server",
            ) from exc
        file_path = candidate

    display_name = _pick_str(header, "invoiceNo", "invoice_no") or file_path.name
    suffix = file_path.suffix or ".pdf"
    if not display_name.lower().endswith(suffix.lower()):
        display_name = f"{display_name}{suffix}"
    return file_path, display_name
