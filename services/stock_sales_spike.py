"""
Detect sustained demand spikes from stock rows using EWMA smoothing, PELT change-point
detection, and IQR filtering for one-off outliers.

The stock table stores last_week_sales and weekly_trend (% WoW change). A short weekly
history is reconstructed backwards for per-product time-series analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence, Union

import numpy as np
import ruptures as rpt
from sqlalchemy.orm import Session

from models import Stock

Number = Union[int, float, Decimal, None]


def _to_float(value: Number) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def reconstruct_weekly_sales(
    last_week_sales: float,
    weekly_trend_pct: float,
    weeks: int = 12,
) -> np.ndarray:
    """
    Build oldest→newest weekly sales. weekly_trend_pct is treated as uniform WoW % change
  used to infer prior weeks from last_week_sales.
    """
    if weeks < 2:
        return np.array([last_week_sales], dtype=float)

    series = np.zeros(weeks, dtype=float)
    series[-1] = max(last_week_sales, 0.0)
    growth = 1.0 + weekly_trend_pct / 100.0
    if abs(growth) < 1e-9:
        growth = 1.0

    for i in range(weeks - 2, -1, -1):
        series[i] = max(series[i + 1] / growth, 0.0)
    return series


def ewma(values: Sequence[float], alpha: float = 0.35) -> np.ndarray:
    """Exponentially weighted moving average (oldest→newest)."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return arr
    alpha = float(np.clip(alpha, 0.01, 1.0))
    out = np.empty_like(arr)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def iqr_bounds(values: Sequence[float], k: float = 1.5) -> tuple[float, float, float]:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return 0.0, 0.0, 0.0
    q1, q3 = np.percentile(arr, [25, 75])
    iqr = q3 - q1
    lower = q1 - k * iqr
    upper = q3 + k * iqr
    return float(lower), float(upper), float(iqr)


def iqr_outlier_indices(values: Sequence[float], k: float = 1.5) -> np.ndarray:
    lower, upper, _ = iqr_bounds(values, k=k)
    arr = np.asarray(values, dtype=float)
    return np.where((arr < lower) | (arr > upper))[0]


def pelt_changepoints(
    values: Sequence[float],
    *,
    pen: float = 3.0,
    min_size: int = 2,
    model: str = "l2",
) -> List[int]:
    """
    PELT segment end indices (1-based segment ends; last value equals len(values)).
    Returns [] when series is too short or flat.
    """
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < 2 * min_size:
        return []
    if np.allclose(arr, arr[0]):
        return []

    signal = arr.reshape(-1, 1)
    algo = rpt.Pelt(model=model, min_size=min_size).fit(signal)
    bkps = algo.predict(pen=pen)
    return [int(b) for b in bkps]


def _segment_means(values: np.ndarray, bkps: List[int]) -> List[tuple[int, int, float]]:
    """Return (start, end_exclusive, mean) for each PELT segment."""
    if not bkps:
        return [(0, len(values), float(np.mean(values)) if len(values) else 0.0)]

    segments: List[tuple[int, int, float]] = []
    start = 0
    for end in bkps:
        end = min(end, len(values))
        if end > start:
            segments.append((start, end, float(np.mean(values[start:end]))))
        start = end
    return segments


@dataclass
class StockSpikeAnalysis:
    stock_id: int
    product_code: str
    product_name: str
    category: Optional[str]
    brand: Optional[str]
    last_week_sales: float
    weekly_trend_pct: float
    available_stock: float
    suggested_qty: float
    ewma_last: float
    spike_ratio: float
    is_sustained_spike: bool
    is_huge_sales: bool
    is_one_off_outlier: bool
    changepoint_week_index: Optional[int]
    mean_before_cp: Optional[float]
    mean_after_cp: Optional[float]
    weekly_sales_series: List[float]
    smoothed_series: List[float]


def analyze_stock_spike(
    *,
    stock_id: int,
    product_code: str,
    product_name: str,
    category: Optional[str],
    brand: Optional[str],
    last_week_sales: Number,
    weekly_trend: Number,
    available_stock: Number = 0,
    suggested_qty: Number = 0,
    lookback_weeks: int = 12,
    ewma_alpha: float = 0.35,
    pelt_penalty: float = 3.0,
    iqr_k: float = 1.5,
    min_uplift_ratio: float = 1.25,
    huge_sales_iqr_k: float = 1.5,
    catalog_sales_upper: Optional[float] = None,
) -> StockSpikeAnalysis:
    sales = last_week_sales_f = _to_float(last_week_sales)
    trend_pct = _to_float(weekly_trend)
    weekly = reconstruct_weekly_sales(sales, trend_pct, weeks=lookback_weeks)
    smoothed = ewma(weekly, alpha=ewma_alpha)

    ewma_last = float(smoothed[-2]) if len(smoothed) >= 2 else float(smoothed[-1])
    spike_ratio = sales / ewma_last if ewma_last > 1e-9 else (sales if sales > 0 else 0.0)

    raw_outliers = set(iqr_outlier_indices(weekly, k=iqr_k).tolist())
    residual = weekly - smoothed
    residual_outliers = set(iqr_outlier_indices(residual, k=iqr_k).tolist())
    last_idx = len(weekly) - 1

    is_one_off = (
        last_idx in raw_outliers
        and last_idx in residual_outliers
        and (last_idx - 1) not in raw_outliers
        and trend_pct <= 0
    )

    bkps = pelt_changepoints(smoothed, pen=pelt_penalty, min_size=2)
    segments = _segment_means(smoothed, bkps)

    changepoint_week_index: Optional[int] = None
    mean_before: Optional[float] = None
    mean_after: Optional[float] = None
    is_sustained = False

    if len(segments) >= 2:
        prev_seg = segments[-2]
        last_seg = segments[-1]
        mean_before = prev_seg[2]
        mean_after = last_seg[2]
        changepoint_week_index = prev_seg[1]
        if mean_before > 1e-9:
            uplift = mean_after / mean_before
        else:
            uplift = mean_after if mean_after > 0 else 0.0
        recent_cp = changepoint_week_index >= len(weekly) - 4
        is_sustained = (
            recent_cp
            and uplift >= min_uplift_ratio
            and not is_one_off
            and mean_after > mean_before
        )
    elif trend_pct >= 10.0 and spike_ratio >= min_uplift_ratio and not is_one_off:
        is_sustained = True
        mean_before = float(np.mean(weekly[:-1])) if len(weekly) > 1 else 0.0
        mean_after = float(weekly[-1])

    _, upper_sales, _ = iqr_bounds(weekly, k=iqr_k)
    above_local_iqr = sales > upper_sales and not is_one_off

    if catalog_sales_upper is not None and sales >= catalog_sales_upper:
        is_huge = True
    elif above_local_iqr and (is_sustained or spike_ratio >= min_uplift_ratio):
        is_huge = True
    elif is_sustained and trend_pct > 0 and spike_ratio >= min_uplift_ratio:
        is_huge = True
    elif sales > 0 and spike_ratio >= 1.75 and trend_pct > 0 and not is_one_off:
        is_huge = True
    else:
        is_huge = False

    return StockSpikeAnalysis(
        stock_id=stock_id,
        product_code=product_code,
        product_name=product_name,
        category=category,
        brand=brand,
        last_week_sales=last_week_sales_f,
        weekly_trend_pct=trend_pct,
        available_stock=_to_float(available_stock),
        suggested_qty=_to_float(suggested_qty),
        ewma_last=float(smoothed[-1]),
        spike_ratio=round(spike_ratio, 4),
        is_sustained_spike=is_sustained or (above_local_iqr and not is_one_off),
        is_huge_sales=is_huge,
        is_one_off_outlier=is_one_off,
        changepoint_week_index=changepoint_week_index,
        mean_before_cp=mean_before,
        mean_after_cp=mean_after,
        weekly_sales_series=[round(float(x), 3) for x in weekly],
        smoothed_series=[round(float(x), 3) for x in smoothed],
    )


def _catalog_sales_upper_bound(sales_values: Iterable[float], k: float) -> Optional[float]:
    arr = np.asarray(list(sales_values), dtype=float)
    if arr.size < 4:
        return None
    _, upper, _ = iqr_bounds(arr, k=k)
    return upper


def detect_stock_sales_spikes(
    db: Session,
    *,
    tenant_id: Optional[int] = None,
    company_id: Optional[int] = None,
    category: Optional[str] = None,
    brand: Optional[str] = None,
    only_huge: bool = True,
    only_sustained: bool = True,
    limit: int = 100,
    lookback_weeks: int = 12,
    ewma_alpha: float = 0.35,
    pelt_penalty: float = 3.0,
    iqr_k: float = 1.5,
    min_uplift_ratio: float = 1.25,
) -> List[StockSpikeAnalysis]:
    q = db.query(Stock)
    if tenant_id is not None:
        q = q.filter(Stock.tenant_id == tenant_id)
    if company_id is not None:
        q = q.filter(Stock.company_id == company_id)
    if category:
        q = q.filter(Stock.category.ilike(f"%{category}%"))
    if brand:
        q = q.filter(Stock.brand.ilike(f"%{brand}%"))

    rows: List[Stock] = q.order_by(Stock.last_week_sales.desc()).all()
    catalog_upper = _catalog_sales_upper_bound(
        (_to_float(r.last_week_sales) for r in rows),
        k=iqr_k,
    )

    results: List[StockSpikeAnalysis] = []
    for row in rows:
        analysis = analyze_stock_spike(
            stock_id=row.stock_id,
            product_code=row.product_code,
            product_name=row.product_name,
            category=row.category,
            brand=row.brand,
            last_week_sales=row.last_week_sales,
            weekly_trend=row.weekly_trend,
            available_stock=row.available_stock,
            suggested_qty=row.suggested_qty,
            lookback_weeks=lookback_weeks,
            ewma_alpha=ewma_alpha,
            pelt_penalty=pelt_penalty,
            iqr_k=iqr_k,
            min_uplift_ratio=min_uplift_ratio,
            catalog_sales_upper=catalog_upper,
        )
        if only_sustained and not analysis.is_sustained_spike:
            continue
        if only_huge and not analysis.is_huge_sales:
            continue
        results.append(analysis)

    results.sort(
        key=lambda a: (a.last_week_sales, a.spike_ratio),
        reverse=True,
    )
    return results[:limit]
