"""Schemas for invoice list / pending endpoints (ERP-backed)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict


class InvoiceHeaderOut(BaseModel):
    """Invoice header returned by GET /invoice/pending."""

    model_config = ConfigDict(extra="ignore")

    tenant_id: int
    company_id: int
    document_type: str
    invoice_no: str
    version_no: int = 1
    vendor_id: int
    po_number: str
    po_id: Optional[int] = None
    invoice_date: Optional[Union[date, datetime, str]] = None
    invoice_amount: Optional[Union[Decimal, float, str]] = None
    currency_code: Optional[str] = None
    status: Optional[str] = None
    remarks: Optional[str] = None


class InvoicePendingListResponse(BaseModel):
    items: List[InvoiceHeaderOut]
    total: int
    take: int
    skip: int
