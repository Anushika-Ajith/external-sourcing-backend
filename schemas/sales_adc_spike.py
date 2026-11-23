from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class SalesAdcSpikeResponse(BaseModel):
    product_id: int
    product_name: str
    category: Optional[str] = None
    period_days: int
    data_anchor_date: date
    previous_period_total: float
    current_period_total: float
    oldest_period_total: float
    previous_adc: float = Field(description="Previous 30-day average daily consumption (units/day)")
    current_adc: float = Field(description="Current 30-day average daily consumption (units/day)")
    oldest_adc: float = Field(description="Oldest 30-day ADC when 90 days of history exist")
    demand_increase_units: float = Field(
        description="current_adc − previous_adc (units per day)"
    )
    growth_pct: float = Field(
        description="(increase ÷ previous_adc) × 100 — growth vs original period"
    )
    is_sustained_growth: bool = Field(
        description="True when 90-day history shows growth is not a one-month spike that reversed"
    )
    is_huge_growth: bool
    has_three_periods: bool
    suggested_qty: float = Field(
        description="Procurement hint: demand_increase_units × period_days when growth is actionable"
    )
    suggested_vendor: Optional[str] = Field(
        None,
        description="Vendor with a currently valid rate contract for this product",
    )
    suggested_vendor_id: Optional[int] = Field(
        None,
        description="Vendor id with a currently valid rate contract (valid_from/valid_to covers today)",
    )
    adc_series: List[float] = Field(
        description="ADC per period oldest→newest (2 or 3 points)"
    )


class SalesAdcSpikeListResponse(BaseModel):
    count: int
    period_days: int
    data_anchor_date: Optional[date] = None
    items: List[SalesAdcSpikeResponse]


class CreatePoFromSalesAdcSpikesRequest(BaseModel):
    product_ids: List[int] = Field(..., min_length=1, max_length=200)
    tenant_id: int = 1
    company_id: int = 3
    department_id: int = 1
    document_type: str = Field(default="PO", max_length=100)
    version_no: int = 1
    vendor_id: Optional[int] = Field(
        None,
        description="PO vendor; defaults to the authenticated vendor user when omitted",
    )
    quantities: Optional[Dict[str, float]] = Field(
        None,
        description=(
            "Optional map of product_id (string key) → order qty. "
            "When omitted or missing for a product, suggested_qty is used."
        ),
    )


class CreatePoFromSalesAdcSpikesResponse(BaseModel):
    po_id: int
    po_number: str
    line_count: int
    po_total_amount: Optional[Decimal] = None
    net_total_amount: Optional[Decimal] = None
    message: str
