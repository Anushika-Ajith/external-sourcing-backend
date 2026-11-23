"""Extract-only OCR (no database persistence)."""

from __future__ import annotations

from typing import Any, Dict, List

from services.document_ocr.pipeline import run_ocr_pipeline
from services.document_ocr.utils import make_json_serializable


def extract_documents(saved_files: List[str]) -> Dict[str, Any]:
    combined_text, document_classification, raw_text = run_ocr_pipeline(saved_files)
    return {
        "status": "completed",
        "combined_text_length": len(combined_text or ""),
        # Needed by GRN label scan to recover barcode/expiry when structured
        # extract maps them into price-list fields or drops them.
        "combined_text": combined_text or "",
        "document_classification": make_json_serializable(document_classification),
        "raw_text": make_json_serializable(raw_text),
    }
