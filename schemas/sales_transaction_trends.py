from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class SalesTransactionTrendResponse(BaseModel):
    product_id: int
    product_name: str
    category: Optional[str] = None
    total_qty_sold: Decimal
    last_week_sales: Decimal
    weekly_trend_pct: float

    class Config:
        from_attributes = True
