"""
After sales uploads, run trend / ADC analysis and write results onto stock master.

suggested_qty = trend_qty + reorder_qty

trend_qty prefers ADC demand-lift suggested_qty when available, else last_week_sales
from weekly sales trend aggregation.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Stock
from services.sales_transaction_adc import detect_sales_adc_spikes
from services.sales_transaction_trends import list_sales_trends_by_product

logger = logging.getLogger(__name__)


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _stock_name_matches_sales_name(stock_name: str, sales_name: str) -> bool:
    """
    Match stock master names to sales line names.

    Handles e.g. stock "Britannia" ↔ sales "Britannia 100 g".
    """
    stock = (stock_name or "").strip().lower()
    sales = (sales_name or "").strip().lower()
    if not stock or not sales:
        return False
    if stock == sales:
        return True
    if sales.startswith(stock + " ") or sales.startswith(stock):
        return True
    if stock.startswith(sales + " ") or stock.startswith(sales):
        return True
    stock_token = stock.split()[0]
    sales_token = sales.split()[0]
    return len(stock_token) >= 4 and stock_token == sales_token


def _product_code_candidates(product_id: int) -> List[str]:
    pid = int(product_id)
    return [
        str(pid),
        f"ITM{pid}",
        f"ITM-{pid}",
        f"itm{pid}",
        f"itm-{pid}",
        f"SKU{pid}",
        f"SKU-{pid}",
    ]


def _find_stock_rows_bulk(
    db: Session,
    *,
    product_trends: List[Dict[str, Any]],
    tenant_id: Optional[int],
    company_id: Optional[int],
) -> Dict[int, Stock]:
    """Resolve stock rows for many products with bulk queries."""
    resolved: Dict[int, Stock] = {}
    if not product_trends:
        return resolved

    code_to_product_ids: Dict[str, List[int]] = defaultdict(list)
    all_codes: List[str] = []
    for trend in product_trends:
        product_id = int(trend["product_id"])
        for code in _product_code_candidates(product_id):
            all_codes.append(code)
            code_to_product_ids[code].append(product_id)

    q = db.query(Stock)
    if tenant_id is not None:
        q = q.filter(Stock.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(Stock.company_id == company_id)

    if all_codes:
        for stock in (
            q.filter(Stock.product_code.in_(all_codes))
            .order_by(Stock.stock_id.asc())
            .all()
        ):
            for product_id in code_to_product_ids.get(stock.product_code, []):
                if product_id not in resolved:
                    resolved[product_id] = stock

    names_by_product: Dict[int, str] = {}
    for trend in product_trends:
        product_id = int(trend["product_id"])
        if product_id in resolved:
            continue
        name = str(trend.get("product_name") or "").strip()
        if name:
            names_by_product[product_id] = name

    if names_by_product:
        name_set = {n.lower() for n in names_by_product.values()}
        for stock in (
            q.filter(func.lower(Stock.product_name).in_(name_set))
            .order_by(Stock.stock_id.asc())
            .all()
        ):
            stock_name = (stock.product_name or "").strip()
            for product_id, name in names_by_product.items():
                if product_id not in resolved and _stock_name_matches_sales_name(
                    stock_name, name
                ):
                    resolved[product_id] = stock

        still_unresolved = {
            pid: name
            for pid, name in names_by_product.items()
            if pid not in resolved
        }
        if still_unresolved:
            tenant_stocks = q.order_by(Stock.stock_id.asc()).all()
            for product_id, sales_name in still_unresolved.items():
                for stock in tenant_stocks:
                    if _stock_name_matches_sales_name(stock.product_name or "", sales_name):
                        resolved[product_id] = stock
                        break

    return resolved


def apply_sales_trend_analysis_to_stock(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    updated_by: Optional[int] = None,
    product_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Run weekly trend + ADC analysis on sales_transactions and update matching stock rows.

    When product_ids is provided, only those products are analyzed and updated.
    """
    if product_ids is not None and not product_ids:
        return {
            "stock_updated": 0,
            "unmatched": [],
            "unmatched_count": 0,
            "trends_analyzed": 0,
            "adc_products": 0,
        }

    scoped_product_ids = list(dict.fromkeys(int(pid) for pid in (product_ids or [])))
    trend_limit = len(scoped_product_ids) if scoped_product_ids else 10_000

    trends = list_sales_trends_by_product(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        product_ids=scoped_product_ids or None,
        skip=0,
        limit=trend_limit,
    )

    adc_items = detect_sales_adc_spikes(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        product_ids=scoped_product_ids or None,
        min_growth_pct=0.0,
        only_huge=False,
        only_sustained=False,
        limit=trend_limit,
    )
    adc_qty_by_product = {
        int(item.product_id): float(item.suggested_qty or 0)
        for item in adc_items
    }

    stock_by_product = _find_stock_rows_bulk(
        db,
        product_trends=trends,
        tenant_id=tenant_id,
        company_id=company_id,
    )

    updated = 0
    unmatched: List[Dict[str, Any]] = []

    for trend in trends:
        product_id = int(trend["product_id"])
        product_name = str(trend.get("product_name") or "")
        last_week = float(trend.get("last_week_sales") or 0)
        weekly_trend = float(trend.get("weekly_trend_pct") or 0)

        trend_qty = adc_qty_by_product.get(product_id)
        if trend_qty is None:
            trend_qty = last_week

        stock = stock_by_product.get(product_id)
        if stock is None:
            unmatched.append(
                {
                    "product_id": product_id,
                    "product_name": product_name,
                    "reason": "no matching stock master row",
                }
            )
            continue

        reorder_qty = _to_decimal(stock.reorder_qty)
        suggested = _to_decimal(trend_qty) + reorder_qty
        if suggested < 0:
            suggested = Decimal("0")

        stock.last_week_sales = _to_decimal(last_week)
        stock.weekly_trend = _to_decimal(round(weekly_trend, 2))
        stock.suggested_qty = suggested.quantize(Decimal("0.001"))
        stock.updated_by = updated_by
        updated += 1

    db.flush()
    logger.info(
        "Sales trend → stock: updated=%s unmatched=%s tenant=%s company=%s products=%s",
        updated,
        len(unmatched),
        tenant_id,
        company_id,
        len(scoped_product_ids) if scoped_product_ids else "all",
    )
    return {
        "stock_updated": updated,
        "unmatched": unmatched[:50],
        "unmatched_count": len(unmatched),
        "trends_analyzed": len(trends),
        "adc_products": len(adc_qty_by_product),
    }
