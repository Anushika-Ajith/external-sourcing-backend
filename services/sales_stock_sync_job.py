"""Background stock trend synchronization after sales upload."""

from __future__ import annotations

import logging
from typing import List, Optional

from database import SessionLocal
from services.sales_upload_perf import SalesUploadPerf
from services.stock_suggested_qty_from_sales import apply_sales_trend_analysis_to_stock

logger = logging.getLogger(__name__)


def run_sales_stock_sync_job(
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    product_ids: List[int],
    updated_by: Optional[int] = None,
) -> None:
    """
    Retryable stock sync in its own DB session.

    Sales transactions must already be committed before this runs.
    """
    if not product_ids:
        logger.info("Sales stock sync skipped: no affected product IDs")
        return

    perf = SalesUploadPerf(label="Sales Stock Sync (background)")
    db = SessionLocal()
    try:
        with perf.stage("stock_analysis"):
            result = apply_sales_trend_analysis_to_stock(
                db,
                tenant_id=tenant_id,
                company_id=company_id,
                updated_by=updated_by,
                product_ids=product_ids,
            )
        with perf.stage("commit"):
            db.commit()
        perf.log_summary(
            extra={
                "product_ids": len(product_ids),
                "stock_updated": result.get("stock_updated"),
            }
        )
    except Exception:
        db.rollback()
        logger.exception(
            "Sales stock sync failed tenant=%s company=%s products=%s",
            tenant_id,
            company_id,
            len(product_ids),
        )
    finally:
        db.close()
