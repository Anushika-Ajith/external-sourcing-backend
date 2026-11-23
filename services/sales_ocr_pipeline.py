"""Sales-specific OCR orchestration: local parse first, OpenAI fallback."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status

from services.ocr_client import extract_from_paths, extract_sales_from_paths
from services.ocr_sales_import import (
    extract_sales_lines_from_payload,
    is_image_path,
    is_pdf_path,
    parse_sales_plaintext,
)
from services.pdf_inspection import (
    extract_pdf_with_targeted_ocr,
    ocr_image_file,
)
from services.sales_upload_perf import SalesUploadPerf, perf_stage

logger = logging.getLogger(__name__)


def _local_ocr_text(path: str) -> str:
    if is_pdf_path(path):
        return extract_pdf_with_targeted_ocr(path)
    if is_image_path(path):
        return ocr_image_file(path)
    return ""


def try_local_ocr_sales_lines(
    scan_files: List[str],
    *,
    perf: Optional[SalesUploadPerf] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Run local OCR + deterministic sales parsing.

    Returns (parsed lines, files still requiring OpenAI structured extraction).
    """
    from services.ocr_sales_import import parse_sales_pdf_fast

    lines: List[Dict[str, Any]] = []
    remaining: List[str] = []

    for path in scan_files:
        file_name = Path(path).name
        if is_pdf_path(path):
            with perf_stage(perf, f"local_ocr:{file_name}"):
                parsed = parse_sales_pdf_fast(
                    path,
                    perf=perf,
                    stage_prefix=f"pdf:{file_name}:",
                )
        else:
            with perf_stage(perf, f"local_ocr:{file_name}"):
                text = _local_ocr_text(path)
            with perf_stage(perf, f"deterministic_parse:{file_name}"):
                parsed = parse_sales_plaintext(text) if text.strip() else []

        if parsed:
            lines.extend(parsed)
            logger.info(
                "Local OCR parsed %s sales lines from %s",
                len(parsed),
                file_name,
            )
        else:
            remaining.append(path)

    return lines, remaining


def _call_ocr_service(
    remaining: List[str],
    *,
    perf: Optional[SalesUploadPerf] = None,
) -> Tuple[str, Dict[str, Any], Any]:
    file_names = ", ".join(Path(p).name for p in remaining)
    sales_label = f"ocr_http:sales_extract [{file_names}]"
    fallback_label = f"ocr_http:extract_fallback [{file_names}]"

    try:
        return extract_sales_from_paths(remaining, perf_label=sales_label)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
        logger.warning(
            "Sales OCR endpoint unavailable (%s); falling back to /ocr/extract",
            exc.detail,
        )
        return extract_from_paths(remaining, perf_label=fallback_label)
    except Exception as exc:
        logger.warning(
            "Sales OCR request failed (%s); falling back to /ocr/extract",
            exc,
        )
        return extract_from_paths(remaining, perf_label=fallback_label)


def run_sales_ocr_pipeline(
    scan_files: List[str],
    *,
    perf: Optional[SalesUploadPerf] = None,
) -> Tuple[str, Dict[str, Any], Any]:
    """
    OCR pipeline optimized for sales uploads.

    1. Local OCR + deterministic parser
    2. Sales-only OpenAI extraction for remaining files
    3. Full OCR microservice fallback if sales endpoint unavailable
    """
    local_lines, remaining = try_local_ocr_sales_lines(scan_files, perf=perf)
    if local_lines and not remaining:
        return "", {"document_type": "Sales Report", "confidence": 1.0}, {"sales": {"lines": local_lines}}

    combined_text = ""
    document_classification: Dict[str, Any] = {}
    raw_text: Any = {"sales": {"lines": local_lines}} if local_lines else {}

    if remaining:
        with perf_stage(perf, "ocr_http"):
            combined_text, document_classification, service_raw = _call_ocr_service(
                remaining,
                perf=perf,
            )

        with perf_stage(perf, "openai_extraction"):
            service_lines = extract_sales_lines_from_payload(
                service_raw if isinstance(service_raw, dict) else {}
            )

        if local_lines:
            if isinstance(raw_text, dict):
                raw_text.setdefault("sales", {}).setdefault("lines", [])
                raw_text["sales"]["lines"].extend(service_lines)
            else:
                raw_text = {"sales": {"lines": local_lines + service_lines}}
        else:
            raw_text = service_raw

        if not local_lines and combined_text.strip():
            with perf_stage(perf, "deterministic_parse:combined_text"):
                deterministic = parse_sales_plaintext(combined_text)
            if deterministic and isinstance(raw_text, dict):
                existing = extract_sales_lines_from_payload(raw_text)
                if len(deterministic) > len(existing):
                    raw_text = {"sales": {"lines": deterministic}}

    return combined_text, document_classification, raw_text
