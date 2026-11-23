"""Persist vendor upload files and normalize paths for DB + HTTP serving."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import settings

_UPLOAD_ROOT = Path(settings.upload_dir or "uploads").resolve()


def vendor_documents_dir(vendor_id: int) -> Path:
    return _UPLOAD_ROOT / "vendor_documents" / str(vendor_id)


def to_public_document_path(absolute_or_relative: str) -> str:
    """Store a stable relative path (uploads/...) for DB and /uploads static mount."""
    raw = (absolute_or_relative or "").strip().replace("\\", "/")
    if not raw:
        return ""
    idx = raw.lower().find("uploads/")
    if idx >= 0:
        return raw[idx:]
    root_name = _UPLOAD_ROOT.name
    if raw.startswith(f"{root_name}/"):
        return raw
    return f"{root_name}/{raw.lstrip('/')}"


def resolve_document_file_path(document_path: str) -> Path:
    """Resolve a stored document_path to an on-disk file."""
    public = to_public_document_path(document_path)
    if not public:
        raise FileNotFoundError("empty document_path")
    if public.startswith("uploads/"):
        rel = public[len("uploads/") :]
        candidate = _UPLOAD_ROOT / rel
    else:
        candidate = Path(public)
    if not candidate.is_absolute():
        candidate = (_UPLOAD_ROOT.parent / candidate).resolve()
    if not candidate.is_file():
        # Legacy rows may store paths relative to backend cwd without uploads/ prefix
        legacy = Path(document_path.replace("\\", "/"))
        if legacy.is_file():
            return legacy.resolve()
        cwd_legacy = Path.cwd() / document_path.replace("\\", "/")
        if cwd_legacy.is_file():
            return cwd_legacy.resolve()
        raise FileNotFoundError(document_path)
    return candidate


def infer_document_type(
    filename: Optional[str],
    ocr_row: Optional[Dict[str, Any]] = None,
    classification: Optional[Dict[str, Any]] = None,
) -> str:
    if ocr_row:
        dtype = str(ocr_row.get("document_type") or "").strip()
        if dtype:
            normalized = dtype.lower()
            if "gst" in normalized:
                return "GST"
            if "pan" in normalized:
                return "PAN"
            if "msme" in normalized:
                return "MSME"
            if "registration" in normalized:
                return "Registration Certificate"
            if len(dtype) <= 100:
                return dtype[:100]

    hay = (filename or "").lower()
    if "rate contract" in hay or "rate_contract" in hay.replace(" ", ""):
        return "Rate Contract"
    if "bank" in hay and any(token in hay for token in ("detail", "form", "account")):
        return "Bank Details"
    if "checklist" in hay:
        return "Checklist"
    if "gst" in hay:
        return "GST"
    if "pan" in hay:
        return "PAN"
    if "msme" in hay:
        return "MSME"
    if "registration" in hay or "incorporation" in hay:
        return "Registration Certificate"

    if classification:
        ctype = str(classification.get("document_type") or "").strip().lower()
        if "gst" in ctype:
            return "GST"
        if "pan" in ctype:
            return "PAN"
        if "msme" in ctype:
            return "MSME"
        if "registration" in ctype:
            return "Registration Certificate"
        if "rate contract" in ctype or "schedule of rates" in ctype:
            return "Other"

    return "Other"


def _safe_filename(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    base = os.path.basename(name.replace("\\", "/"))
    return base or None


def persist_vendor_upload_files(
    vendor_id: int,
    source_paths: Sequence[str],
    original_filenames: Optional[Sequence[str]] = None,
) -> Tuple[List[str], List[str]]:
    """
    Copy temp OCR uploads into uploads/vendor_documents/{vendor_id}/.

    Returns (public_paths_for_db, original_filenames_aligned).
    """
    dest_dir = vendor_documents_dir(vendor_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    names = list(original_filenames or [])
    public_paths: List[str] = []
    aligned_names: List[str] = []

    for idx, src in enumerate(source_paths):
        if not src or not os.path.isfile(src):
            continue
        ext = os.path.splitext(src)[1] or os.path.splitext(
            names[idx] if idx < len(names) else ""
        )[1]
        dest = dest_dir / f"{uuid.uuid4().hex}{ext}"
        shutil.copy2(src, dest)
        public_paths.append(to_public_document_path(str(dest)))
        aligned_names.append(
            _safe_filename(names[idx] if idx < len(names) else None)
            or _safe_filename(src)
            or dest.name
        )

    return public_paths, aligned_names
