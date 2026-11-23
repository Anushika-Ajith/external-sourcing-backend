"""Sales document OCR / CSV import into sales_transactions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from config import settings
from services.document_ocr.utils import (
    classified_document_type,
    make_json_serializable,
)
from services.ocr_sales_import import (
    collect_lines_from_files,
    extract_sales_lines_from_payload,
    insert_sales_transaction_lines,
    is_csv_path,
    is_pdf_path,
    source_has_product_id_column,
)
from services.sales_ocr_pipeline import run_sales_ocr_pipeline
from services.sales_upload_perf import SalesUploadPerf
from services.stock_suggested_qty_from_sales import apply_sales_trend_analysis_to_stock


def process_sales_documents(
    db: Session,
    *,
    saved_files: List[str],
    received_files: List[Dict[str, str]],
    tenant_id: int = 1,
    company_id: int = 3,
    created_by: Optional[int] = None,
    created_ip: Optional[str] = None,
    defer_stock_sync: Optional[bool] = None,
    perf: Optional[SalesUploadPerf] = None,
) -> Dict[str, Any]:
    """
    Import sales lines from CSV and/or scanned documents (PDF/images via OCR).

    When defer_stock_sync is True (default from SALES_STOCK_SYNC_ASYNC), stock trend
    synchronization is skipped so the API can return after sales commit.
    """
    timer = perf or SalesUploadPerf()
    defer_stock_sync = (
        settings.sales_stock_sync_async
        if defer_stock_sync is None
        else defer_stock_sync
    )

    document_classification: Any = None
    raw_text: Any = None
    source_parts: List[str] = []

    lines, scan_files = collect_lines_from_files(saved_files, perf=timer)

    if lines:
        if any(is_csv_path(p) for p in saved_files):
            source_parts.append("csv")
        elif any(is_pdf_path(p) for p in saved_files):
            source_parts.append("pdf")

    if scan_files:
        _, document_classification, raw_text = run_sales_ocr_pipeline(
            scan_files,
            perf=timer,
        )
        source_parts.append("ocr")
        with timer.stage("extract_sales_lines"):
            if isinstance(raw_text, dict):
                classified_type = classified_document_type(
                    document_classification
                    if isinstance(document_classification, dict)
                    else None
                )
                if classified_type in ("sales report", "sales") or isinstance(
                    raw_text.get("sales"), dict
                ):
                    lines.extend(extract_sales_lines_from_payload(raw_text))
                elif not lines:
                    lines.extend(extract_sales_lines_from_payload(raw_text))

    use_product_matching = not source_has_product_id_column(saved_files)

    import_result = insert_sales_transaction_lines(
        db,
        lines,
        tenant_id=tenant_id,
        company_id=company_id,
        created_by=created_by,
        created_ip=created_ip,
        use_product_matching=use_product_matching,
        perf=timer,
    )

    affected_product_ids = import_result.get("affected_product_ids") or []

    with timer.stage("commit"):
        db.commit()

    stock_trend_sync: Dict[str, Any] = {
        "stock_updated": 0,
        "unmatched_count": 0,
        "trends_analyzed": 0,
        "adc_products": 0,
    }

    if defer_stock_sync:
        if affected_product_ids:
            stock_trend_sync = {
                "status": "queued",
                "affected_products": len(affected_product_ids),
                "stock_updated": 0,
                "unmatched_count": 0,
                "trends_analyzed": 0,
                "adc_products": 0,
            }
    elif affected_product_ids or (import_result.get("inserted") or 0) > 0:
        try:
            with timer.stage("stock_analysis"):
                stock_trend_sync = apply_sales_trend_analysis_to_stock(
                    db,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    updated_by=created_by,
                    product_ids=affected_product_ids or None,
                )
            with timer.stage("stock_commit"):
                db.commit()
        except Exception as exc:
            db.rollback()
            stock_trend_sync = {
                "stock_updated": 0,
                "error": str(exc),
            }

    timer.log_summary(
        extra={
            "source": "+".join(source_parts) if source_parts else "none",
            "inserted": import_result.get("inserted"),
            "defer_stock_sync": defer_stock_sync,
            "endpoint": "POST /ocr/sales" if perf is not None else None,
        }
    )

    return {
        "status": "completed",
        "files_received": received_files,
        "source": "+".join(source_parts) if source_parts else "none",
        "document_classification": make_json_serializable(document_classification),
        "sales_import": import_result,
        "stock_trend_sync": make_json_serializable(stock_trend_sync),
        "raw_text": make_json_serializable(raw_text),
        "affected_product_ids": affected_product_ids,
        "performance": timer.as_dict(),
    }
