from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class StockUploadDetailBase(BaseModel):
    tenant_id: int
    company_id: int
    stock_upload_hdr_id: int
    line_no: int
    supplier_product_id: int
    available_stock: Optional[Decimal] = Decimal("0")


class StockUploadDetailCreate(StockUploadDetailBase):
    pass


class StockUploadDetailUpdate(BaseModel):
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    stock_upload_hdr_id: Optional[int] = None
    line_no: Optional[int] = None
    supplier_product_id: Optional[int] = None
    available_stock: Optional[Decimal] = None


class StockUploadDetailResponse(StockUploadDetailBase):
    stock_upload_dtl_id: int
    created_by: Optional[int] = None
    created_on: Optional[datetime] = None
    updated_by: Optional[int] = None
    updated_on: Optional[datetime] = None

    class Config:
        from_attributes = True
