"""Pre-aggregate sales_transactions in Postgres for the WhatsApp Chart.js dashboard.

Uses full-table GROUP BY — totals match summing every row; only raw line detail is dropped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import SalesTransaction

_TOP_PRODUCTS = 10
_TABLE_PRODUCTS = 25
_DAILY_DAYS = 90
_MAX_RECORDS = 20000


def _round2(n: float) -> float:
    return round(float(n) * 100) / 100


def _en_in(n: float) -> str:
    """Locale-ish thousands separator for KPI / insight text."""
    v = _round2(n)
    if abs(v - int(v)) < 1e-9:
        return f"{int(v):,}"
    return f"{v:,.2f}"


def build_sales_dashboard_summary(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    product_id: Optional[int] = None,
    product_name: Optional[str] = None,
    category: Optional[str] = None,
    transaction_date_from: Optional[date] = None,
    transaction_date_to: Optional[date] = None,
    focus_question: Optional[str] = None,
) -> Dict[str, Any]:
    base = db.query(SalesTransaction)
    if tenant_id is not None:
        base = base.filter(SalesTransaction.tenant_id == tenant_id)
    if company_id is not None:
        base = base.filter(SalesTransaction.company_id == company_id)
    if product_id is not None:
        base = base.filter(SalesTransaction.product_id == product_id)
    if product_name:
        base = base.filter(SalesTransaction.product_name.ilike(f"%{product_name}%"))
    if category:
        base = base.filter(SalesTransaction.category.ilike(f"%{category}%"))
    if transaction_date_from is not None:
        base = base.filter(SalesTransaction.transaction_date >= transaction_date_from)
    if transaction_date_to is not None:
        base = base.filter(SalesTransaction.transaction_date <= transaction_date_to)

    row_count = int(
        base.with_entities(func.count(SalesTransaction.sales_transaction_id)).scalar()
        or 0
    )
    if row_count == 0:
        return {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "rowCount": 0,
            "kpis": [],
            "monthly": [],
            "daily": [],
            "topProducts": [],
            "categories": [],
            "table": [],
            "insights": [],
            "categoriesList": [],
            "records": [],
        }

    totals = base.with_entities(
        func.coalesce(func.sum(SalesTransaction.qty_sold), 0),
        func.min(SalesTransaction.transaction_date),
        func.max(SalesTransaction.transaction_date),
        func.count(func.distinct(SalesTransaction.product_name)),
        func.count(func.distinct(func.coalesce(SalesTransaction.category, ""))),
    ).one()
    total_qty = float(totals[0] or 0)
    first_date: Optional[date] = totals[1]
    last_date: Optional[date] = totals[2]
    product_count = int(totals[3] or 0)
    category_count = int(totals[4] or 0)

    month_expr = func.to_char(SalesTransaction.transaction_date, "YYYY-MM")
    monthly_rows = (
        base.with_entities(
            month_expr.label("ym"),
            func.coalesce(func.sum(SalesTransaction.qty_sold), 0),
        )
        .group_by(month_expr)
        .order_by(month_expr)
        .all()
    )
    monthly: List[Tuple[str, float]] = [
        (str(r[0]), _round2(float(r[1] or 0))) for r in monthly_rows
    ]

    daily_q = base.with_entities(
        SalesTransaction.transaction_date,
        func.coalesce(func.sum(SalesTransaction.qty_sold), 0),
    ).group_by(SalesTransaction.transaction_date)
    if last_date is not None:
        daily_from = last_date - timedelta(days=_DAILY_DAYS - 1)
        daily_q = daily_q.filter(SalesTransaction.transaction_date >= daily_from)
    daily_rows = daily_q.order_by(SalesTransaction.transaction_date).all()
    daily: List[Tuple[str, float]] = [
        (r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]), _round2(float(r[1] or 0)))
        for r in daily_rows
    ]

    product_rows = (
        base.with_entities(
            SalesTransaction.product_name,
            func.coalesce(func.sum(SalesTransaction.qty_sold), 0),
        )
        .group_by(SalesTransaction.product_name)
        .order_by(func.sum(SalesTransaction.qty_sold).desc())
        .all()
    )
    top_products: List[Tuple[str, float]] = [
        (str(r[0] or "Unknown"), _round2(float(r[1] or 0)))
        for r in product_rows[:_TOP_PRODUCTS]
    ]
    table: List[Tuple[str, float, str]] = []
    for r in product_rows[:_TABLE_PRODUCTS]:
        qty = _round2(float(r[1] or 0))
        share = f"{(qty / total_qty * 100):.1f}%" if total_qty else "0.0%"
        table.append((str(r[0] or "Unknown"), qty, share))

    category_expr = func.coalesce(SalesTransaction.category, "Uncategorized")
    category_rows = (
        base.with_entities(
            category_expr,
            func.coalesce(func.sum(SalesTransaction.qty_sold), 0),
        )
        .group_by(category_expr)
        .order_by(func.sum(SalesTransaction.qty_sold).desc())
        .all()
    )
    categories = [
        {"name": str(r[0] or "Uncategorized"), "value": _round2(float(r[1] or 0))}
        for r in category_rows
    ]
    categories_list = [c["name"] for c in categories]

    record_rows = (
        base.with_entities(
            SalesTransaction.transaction_date,
            SalesTransaction.product_name,
            category_expr,
            func.coalesce(func.sum(SalesTransaction.qty_sold), 0),
        )
        .group_by(
            SalesTransaction.transaction_date,
            SalesTransaction.product_name,
            category_expr,
        )
        .order_by(
            SalesTransaction.transaction_date,
            SalesTransaction.product_name,
        )
        .limit(_MAX_RECORDS)
        .all()
    )
    records = [
        {
            "date": r[0].isoformat() if hasattr(r[0], "isoformat") else str(r[0]),
            "product": str(r[1] or "Unknown"),
            "category": str(r[2] or "Uncategorized"),
            "qty": _round2(float(r[3] or 0)),
        }
        for r in record_rows
    ]

    day_count = len(daily) or max(
        1,
        (
            (last_date - first_date).days + 1
            if first_date and last_date
            else 1
        ),
    )
    # Prefer distinct day count from full range for avg KPI when daily is truncated.
    distinct_days = (
        base.with_entities(func.count(func.distinct(SalesTransaction.transaction_date)))
        .scalar()
        or day_count
    )
    avg_per_day = total_qty / float(distinct_days or 1)

    peak_month = max(monthly, key=lambda x: x[1]) if monthly else None
    peak_day = max(daily, key=lambda x: x[1]) if daily else None
    top_prod = top_products[0] if top_products else None
    top_cat = categories[0] if categories else None

    insights: List[str] = []
    focus = (focus_question or "").strip()
    if focus:
        insights.append(f"Focus: {focus[:180]}")
    if top_prod and total_qty:
        insights.append(
            f"Top product: {top_prod[0]} with {_en_in(top_prod[1])} units "
            f"({(top_prod[1] / total_qty * 100):.1f}% of all units)."
        )
    if top_cat:
        insights.append(
            f"Top category: {top_cat['name']} with {_en_in(top_cat['value'])} units."
        )
    if peak_month or peak_day:
        bits = []
        if peak_month:
            bits.append(f"Peak month: {peak_month[0]} ({_en_in(peak_month[1])} units)")
        if peak_day:
            bits.append(f"Peak day: {peak_day[0]} ({_en_in(peak_day[1])} units)")
        insights.append(". ".join(bits) + ".")
    if first_date and last_date:
        insights.append(
            f"Coverage: {first_date.isoformat()} → {last_date.isoformat()}, "
            f"{row_count:,} transactions, {_en_in(total_qty)} units total."
        )
    insights.append(
        "This export contains quantities only (no price or revenue), so all measures are units sold."
    )

    date_range = (
        f"{first_date.isoformat()} → {last_date.isoformat()}"
        if first_date and last_date
        else "—"
    )
    kpis = [
        {"label": "Total units sold", "value": _en_in(total_qty)},
        {"label": "Transactions", "value": f"{row_count:,}"},
        {"label": "Products", "value": str(product_count)},
        {"label": "Categories", "value": str(category_count)},
        {"label": "Avg units / day", "value": _en_in(avg_per_day)},
        {"label": "Date range", "value": date_range},
    ]

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "rowCount": row_count,
        "kpis": kpis,
        "monthly": monthly,
        "daily": daily,
        "topProducts": top_products,
        "categories": categories,
        "table": table,
        "insights": insights,
        "categoriesList": categories_list,
        "records": records,
    }
