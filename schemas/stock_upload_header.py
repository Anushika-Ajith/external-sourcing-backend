from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class StockUploadHeaderBase(BaseModel):
    tenant_id: int
    company_id: int
    document_no: str = Field(..., max_length=30)
    upload_datetime: Optional[datetime] = None
    upload_source: Optional[str] = Field(None, max_length=20)
    status: Optional[str] = Field(default="PENDING", max_length=20)


class StockUploadHeaderCreate(StockUploadHeaderBase):
    pass


class StockUploadHeaderUpdate(BaseModel):
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    document_no: Optional[str] = Field(None, max_length=30)
    upload_datetime: Optional[datetime] = None
    upload_source: Optional[str] = Field(None, max_length=20)
    status: Optional[str] = Field(None, max_length=20)


class StockUploadHeaderResponse(StockUploadHeaderBase):
    stock_upload_hdr_id: int
    created_by: Optional[int] = None
    created_on: Optional[datetime] = None
    updated_by: Optional[int] = None
    updated_on: Optional[datetime] = None

    class Config:
        from_attributes = True
