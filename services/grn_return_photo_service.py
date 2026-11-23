"""Returned / damaged goods photos on a GRN line.

Photos are saved under ``uploads/grn_return_photos/...`` (served by the ``/uploads``
static mount). The text on the product label in each photo is read through the OCR
service, and the photo list is stored on ``grn_line.return_photos`` by the ERP
backend, which also rolls the label text up to ``grn_header.scanned_label``.
"""

from __future__ import annotations

import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests
from fastapi import HTTPException, status

from config import settings
from services.ocr_client import extract_from_paths
from services.vendor_document_storage import (
    resolve_document_file_path,
    to_public_document_path,
)

logger = logging.getLogger(__name__)

# SAMPLE = store-keeper count / label photos (batch + expiry capture).
RETURN_CONDITIONS = ("RETURNED", "DAMAGED", "SAMPLE")
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
MAX_PHOTOS_PER_UPLOAD = 10
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_LABEL_CHARS = 4000

_UPLOAD_ROOT = Path(settings.upload_dir or "uploads").resolve()


@dataclass(frozen=True)
class GrnLineKey:
    tenant_id: int
    company_id: int
    document_type: str
    grn_number: str
    version_no: int
    line_no: int

    def query_params(self) -> Dict[str, str]:
        return {
            "tenantId": str(self.tenant_id),
            "companyId": str(self.company_id),
            "documentType": self.document_type,
            "grnNumber": self.grn_number,
            "versionNo": str(self.version_no),
            "lineNo": str(self.line_no),
        }


@dataclass(frozen=True)
class IncomingPhoto:
    file_name: str
    content: bytes


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._") or "x"


def _photo_dir(key: GrnLineKey) -> Path:
    return (
        _UPLOAD_ROOT
        / "grn_return_photos"
        / str(key.tenant_id)
        / str(key.company_id)
        / _safe_segment(key.grn_number)
        / f"v{key.version_no}"
        / f"line{key.line_no}"
    )


def _erp_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_API_URL is not configured",
        )
    return base


def _erp_error(resp: requests.Response, fallback: str) -> HTTPException:
    try:
        body = resp.json()
        raw = body.get("message") or body.get("detail") or body.get("error") or body
        if isinstance(raw, list):
            parts = []
            for item in raw:
                if isinstance(item, dict):
                    parts.append(str(item.get("msg") or item.get("message") or item))
                else:
                    parts.append(str(item))
            detail = "; ".join(p for p in parts if p) or fallback
        elif isinstance(raw, dict):
            detail = str(raw.get("message") or raw.get("msg") or raw)[:400]
        else:
            detail = str(raw)[:400] if raw is not None else fallback
    except Exception:
        detail = (resp.text or "").strip()[:400] or fallback
    if not detail or detail == "{}":
        detail = f"{fallback} (HTTP {resp.status_code})"
    elif resp.status_code and fallback not in str(detail):
        detail = f"{fallback}: {detail}"
    code = resp.status_code if 400 <= resp.status_code < 500 else status.HTTP_502_BAD_GATEWAY
    return HTTPException(status_code=code, detail=detail)


def normalize_label_text(text: str) -> Optional[str]:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    joined = " / ".join(ln for ln in lines if ln)
    return joined[:MAX_LABEL_CHARS] or None


def read_label_text(path: Path) -> Optional[str]:
    """Text printed on the label in the photo; None when OCR fails or finds nothing."""
    try:
        combined_text, _, _ = extract_from_paths(
            [str(path)], perf_label="POST /ocr/extract (GRN return photo)"
        )
    except Exception as exc:
        logger.warning("[GRN-RETURN-PHOTO] label OCR failed for %s: %s", path.name, exc)
        return None
    return normalize_label_text(combined_text)


def validate_photos(photos: List[IncomingPhoto]) -> None:
    if not photos:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Attach at least one photo.")
    if len(photos) > MAX_PHOTOS_PER_UPLOAD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Attach at most {MAX_PHOTOS_PER_UPLOAD} photos at a time.",
        )
    for photo in photos:
        ext = Path(photo.file_name or "").suffix.lower()
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{photo.file_name or 'File'} is not a supported image (JPG, PNG, WEBP, HEIC).",
            )
        if not photo.content:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{photo.file_name} is empty.")
        if len(photo.content) > MAX_PHOTO_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{photo.file_name} is larger than 10 MB.",
            )


def add_return_photos(
    *,
    key: GrnLineKey,
    condition: str,
    photos: List[IncomingPhoto],
    remarks: Optional[str] = None,
    uploaded_by: Optional[int] = None,
    label_reader: Callable[[Path], Optional[str]] = read_label_text,
    http_post: Callable[..., requests.Response] = requests.post,
) -> Dict[str, Any]:
    condition_norm = (condition or "").strip().upper()
    if condition_norm not in RETURN_CONDITIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="condition must be RETURNED, DAMAGED, or SAMPLE",
        )
    validate_photos(photos)

    target = _photo_dir(key)
    target.mkdir(parents=True, exist_ok=True)
    saved: List[tuple[str, Path, str]] = []
    for photo in photos:
        photo_id = uuid.uuid4().hex
        ext = Path(photo.file_name).suffix.lower()
        path = target / f"{photo_id}{ext}"
        path.write_bytes(photo.content)
        saved.append((photo_id, path, photo.file_name))

    with ThreadPoolExecutor(max_workers=min(4, len(saved))) as pool:
        labels = list(pool.map(label_reader, [p for _, p, _ in saved]))

    uploaded_at = datetime.now(timezone.utc).isoformat()
    remarks_clean = (remarks or "").strip()[:500] or None
    entries = [
        {
            "id": photo_id,
            "path": to_public_document_path(str(path)),
            "fileName": file_name[:255],
            "condition": condition_norm,
            "labelText": label,
            "remarks": remarks_clean,
            "uploadedAt": uploaded_at,
            "uploadedBy": uploaded_by,
        }
        for (photo_id, path, file_name), label in zip(saved, labels)
    ]

    try:
        resp = http_post(
            f"{_erp_base()}/grn-line/return-photos",
            params=key.query_params(),
            json={"photos": entries},
            timeout=30,
        )
    except requests.RequestException as exc:
        _delete_files([p for _, p, _ in saved])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ERP service unavailable: {exc}",
        ) from exc
    if not resp.ok:
        _delete_files([p for _, p, _ in saved])
        raise _erp_error(resp, "Could not save photos on the GRN line")

    result = resp.json()
    result["added"] = entries
    return result


def remove_return_photo(
    *,
    key: GrnLineKey,
    photo_id: str,
    http_delete: Callable[..., requests.Response] = requests.delete,
) -> Dict[str, Any]:
    try:
        resp = http_delete(
            f"{_erp_base()}/grn-line/return-photos",
            params={**key.query_params(), "photoId": photo_id},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ERP service unavailable: {exc}",
        ) from exc
    if not resp.ok:
        raise _erp_error(resp, "Could not remove the photo")

    result = resp.json()
    removed_path = ((result.get("removed") or {}).get("path") or "").strip()
    if removed_path.startswith("uploads/grn_return_photos/"):
        try:
            _delete_files([resolve_document_file_path(removed_path)])
        except FileNotFoundError:
            pass
    return result


def _delete_files(paths: List[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[GRN-RETURN-PHOTO] could not delete %s: %s", path, exc)
