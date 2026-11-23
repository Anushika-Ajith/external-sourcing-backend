"""Average Daily Consumption (ADC) and demand-growth analysis from sales_transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from models import SalesTransaction, Vendor, VendorItem, VendorRate

PERIOD_DAYS = 30
DEFAULT_HUGE_GROWTH_PCT = 25.0


def average_daily_consumption(total_units: float, days: int) -> float:
    """ADC = total sales in period ÷ number of days in period."""
    if days <= 0:
        return 0.0
    return float(total_units) / float(days)


def demand_increase_units(current_adc: float, previous_adc: float) -> float:
    """Absolute increase in units per day (current minus previous)."""
    return float(current_adc) - float(previous_adc)


def growth_percent_against_original(increase: float, original_adc: float) -> float:
    """
    Growth % = (increase ÷ original) × 100.
    Original is the previous-period ADC (standard growth measurement).
    """
    if original_adc <= 1e-9:
        return 100.0 if increase > 0 else 0.0
    return (float(increase) / float(original_adc)) * 100.0


def is_sustained_adc_growth(
    *,
    oldest_adc: Optional[float],
    middle_adc: float,
    current_adc: float,
    uplift_ratio: float = 1.1,
    reversal_ratio: float = 0.9,
) -> bool:
    """
    Detect sustained demand vs a one-month spike that reverses (e.g. Apr 20, May 25, Jun 20).

    With three 30-day periods [oldest, middle, current]:
    - Reject when middle spikes vs oldest and current drops back below middle.
    - Accept when current remains elevated vs oldest and does not collapse vs middle.

    With only two periods (oldest_adc is None), procurement-sustained is False.
    """
    if oldest_adc is None:
        return False

    o = float(oldest_adc)
    m = float(middle_adc)
    c = float(current_adc)

    if o <= 1e-9 and m <= 1e-9 and c <= 1e-9:
        return False

    middle_spike = m > o * uplift_ratio if o > 1e-9 else m > 0
    reverted_after_spike = middle_spike and c < m * reversal_ratio

    if reverted_after_spike:
        return False

    if o > 1e-9:
        return c >= o * uplift_ratio and c >= m * reversal_ratio
    return c >= m * reversal_ratio


def _to_float(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _period_windows(anchor: date, period_days: int = PERIOD_DAYS) -> Tuple[
    Tuple[date, date],
    Tuple[date, date],
    Tuple[date, date],
]:
    current_end = anchor
    current_start = anchor - timedelta(days=period_days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=period_days - 1)
    oldest_end = previous_start - timedelta(days=1)
    oldest_start = oldest_end - timedelta(days=period_days - 1)
    return (
        (current_start, current_end),
        (previous_start, previous_end),
        (oldest_start, oldest_end),
    )


@dataclass
class SalesAdcSpikeAnalysis:
    product_id: int
    product_name: str
    category: Optional[str]
    period_days: int
    data_anchor_date: date
    previous_period_total: float
    current_period_total: float
    oldest_period_total: float
    previous_adc: float
    current_adc: float
    oldest_adc: float
    demand_increase_units: float
    growth_pct: float
    is_sustained_growth: bool
    is_huge_growth: bool
    has_three_periods: bool
    suggested_qty: float
    suggested_vendor: Optional[str]
    suggested_vendor_id: Optional[int]
    adc_series: List[float]


def _analyze_product_adc(
    *,
    product_id: int,
    product_name: str,
    category: Optional[str],
    current_total: float,
    previous_total: float,
    oldest_total: float,
    anchor: date,
    period_days: int,
    has_three_periods: bool,
    min_growth_pct: float,
    huge_growth_pct: float,
) -> Optional[SalesAdcSpikeAnalysis]:
    previous_adc = average_daily_consumption(previous_total, period_days)
    current_adc = average_daily_consumption(current_total, period_days)
    oldest_adc = average_daily_consumption(oldest_total, period_days) if has_three_periods else 0.0

    increase = demand_increase_units(current_adc, previous_adc)
    growth_pct = growth_percent_against_original(increase, previous_adc)

    if previous_adc <= 1e-9 and current_adc <= 1e-9:
        return None

    sustained = is_sustained_adc_growth(
        oldest_adc=oldest_adc if has_three_periods and oldest_total > 0 else None,
        middle_adc=previous_adc,
        current_adc=current_adc,
    )
    is_huge = growth_pct >= huge_growth_pct and increase > 0

    # Suggested procurement lift: extra units for one month at the daily increase rate
    suggested_qty = max(0.0, round(increase * period_days, 3))

    adc_series = (
        [oldest_adc, previous_adc, current_adc]
        if has_three_periods and oldest_total > 0
        else [previous_adc, current_adc]
    )

    if growth_pct < min_growth_pct and not sustained:
        return None

    return SalesAdcSpikeAnalysis(
        product_id=product_id,
        product_name=product_name or f"Product {product_id}",
        category=category,
        period_days=period_days,
        data_anchor_date=anchor,
        previous_period_total=round(previous_total, 3),
        current_period_total=round(current_total, 3),
        oldest_period_total=round(oldest_total, 3),
        previous_adc=round(previous_adc, 4),
        current_adc=round(current_adc, 4),
        oldest_adc=round(oldest_adc, 4),
        demand_increase_units=round(increase, 4),
        growth_pct=round(growth_pct, 2),
        is_sustained_growth=sustained,
        is_huge_growth=is_huge,
        has_three_periods=has_three_periods and oldest_total > 0,
        suggested_qty=suggested_qty,
        suggested_vendor=None,
        suggested_vendor_id=None,
        adc_series=adc_series,
    )


def _rate_dates_valid(valid_from: Optional[date], valid_to: Optional[date], on_day: date) -> bool:
    if valid_from is not None and valid_from > on_day:
        return False
    if valid_to is not None and valid_to < on_day:
        return False
    return True


def _product_ids_from_vendor_item(
    *,
    item_id: Optional[int],
    erp_code: Optional[str],
    product_ids: List[int],
    product_id_set: set,
) -> List[int]:
    """Map a vendor_item row back to one or more spike product_ids."""
    matched: List[int] = []
    if item_id is not None and int(item_id) in product_id_set:
        matched.append(int(item_id))

    code = (erp_code or "").strip()
    if not code:
        return matched

    # Direct numeric erp_code, plus common ITM{id} / ITM-{id} patterns
    upper = code.upper()
    for pid in product_ids:
        if code == str(pid) or upper in {f"ITM{pid}", f"ITM-{pid}"}:
            if pid not in matched:
                matched.append(pid)
    return matched


def _resolve_suggested_vendors(
    db: Session,
    product_ids: List[int],
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
) -> Dict[int, Tuple[Optional[int], Optional[str]]]:
    """
    Map product_id → (vendor_id, vendor_name).

    Default is the vendor with a currently valid rate-contract date for the product:
    valid_from ≤ today ≤ valid_to (null bounds treated as open-ended).
    Prefers approved rates, then newest valid_from.
    """
    _ = tenant_id, company_id  # reserved for future tenant-scoped rate sources
    if not product_ids:
        return {}

    today = date.today()
    product_id_set = set(int(pid) for pid in product_ids)
    code_keys: List[str] = []
    for pid in product_ids:
        code_keys.extend([str(pid), f"ITM{pid}", f"ITM-{pid}", f"itm{pid}", f"itm-{pid}"])

    rate_rows = (
        db.query(
            VendorItem.item_id,
            VendorItem.erp_code,
            VendorItem.vendor_id.label("item_vendor_id"),
            VendorRate.vendor_id.label("rate_vendor_id"),
            VendorRate.valid_from,
            VendorRate.valid_to,
            VendorRate.status,
            VendorRate.id,
        )
        .join(VendorRate, VendorRate.item_id == VendorItem.id)
        .filter(
            or_(
                VendorItem.item_id.in_(product_ids),
                VendorItem.erp_code.in_(code_keys),
            ),
            or_(VendorRate.valid_from.is_(None), VendorRate.valid_from <= today),
            or_(VendorRate.valid_to.is_(None), VendorRate.valid_to >= today),
        )
        .all()
    )

    vendor_ids_needed = {
        int(vid)
        for row in rate_rows
        for vid in (row.rate_vendor_id or row.item_vendor_id,)
        if vid is not None
    }

    vendor_names: Dict[int, str] = {}
    if vendor_ids_needed:
        for v in (
            db.query(Vendor.vendor_id, Vendor.company_name)
            .filter(Vendor.vendor_id.in_(list(vendor_ids_needed)))
            .all()
        ):
            name = (v.company_name or "").strip()
            if name:
                vendor_names[int(v.vendor_id)] = name

    candidates: Dict[int, List[Tuple]] = {}
    for row in rate_rows:
        if not _rate_dates_valid(row.valid_from, row.valid_to, today):
            continue
        vid = row.rate_vendor_id or row.item_vendor_id
        if vid is None:
            continue
        vendor_id = int(vid)
        name = vendor_names.get(vendor_id)
        if not name:
            continue

        matched_pids = _product_ids_from_vendor_item(
            item_id=row.item_id,
            erp_code=row.erp_code,
            product_ids=product_ids,
            product_id_set=product_id_set,
        )
        status = (row.status or "").strip().lower()
        score = 0 if status == "approved" else 1
        valid_from = row.valid_from or date.min
        for pid in matched_pids:
            candidates.setdefault(pid, []).append(
                (score, valid_from, int(row.id), vendor_id, name)
            )

    suggested: Dict[int, Tuple[Optional[int], Optional[str]]] = {}
    for pid, rows in candidates.items():
        rows.sort(
            key=lambda r: (
                r[0],
                -(r[1].toordinal() if isinstance(r[1], date) else 0),
                -r[2],
            )
        )
        _score, _vf, _rid, vendor_id, name = rows[0]
        suggested[pid] = (vendor_id, name)

    return suggested


def detect_sales_adc_spikes(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    category: Optional[str] = None,
    product_ids: Optional[List[int]] = None,
    only_huge: bool = False,
    only_sustained: bool = False,
    min_growth_pct: float = 0.0,
    huge_growth_pct: float = DEFAULT_HUGE_GROWTH_PCT,
    period_days: int = PERIOD_DAYS,
    limit: int = 100,
) -> List[SalesAdcSpikeAnalysis]:
    """
    Compare consecutive 30-day ADC windows from sales_transactions.
    Requires at least ~60 days of sales history (two full periods).
    """
    anchor_q = db.query(func.max(SalesTransaction.transaction_date))
    if tenant_id is not None:
        anchor_q = anchor_q.filter(SalesTransaction.tenant_id == tenant_id)
    if company_id is not None:
        anchor_q = anchor_q.filter(SalesTransaction.company_id == company_id)
    anchor: Optional[date] = anchor_q.scalar()
    if anchor is None:
        return []

    min_q = db.query(func.min(SalesTransaction.transaction_date))
    if tenant_id is not None:
        min_q = min_q.filter(SalesTransaction.tenant_id == tenant_id)
    if company_id is not None:
        min_q = min_q.filter(SalesTransaction.company_id == company_id)
    min_date: Optional[date] = min_q.scalar()

    (current_start, current_end), (previous_start, previous_end), (
        oldest_start,
        oldest_end,
    ) = _period_windows(anchor, period_days)

    span_days = (anchor - min_date).days + 1 if min_date else 0
    has_three_periods = span_days >= period_days * 3

    current_sum = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        SalesTransaction.transaction_date >= current_start,
                        SalesTransaction.transaction_date <= current_end,
                    ),
                    SalesTransaction.qty_sold,
                ),
                else_=0,
            )
        ),
        0,
    )
    previous_sum = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        SalesTransaction.transaction_date >= previous_start,
                        SalesTransaction.transaction_date <= previous_end,
                    ),
                    SalesTransaction.qty_sold,
                ),
                else_=0,
            )
        ),
        0,
    )
    oldest_sum = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        SalesTransaction.transaction_date >= oldest_start,
                        SalesTransaction.transaction_date <= oldest_end,
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
        current_sum.label("current_total"),
        previous_sum.label("previous_total"),
        oldest_sum.label("oldest_total"),
    ).filter(
        SalesTransaction.transaction_date >= oldest_start,
        SalesTransaction.transaction_date <= current_end,
    )

    if tenant_id is not None:
        q = q.filter(SalesTransaction.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(SalesTransaction.company_id == company_id)
    if category:
        q = q.filter(SalesTransaction.category.ilike(f"%{category}%"))
    if product_ids:
        q = q.filter(SalesTransaction.product_id.in_(product_ids))

    q = q.group_by(SalesTransaction.product_id)

    if span_days < period_days * 2:
        return []

    results: List[SalesAdcSpikeAnalysis] = []
    for row in q.all():
        analysis = _analyze_product_adc(
            product_id=int(row.product_id),
            product_name=row.product_name,
            category=row.category,
            current_total=_to_float(row.current_total),
            previous_total=_to_float(row.previous_total),
            oldest_total=_to_float(row.oldest_total),
            anchor=anchor,
            period_days=period_days,
            has_three_periods=has_three_periods,
            min_growth_pct=min_growth_pct,
            huge_growth_pct=huge_growth_pct,
        )
        if analysis is None:
            continue
        if only_sustained and not analysis.is_sustained_growth:
            continue
        if only_huge and not analysis.is_huge_growth:
            continue
        results.append(analysis)

    results.sort(key=lambda r: (r.growth_pct, r.current_adc), reverse=True)
    limited = results[:limit]

    vendor_by_product = _resolve_suggested_vendors(
        db,
        [r.product_id for r in limited],
        tenant_id=tenant_id,
        company_id=company_id,
    )
    for row in limited:
        vendor_id, vendor_name = vendor_by_product.get(row.product_id, (None, None))
        row.suggested_vendor_id = vendor_id
        row.suggested_vendor = vendor_name

    return limited
