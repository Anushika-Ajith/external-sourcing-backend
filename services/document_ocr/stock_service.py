"""Stock document OCR / CSV / Excel import into stock + upload header/detail."""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from services.document_ocr.pipeline import run_ocr_pipeline
from services.document_ocr.utils import (
    classified_document_type,
    make_json_serializable,
)
from services.ocr_stock_import import (
    collect_stock_lines_from_files,
    extract_stock_lines_from_payload,
    import_stock_lines,
    parse_stock_lines_from_text,
)

logger = logging.getLogger(__name__)

# Matches stock_upload_header.document_no Column(String(30)).
_DOCUMENT_NO_MAX_LEN = 30


def _document_no_from_files(received_files: List[Dict[str, str]]) -> str:
    """
    Build a unique document_no per upload within the 30-char DB limit.

    Previous logic truncated `base-hash` to 30 and often dropped the hash,
    so re-uploading the same long filename collided on uq_stock_upload_document_no.
    """
    if not received_files:
        base = "STOCK"
    else:
        name = (received_files[0].get("filename") or "STOCK").strip()
        stem = Path(name).stem or "STOCK"
        # Keep alphanumeric tokens only so length stays predictable.
        base = re.sub(r"[^A-Za-z0-9]+", "", stem).upper() or "STOCK"

    # Unique per upload; keep stamp at the end so truncation never drops it.
    stamp = uuid.uuid4().hex[:10]
    prefix = "STK-"
    budget = _DOCUMENT_NO_MAX_LEN - len(prefix) - len(stamp)
    if budget < 1:
        return f"{prefix}{stamp}"[-_DOCUMENT_NO_MAX_LEN:]
    return f"{prefix}{base[:budget]}{stamp}"


def process_stock_documents(
    db: Session,
    *,
    saved_files: List[str],
    received_files: List[Dict[str, str]],
    tenant_id: int = 1,
    company_id: int = 3,
    created_by: Optional[int] = None,
    document_no: Optional[str] = None,
    upload_source: str = "WEB",
) -> Dict[str, Any]:
    """
    Import stock lines from CSV/Excel and/or scanned documents (PDF/images via OCR).

    Writes:
      - stock_upload_header
      - stock_upload_detail (one line per product in the file)
      - stock master (upsert by product_code; available_stock overwritten)
    """
    lines, scan_files = collect_stock_lines_from_files(saved_files)
    document_classification: Any = None
    raw_text: Any = None
    source_parts: List[str] = []

    if lines:
        source_parts.append("tabular")

    if scan_files:
        combined_text, document_classification, raw_text = run_ocr_pipeline(scan_files)
        source_parts.append("ocr")
        ocr_lines: List[Dict[str, Any]] = []
        if isinstance(raw_text, dict):
            classified_type = classified_document_type(
                document_classification
                if isinstance(document_classification, dict)
                else None
            )
            items = raw_text.get("items") if isinstance(raw_text.get("items"), list) else []
            items_look_like_stock = any(
                isinstance(row, dict)
                and (
                    row.get("quantity_balance") is not None
                    or row.get("available_stock") is not None
                    or row.get("current_stock") is not None
                )
                for row in items
            )
            stock_like = classified_type in (
                "stock",
                "stock report",
                "inventory",
                "inventory report",
            ) or any(
                isinstance(raw_text.get(k), (dict, list))
                for k in ("stock", "inventory", "stock_report", "stock_lines", "lines")
            ) or items_look_like_stock
            if stock_like or not lines:
                ocr_lines = extract_stock_lines_from_payload(raw_text)

        # Plain-text table fallback. Prefer when it parses — ReportLab stock
        # PDFs often put Contract Qty into OCR items.quantity_balance while
        # Current Stock is only reliable from the text table.
        text_lines: List[Dict[str, Any]] = []
        if combined_text:
            text_lines = parse_stock_lines_from_text(str(combined_text))

        if text_lines:
            lines.extend(text_lines)
            source_parts.append("text-table")
        elif ocr_lines:
            lines.extend(ocr_lines)
    logger.info(
        "[STOCK OCR] extracted_lines=%s source=%s",
        len(lines),
        "+".join(source_parts) if source_parts else "none",
    )

    import_result = import_stock_lines(
        db,
        lines,
        tenant_id=tenant_id,
        company_id=company_id,
        document_no=document_no or _document_no_from_files(received_files),
        upload_source=upload_source,
        created_by=created_by,
    )
    logger.info(
        "[STOCK OCR] import document_no=%s created=%s updated=%s details=%s skipped=%s",
        import_result.get("document_no"),
        import_result.get("stock_created"),
        import_result.get("stock_updated"),
        import_result.get("inserted_details"),
        import_result.get("skipped"),
    )
    if import_result.get("skipped_reasons"):
        logger.info(
            "[STOCK OCR] skipped_reasons=%s",
            import_result.get("skipped_reasons")[:20],
        )
    db.commit()

    return {
        "status": "completed",
        "files_received": received_files,
        "source": "+".join(source_parts) if source_parts else "none",
        "document_classification": make_json_serializable(document_classification),
        "stock_import": import_result,
        "raw_text": make_json_serializable(raw_text),
    }
