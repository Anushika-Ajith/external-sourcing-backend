"""HTTP client for the standalone Document OCR microservice."""

from __future__ import annotations

import mimetypes
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException, status

from config import settings

PERF_PREFIX = "[SALES-UPLOAD-PERF]"

logger = logging.getLogger(__name__)


def _log_http_timing(label: str, url: str, elapsed: float, *, files: int) -> None:
    line = f"{PERF_PREFIX} OCR HTTP {label}: {elapsed:.3f}s url={url} files={files}"
    print(line, flush=True)
    logger.info(line)


def _base_url() -> str:
    return settings.ocr_service_url.rstrip("/")


def _headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    key = (settings.ocr_service_api_key or "").strip()
    if key:
        headers["X-OCR-Service-Key"] = key
    return headers


def _raise_for_response(resp: requests.Response) -> Dict[str, Any]:
    if resp.ok:
        return resp.json()
    detail: Any
    try:
        body = resp.json()
        detail = body.get("detail", body)
    except Exception:
        detail = resp.text or resp.reason
    raise HTTPException(status_code=resp.status_code, detail=detail)


def extract_from_paths(
    file_paths: List[str],
    *,
    perf_label: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], Any]:
    """
    Call OCR microservice POST /ocr/extract.

    Returns:
        combined_text (empty — not returned by service), classification, raw_text
    """
    url = f"{_base_url()}/ocr/extract"
    files: List[tuple] = []
    handles = []
    label = perf_label or "POST /ocr/extract"
    t0 = time.perf_counter()
    try:
        for path in file_paths:
            p = Path(path)
            mime, _ = mimetypes.guess_type(str(p))
            fh = open(p, "rb")
            handles.append(fh)
            files.append(("files", (p.name, fh, mime or "application/octet-stream")))
        resp = requests.post(
            url,
            files=files,
            headers=_headers(),
            timeout=settings.ocr_service_timeout_seconds,
        )
    except requests.RequestException as exc:
        _log_http_timing(label, url, time.perf_counter() - t0, files=len(file_paths))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"OCR service unavailable at {url}: {exc}",
        ) from exc
    finally:
        for fh in handles:
            fh.close()

    _log_http_timing(label, url, time.perf_counter() - t0, files=len(file_paths))
    data = _raise_for_response(resp)
    combined_text = str(data.get("combined_text") or "")
    return (
        combined_text,
        data.get("document_classification") or {},
        data.get("raw_text"),
    )


def extract_sales_from_paths(
    file_paths: List[str],
    *,
    perf_label: Optional[str] = None,
) -> Tuple[str, Dict[str, Any], Any]:
    """
    Call OCR microservice POST /ocr/sales/extract (sales-only structured extraction).

    Returns:
        combined_text, classification, raw_text with sales.lines
    """
    url = f"{_base_url()}/ocr/sales/extract"
    files: List[tuple] = []
    handles = []
    label = perf_label or "POST /ocr/sales/extract"
    t0 = time.perf_counter()
    try:
        for path in file_paths:
            p = Path(path)
            mime, _ = mimetypes.guess_type(str(p))
            fh = open(p, "rb")
            handles.append(fh)
            files.append(("files", (p.name, fh, mime or "application/octet-stream")))
        resp = requests.post(
            url,
            files=files,
            headers=_headers(),
            timeout=settings.ocr_service_timeout_seconds,
        )
    except requests.RequestException as exc:
        _log_http_timing(label, url, time.perf_counter() - t0, files=len(file_paths))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"OCR service unavailable at {url}: {exc}",
        ) from exc
    finally:
        for fh in handles:
            fh.close()

    _log_http_timing(label, url, time.perf_counter() - t0, files=len(file_paths))
    data = _raise_for_response(resp)
    combined_text = str(data.get("combined_text") or "")
    raw_text = data.get("raw_text") or {"sales": data.get("sales") or {}}
    return (
        combined_text,
        data.get("document_classification") or {},
        raw_text,
    )


def extract_purchase_order_from_paths(file_paths: List[str]) -> Dict[str, Any]:
    """Call OCR microservice POST /ocr/purchase-order/extract."""
    if len(file_paths) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Purchase order extraction requires exactly one file",
        )
    url = f"{_base_url()}/ocr/purchase-order/extract"
    path = Path(file_paths[0])
    mime, _ = mimetypes.guess_type(str(path))
    try:
        with open(path, "rb") as fh:
            resp = requests.post(
                url,
                files={"file": (path.name, fh, mime or "application/octet-stream")},
                headers=_headers(),
                timeout=settings.ocr_service_timeout_seconds,
            )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"OCR service unavailable at {url}: {exc}",
        ) from exc

    return _raise_for_response(resp)
