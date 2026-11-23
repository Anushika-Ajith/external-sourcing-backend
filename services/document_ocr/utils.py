"""Shared helpers for the document OCR service."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def make_json_serializable(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: make_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_serializable(i) for i in obj]
    return obj


def cleanup_job_dir(job_dir: Path, saved_files: List[str]) -> None:
    for path in saved_files:
        if os.path.exists(path):
            os.remove(path)
    if job_dir.exists():
        try:
            job_dir.rmdir()
        except OSError:
            pass


def build_ocr_serializable(
    raw_text: Any,
    document_classification: Any,
) -> Dict[str, Any]:
    ocr_serializable = make_json_serializable(raw_text)
    classification_serializable = make_json_serializable(document_classification)
    if isinstance(ocr_serializable, dict):
        return {
            **ocr_serializable,
            "document_classification": classification_serializable,
        }
    return {
        "vendor_extraction": ocr_serializable,
        "document_classification": classification_serializable,
    }


def classified_document_type(document_classification: Optional[Dict[str, Any]]) -> str:
    return str((document_classification or {}).get("document_type") or "").strip().lower()


def classification_for_file_index(
    document_classification: Optional[Dict[str, Any]],
    file_index: int,
) -> Optional[Dict[str, Any]]:
    """Return per-file classification when a multi-file bundle was processed."""
    dc = document_classification or {}
    per_file = dc.get("per_file")
    if isinstance(per_file, list) and 0 <= file_index < len(per_file):
        row = per_file[file_index]
        return row if isinstance(row, dict) else None
    return dc if isinstance(dc, dict) else None


def classification_includes(
    document_classification: Optional[Dict[str, Any]],
    *document_types: str,
) -> bool:
    """True when any bundle file (or the primary label) matches one of the types."""
    wanted = {str(t or "").strip().lower() for t in document_types if str(t or "").strip()}
    if not wanted:
        return False

    dc = document_classification or {}
    primary = str(dc.get("document_type") or "").strip().lower()
    if primary in wanted:
        return True

    for row in dc.get("per_file") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("document_type") or "").strip().lower() in wanted:
            return True

    for row in dc.get("bundle_document_types") or []:
        if str(row or "").strip().lower() in wanted:
            return True

    return False
