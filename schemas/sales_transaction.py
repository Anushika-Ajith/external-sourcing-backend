from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class SalesTransactionBase(BaseModel):
    transaction_date: date
    product_id: int
    product_name: str = Field(..., max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    qty_sold: Decimal
    tenant_id: Optional[int] = 1
    company_id: Optional[int] = 3


class SalesTransactionCreate(SalesTransactionBase):
    pass


class SalesTransactionUpdate(BaseModel):
    transaction_date: Optional[date] = None
    product_id: Optional[int] = None
    product_name: Optional[str] = Field(None, max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    qty_sold: Optional[Decimal] = None
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None


class SalesTransactionResponse(SalesTransactionBase):
    sales_transaction_id: int
    created_by: Optional[int] = None
    created_on: Optional[datetime] = None
    created_ip: Optional[str] = None
    modified_by: Optional[int] = None
    modified_on: Optional[datetime] = None
    modified_ip: Optional[str] = None

    class Config:
        from_attributes = True
