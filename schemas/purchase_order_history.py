from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class PurchaseOrderHeaderHistoryBase(BaseModel):
    tenant_id: int
    company_id: int
    document_type: str = Field(..., max_length=100)
    po_number: str = Field(..., max_length=30)
    version_no: int = 1
    po_date: date
    vendor_id: int
    vendor_site_id: Optional[int] = None
    vendor_name_snapshot: Optional[str] = Field(None, max_length=200)
    currency_code: Optional[str] = Field(None, max_length=10)
    exchange_rate: Optional[Decimal] = None
    incoterms: Optional[str] = Field(None, max_length=100)
    delivery_terms: Optional[str] = Field(None, max_length=200)
    billing_address_id: Optional[int] = None
    shipping_address_id: Optional[int] = None
    expected_delivery_date: Optional[date] = None
    status: Optional[str] = Field(None, max_length=50)
    approval_status: Optional[str] = Field(None, max_length=50)
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    rejected_by: Optional[int] = None
    rejected_at: Optional[datetime] = None
    rejection_reason: Optional[str] = Field(None, max_length=500)
    po_total_amount: Optional[Decimal] = None
    tax_total_amount: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    net_total_amount: Optional[Decimal] = None
    source_type: Optional[str] = Field(None, max_length=100)
    source_requisition_no: Optional[str] = Field(None, max_length=30)
    vendor_acknowledged: Optional[bool] = False
    acknowledged_at: Optional[datetime] = None
    dispatch_status: Optional[str] = Field(None, max_length=50)
    dispatched_at: Optional[datetime] = None
    is_payment_terms_overridden: Optional[bool] = False
    remarks: Optional[str] = Field(None, max_length=500)
    attachment_count: Optional[int] = None
    document_number: Optional[str] = Field(None, max_length=200)
    po_id: Optional[int] = None
    document_hash: Optional[str] = Field(None, max_length=64)
    business_hash: Optional[str] = Field(None, max_length=64)
    revision_no: Optional[int] = 1
    current_revision: Optional[bool] = True
    history_action: Optional[str] = Field(None, max_length=50)
    history_reason: Optional[str] = Field(None, max_length=500)
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PurchaseOrderHeaderHistoryCreate(PurchaseOrderHeaderHistoryBase):
    history_created_by: Optional[int] = None


class PurchaseOrderHeaderHistoryUpdate(BaseModel):
    po_date: Optional[date] = None
    vendor_id: Optional[int] = None
    vendor_site_id: Optional[int] = None
    vendor_name_snapshot: Optional[str] = Field(None, max_length=200)
    currency_code: Optional[str] = Field(None, max_length=10)
    exchange_rate: Optional[Decimal] = None
    incoterms: Optional[str] = Field(None, max_length=100)
    delivery_terms: Optional[str] = Field(None, max_length=200)
    billing_address_id: Optional[int] = None
    shipping_address_id: Optional[int] = None
    expected_delivery_date: Optional[date] = None
    status: Optional[str] = Field(None, max_length=50)
    approval_status: Optional[str] = Field(None, max_length=50)
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    rejected_by: Optional[int] = None
    rejected_at: Optional[datetime] = None
    rejection_reason: Optional[str] = Field(None, max_length=500)
    po_total_amount: Optional[Decimal] = None
    tax_total_amount: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    net_total_amount: Optional[Decimal] = None
    source_type: Optional[str] = Field(None, max_length=100)
    source_requisition_no: Optional[str] = Field(None, max_length=30)
    vendor_acknowledged: Optional[bool] = None
    acknowledged_at: Optional[datetime] = None
    dispatch_status: Optional[str] = Field(None, max_length=50)
    dispatched_at: Optional[datetime] = None
    is_payment_terms_overridden: Optional[bool] = None
    remarks: Optional[str] = Field(None, max_length=500)
    attachment_count: Optional[int] = None
    document_number: Optional[str] = Field(None, max_length=200)
    po_id: Optional[int] = None
    document_hash: Optional[str] = Field(None, max_length=64)
    business_hash: Optional[str] = Field(None, max_length=64)
    revision_no: Optional[int] = None
    current_revision: Optional[bool] = None
    history_action: Optional[str] = Field(None, max_length=50)
    history_reason: Optional[str] = Field(None, max_length=500)
    updated_by: Optional[int] = None
    updated_at: Optional[datetime] = None


class PurchaseOrderHeaderHistoryResponse(PurchaseOrderHeaderHistoryBase):
    history_id: int
    history_created_by: Optional[int] = None
    history_created_at: datetime

    class Config:
        from_attributes = True


class PurchaseOrderLineHistoryBase(BaseModel):
    line_id: Optional[int] = None
    tenant_id: int
    company_id: int
    document_type: str = Field(..., max_length=100)
    po_number: str = Field(..., max_length=30)
    version_no: int
    line_no: int
    po_id: int
    item_id: Optional[int] = None
    item_no: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    uom: Optional[str] = Field(None, max_length=50)
    qty_ordered: Optional[Decimal] = None
    qty_received: Optional[Decimal] = None
    qty_invoiced: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    po_item_rate: Optional[Decimal] = None
    rate_contract_item_rate: Optional[Decimal] = None
    tax_percent: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    discount_percent: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    total_amount: Optional[Decimal] = None
    delivery_date: Optional[date] = None
    lead_time_days: Optional[int] = None
    warehouse_id: Optional[int] = None
    pr_line_id: Optional[int] = None
    pr_requisition_no: Optional[str] = Field(None, max_length=30)
    pr_version_no: Optional[int] = None
    pr_line_no: Optional[int] = None
    line_status: Optional[str] = Field(None, max_length=50)
    remarks: Optional[str] = Field(None, max_length=500)
    attachment_count: Optional[int] = None
    revision_no: Optional[int] = 1
    history_action: Optional[str] = Field(None, max_length=50)
    history_reason: Optional[str] = Field(None, max_length=500)
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PurchaseOrderLineHistoryCreate(PurchaseOrderLineHistoryBase):
    history_created_by: Optional[int] = None


class PurchaseOrderLineHistoryUpdate(BaseModel):
    line_id: Optional[int] = None
    item_id: Optional[int] = None
    item_no: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    uom: Optional[str] = Field(None, max_length=50)
    qty_ordered: Optional[Decimal] = None
    qty_received: Optional[Decimal] = None
    qty_invoiced: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    po_item_rate: Optional[Decimal] = None
    rate_contract_item_rate: Optional[Decimal] = None
    tax_percent: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    discount_percent: Optional[Decimal] = None
    discount_amount: Optional[Decimal] = None
    total_amount: Optional[Decimal] = None
    delivery_date: Optional[date] = None
    lead_time_days: Optional[int] = None
    warehouse_id: Optional[int] = None
    pr_line_id: Optional[int] = None
    pr_requisition_no: Optional[str] = Field(None, max_length=30)
    pr_version_no: Optional[int] = None
    pr_line_no: Optional[int] = None
    line_status: Optional[str] = Field(None, max_length=50)
    remarks: Optional[str] = Field(None, max_length=500)
    attachment_count: Optional[int] = None
    revision_no: Optional[int] = None
    history_action: Optional[str] = Field(None, max_length=50)
    history_reason: Optional[str] = Field(None, max_length=500)
    updated_by: Optional[int] = None
    updated_at: Optional[datetime] = None


class PurchaseOrderLineHistoryResponse(PurchaseOrderLineHistoryBase):
    history_id: int
    history_created_by: Optional[int] = None
    history_created_at: datetime

    class Config:
        from_attributes = True
