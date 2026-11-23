"""Core OCR pipeline — delegates to the standalone OCR microservice."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from services.ocr_client import extract_from_paths


def run_ocr_pipeline(saved_paths: List[str]) -> Tuple[str, Dict[str, Any], Any]:
    return extract_from_paths(saved_paths)
