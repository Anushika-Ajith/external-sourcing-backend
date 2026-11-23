"""File upload handling for OCR jobs."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from fastapi import HTTPException, UploadFile, status

from services.document_ocr.constants import ALLOWED_OCR_MIME_TYPES, OCR_UPLOAD_DIR


async def save_upload_files(
    files: List[UploadFile],
    *,
    upload_dir: Path = OCR_UPLOAD_DIR,
    allowed_mime_types: Optional[Set[str]] = None,
) -> Tuple[str, Path, List[str], List[Dict[str, str]]]:
    allowed = allowed_mime_types or ALLOWED_OCR_MIME_TYPES
    job_id = str(uuid.uuid4())
    job_dir = upload_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: List[str] = []
    received: List[Dict[str, str]] = []

    for file in files:
        content_type = (file.content_type or "").lower()
        filename = (file.filename or "").lower()
        # Some browsers send empty/octet-stream for CSV/Excel — accept by extension.
        is_csv = filename.endswith(".csv") or content_type in {
            "text/csv",
            "application/csv",
            "application/vnd.ms-excel",
        }
        is_excel = filename.endswith(".xls") or filename.endswith(".xlsx")
        type_ok = content_type in allowed or (
            is_csv and any(t.endswith("csv") or "excel" in t for t in allowed)
        ) or (
            is_excel
            and any(
                "excel" in t or "spreadsheet" in t or t.endswith("sheet")
                for t in allowed
            )
        )
        if not type_ok:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported file type: {file.content_type}",
            )
        dest = job_dir / (file.filename or f"upload_{uuid.uuid4().hex}")
        with open(dest, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        saved_paths.append(str(dest))
        received.append(
            {
                "filename": file.filename or dest.name,
                "content_type": file.content_type or "",
            }
        )
    return job_id, job_dir, saved_paths, received
