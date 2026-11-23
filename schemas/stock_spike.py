from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class StockSpikeAnalysisResponse(BaseModel):
    stock_id: int
    product_code: str
    product_name: str
    category: Optional[str] = None
    brand: Optional[str] = None
    last_week_sales: float
    weekly_trend_pct: float
    available_stock: float
    suggested_qty: float
    ewma_last: float
    spike_ratio: float = Field(
        description="last_week_sales divided by prior-week EWMA baseline"
    )
    is_sustained_spike: bool
    is_huge_sales: bool
    is_one_off_outlier: bool
    changepoint_week_index: Optional[int] = Field(
        None,
        description="Index in reconstructed weekly series where PELT detected a level shift",
    )
    mean_before_cp: Optional[float] = None
    mean_after_cp: Optional[float] = None
    weekly_sales_series: List[float]
    smoothed_series: List[float]


class StockSpikeDetectionListResponse(BaseModel):
    count: int
    items: List[StockSpikeAnalysisResponse]


class CreatePoFromSalesSpikesRequest(BaseModel):
    stock_ids: List[int] = Field(..., min_length=1, max_length=200)
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
            "Optional map of stock_id (string key) → order qty. "
            "When omitted or missing for a stock row, suggested/reorder qty is used."
        ),
    )


class CreatePoFromSalesSpikesResponse(BaseModel):
    po_id: int
    po_number: str
    line_count: int
    po_total_amount: Optional[Decimal] = None
    net_total_amount: Optional[Decimal] = None
    message: str
    # Same header fields the UI uses for approval / invoice-upload gating
    approval_status: Optional[str] = None
    document_type: Optional[str] = None
    version_no: Optional[int] = None
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
