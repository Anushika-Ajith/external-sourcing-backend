from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class StockBase(BaseModel):
    product_code: str = Field(..., max_length=50)
    barcode: Optional[str] = Field(None, max_length=50)
    product_name: str = Field(..., max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    brand: Optional[str] = Field(None, max_length=100)
    unit: Optional[str] = Field(None, max_length=20)
    available_stock: Optional[Decimal] = Field(default=Decimal("0"))
    reorder_level: Optional[Decimal] = Field(default=Decimal("0"))
    reorder_qty: Optional[Decimal] = Field(default=Decimal("0"))
    last_week_sales: Optional[Decimal] = Field(default=Decimal("0"))
    weekly_trend: Optional[Decimal] = Field(default=Decimal("0"))
    suggested_qty: Optional[Decimal] = Field(default=Decimal("0"))
    preferred_supplier: Optional[str] = Field(None, max_length=150)
    tenant_id: Optional[int] = None
    company_id: Optional[int] = Field(default=3)


class StockCreate(StockBase):
    pass


class StockUpdate(BaseModel):
    product_code: Optional[str] = Field(None, max_length=50)
    barcode: Optional[str] = Field(None, max_length=50)
    product_name: Optional[str] = Field(None, max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    brand: Optional[str] = Field(None, max_length=100)
    unit: Optional[str] = Field(None, max_length=20)
    available_stock: Optional[Decimal] = None
    reorder_level: Optional[Decimal] = None
    reorder_qty: Optional[Decimal] = None
    last_week_sales: Optional[Decimal] = None
    weekly_trend: Optional[Decimal] = None
    suggested_qty: Optional[Decimal] = None
    preferred_supplier: Optional[str] = Field(None, max_length=150)
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None


class StockResponse(StockBase):
    stock_id: int
    pipeline_qty: Optional[Decimal] = Field(
        default=Decimal("0"),
        description="Open PO qty not yet received (PO qty − GRN received).",
    )
    stock_with_pipeline: Optional[Decimal] = Field(
        default=Decimal("0"),
        description="available_stock + pipeline_qty.",
    )
    needs_reorder: bool = Field(
        default=False,
        description="True when stock_with_pipeline < reorder_level.",
    )
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
