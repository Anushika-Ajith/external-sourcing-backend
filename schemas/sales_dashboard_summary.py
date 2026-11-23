"""WhatsApp Chart.js sales dashboard summary (pre-aggregated)."""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field


class SalesDashboardKpi(BaseModel):
    label: str
    value: str


class SalesDashboardCategory(BaseModel):
    name: str
    value: float


class SalesDashboardRecord(BaseModel):
    date: str
    product: str
    category: str
    qty: float


class SalesDashboardSummaryResponse(BaseModel):
    """Matches whatsapp-backend SalesDashboardData (+ rowCount)."""

    generatedAt: Optional[str] = None
    rowCount: int = 0
    kpis: List[SalesDashboardKpi] = Field(default_factory=list)
    monthly: List[Tuple[str, float]] = Field(default_factory=list)
    daily: List[Tuple[str, float]] = Field(default_factory=list)
    topProducts: List[Tuple[str, float]] = Field(default_factory=list)
    categories: List[SalesDashboardCategory] = Field(default_factory=list)
    table: List[Tuple[str, float, str]] = Field(default_factory=list)
    insights: List[str] = Field(default_factory=list)
    categoriesList: List[str] = Field(default_factory=list)
    records: List[SalesDashboardRecord] = Field(default_factory=list)
