"""Resolve document pickup folder from the Foundation/NestJS API."""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from config import settings

logger = logging.getLogger(__name__)


def _extract_file_path(payload: Any) -> Optional[str]:
    if isinstance(payload, str):
        text = payload.strip()
        return text if text else None

    if not isinstance(payload, dict):
        return None

    for key in (
        "filePath",
        "file_path",
        "path",
        "folderPath",
        "folder_path",
        "documentPath",
        "document_path",
        "directory",
        "folder",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for nested_key in ("data", "result", "payload"):
        nested = payload.get(nested_key)
        found = _extract_file_path(nested)
        if found:
            return found

    return None


def fetch_document_type_file_path(
    *,
    name: Optional[str] = None,
    process_id: Optional[int] = None,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    timeout_seconds: Optional[float] = None,
) -> str:
    resolved_timeout = (
        float(timeout_seconds)
        if timeout_seconds is not None
        else float(settings.document_pickup_api_timeout_seconds)
    )
    doc_name = (name or settings.document_pickup_document_type_name or "").strip()
    if not doc_name:
        raise ValueError("Document type name is required for file-path lookup")

    resolved_process_id = int(
        process_id if process_id is not None else settings.document_pickup_process_id
    )
    resolved_tenant_id = int(
        tenant_id if tenant_id is not None else settings.document_pickup_tenant_id
    )
    resolved_company_id = int(
        company_id if company_id is not None else settings.document_pickup_company_id
    )

    base = (settings.nestjs_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("NESTJS_API_URL is not configured")
    url = f"{base}/document-type/file-path"
    params = {
        "name": doc_name,
        "processId": resolved_process_id,
        "tenantId": resolved_tenant_id,
        "companyId": resolved_company_id,
    }

    logger.info(
        "[folder-pickup] Resolving document folder via GET %s params=%s",
        url,
        params,
    )

    try:
        response = requests.get(url, params=params, timeout=resolved_timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed to fetch document file path from {url}: {exc}"
        ) from exc

    try:
        body: Any = response.json()
    except ValueError:
        body = response.text

    path = _extract_file_path(body)
    if not path:
        raise RuntimeError(
            f"Document file-path API returned no usable path: {body!r}"
        )

    logger.info("[folder-pickup] Resolved document folder: %s", path)
    return path


def resolve_document_upload_folder() -> Optional[str]:
    """
    Resolve pickup inbox path.

    Priority:
    1. DOCUMENT_UPLOAD_FOLDER env override (local/dev)
    2. Foundation API GET /document-type/file-path
    """
    override = (settings.document_upload_folder or "").strip()
    if override:
        logger.info(
            "[folder-pickup] Using DOCUMENT_UPLOAD_FOLDER override: %s",
            override,
        )
        return override

    return fetch_document_type_file_path()
