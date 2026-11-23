"""Aggregate sales_transactions for trend views."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from models import SalesTransaction


def _resolve_trend_anchor_date(
    db: Session,
    *,
    tenant_id: Optional[int],
    company_id: Optional[int],
    category: Optional[str],
    product_ids: Optional[List[int]],
    anchor_date: Optional[date],
) -> date:
    if anchor_date is not None:
        return anchor_date

    q = db.query(func.max(SalesTransaction.transaction_date))
    if tenant_id is not None:
        q = q.filter(SalesTransaction.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(SalesTransaction.company_id == company_id)
    if category:
        q = q.filter(SalesTransaction.category.ilike(f"%{category}%"))
    if product_ids:
        q = q.filter(SalesTransaction.product_id.in_(product_ids))

    latest = q.scalar()
    return latest or date.today()


def list_sales_trends_by_product(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    category: Optional[str] = None,
    product_ids: Optional[List[int]] = None,
    skip: int = 0,
    limit: int = 50,
    anchor_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    Weekly sales trend per product.

    Windows are anchored on the latest transaction_date in scope (not necessarily
    today), so historical sales register uploads still produce meaningful trends.
    """
    anchor = _resolve_trend_anchor_date(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        category=category,
        product_ids=product_ids,
        anchor_date=anchor_date,
    )
    current_week_start = anchor - timedelta(days=6)
    prior_week_start = anchor - timedelta(days=13)

    last_week_sales = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        SalesTransaction.transaction_date >= current_week_start,
                        SalesTransaction.transaction_date <= anchor,
                    ),
                    SalesTransaction.qty_sold,
                ),
                else_=0,
            )
        ),
        0,
    )
    prev_week_sales = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        SalesTransaction.transaction_date >= prior_week_start,
                        SalesTransaction.transaction_date < current_week_start,
                    ),
                    SalesTransaction.qty_sold,
                ),
                else_=0,
            )
        ),
        0,
    )

    q = db.query(
        SalesTransaction.product_id,
        func.max(SalesTransaction.product_name).label("product_name"),
        func.max(SalesTransaction.category).label("category"),
        func.sum(SalesTransaction.qty_sold).label("total_qty_sold"),
        last_week_sales.label("last_week_sales"),
        prev_week_sales.label("prev_week_sales"),
    )

    if tenant_id is not None:
        q = q.filter(SalesTransaction.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(SalesTransaction.company_id == company_id)
    if category:
        q = q.filter(SalesTransaction.category.ilike(f"%{category}%"))
    if product_ids:
        q = q.filter(SalesTransaction.product_id.in_(product_ids))

    q = q.group_by(SalesTransaction.product_id).order_by(
        func.sum(SalesTransaction.qty_sold).desc()
    )

    rows = q.offset(skip).limit(limit).all()
    out: List[Dict[str, Any]] = []
    for row in rows:
        lw = float(row.last_week_sales or 0)
        pw = float(row.prev_week_sales or 0)
        if pw > 0:
            trend_pct = ((lw - pw) / pw) * 100.0
        elif lw > 0:
            trend_pct = 100.0
        else:
            trend_pct = 0.0
        out.append(
            {
                "product_id": int(row.product_id),
                "product_name": row.product_name or "",
                "category": row.category,
                "total_qty_sold": row.total_qty_sold or 0,
                "last_week_sales": lw,
                "weekly_trend_pct": trend_pct,
                "trend_anchor_date": anchor.isoformat(),
            }
        )
    return out
