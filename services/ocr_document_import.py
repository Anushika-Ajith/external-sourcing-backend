"""
Insert vendor_documents rows from OCR `documents[]` and persisted upload paths.

Always registers **one row per uploaded file** so View Document works even when
Gemini omits documents[], returns duplicates, or only classifies items/rates.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models import VendorDocument
from services.vendor_document_storage import infer_document_type, to_public_document_path
from services.document_ocr.utils import classification_for_file_index


def _clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _clip(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    return s if len(s) <= max_len else s[:max_len]


def _parse_doc_date(val: Any) -> Optional[date]:
    if val is None or val == "":
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        x = val.strip()[:10]
        try:
            return date.fromisoformat(x)
        except ValueError:
            return None
    return None


def _path_for_index(idx: int, paths: List[str]) -> str:
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    return paths[min(idx, len(paths) - 1)]


def _path_exists(db: Session, vendor_id: int, document_path: str) -> bool:
    public = to_public_document_path(document_path)
    if not public:
        return False
    return (
        db.query(VendorDocument.id)
        .filter(
            VendorDocument.vendor_id == vendor_id,
            VendorDocument.document_path == public,
        )
        .first()
        is not None
    )


def insert_ocr_vendor_documents(
    db: Session,
    vendor_id: int,
    ocr_payload: Dict[str, Any],
    persisted_file_paths: List[str],
    uploaded_filenames: List[str],
    *,
    document_classification: Optional[Dict[str, Any]] = None,
) -> int:
    """
    Insert VendorDocument rows for every persisted upload.

    OCR metadata from documents[] is applied per file index when available.
    Returns number of rows inserted.
    """
    if not persisted_file_paths:
        return 0

    docs_in: List[Any] = ocr_payload.get("documents") or []
    inserted = 0
    used_paths: set[str] = set()

    if isinstance(docs_in, list) and docs_in:
        for idx, row in enumerate(docs_in):
            if not isinstance(row, dict):
                continue
            doc_path = to_public_document_path(_path_for_index(idx, persisted_file_paths))
            if not doc_path or doc_path in used_paths:
                continue

            dtype = infer_document_type(
                uploaded_filenames[idx] if idx < len(uploaded_filenames) else None,
                ocr_row=row,
                classification=classification_for_file_index(
                    document_classification, idx
                ),
            )
            dnum = _clip(_clean_str(row.get("document_number")), 100)
            if dnum:
                dup = (
                    db.query(VendorDocument.id)
                    .filter(
                        VendorDocument.vendor_id == vendor_id,
                        VendorDocument.document_type == dtype,
                        VendorDocument.document_number == dnum,
                    )
                    .first()
                )
                if dup is not None:
                    continue

            db_doc = VendorDocument(
                vendor_id=vendor_id,
                document_type=_clip(dtype, 100) or "Other",
                document_number=dnum
                or _clip(
                    _clean_str(
                        uploaded_filenames[idx] if idx < len(uploaded_filenames) else None
                    ),
                    100,
                ),
                issue_date=_parse_doc_date(row.get("issue_date")),
                expiry_date=_parse_doc_date(row.get("expiry_date")),
                document_path=_clip(doc_path, 255) or doc_path,
                status="pending",
            )
            db.add(db_doc)
            used_paths.add(doc_path)
            inserted += 1

    for idx, raw_path in enumerate(persisted_file_paths):
        doc_path = to_public_document_path(raw_path)
        if not doc_path or doc_path in used_paths or _path_exists(db, vendor_id, doc_path):
            continue

        fname = uploaded_filenames[idx] if idx < len(uploaded_filenames) else None
        ocr_row = (
            docs_in[idx]
            if isinstance(docs_in, list) and idx < len(docs_in) and isinstance(docs_in[idx], dict)
            else None
        )
        dtype = infer_document_type(
            fname,
            ocr_row=ocr_row,
            classification=classification_for_file_index(document_classification, idx),
        )

        db_doc = VendorDocument(
            vendor_id=vendor_id,
            document_type=_clip(dtype, 100) or "Other",
            document_number=_clip(_clean_str(fname), 100),
            issue_date=_parse_doc_date(ocr_row.get("issue_date")) if ocr_row else None,
            expiry_date=_parse_doc_date(ocr_row.get("expiry_date")) if ocr_row else None,
            document_path=_clip(doc_path, 255) or doc_path,
            status="pending",
        )
        db.add(db_doc)
        inserted += 1

    if inserted:
        db.flush()
    return inserted
