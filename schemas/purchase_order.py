from pydantic import BaseModel, Field, model_validator
from typing import Any, Dict, List, Optional
from datetime import date, datetime
from decimal import Decimal


class PurchaseOrderHeaderBase(BaseModel):
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
    approval_status: Optional[str] = Field("DRAFT", max_length=50)
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
    document_hash: Optional[str] = Field(None, max_length=64)
    business_hash: Optional[str] = Field(None, max_length=64)
    revision_no: Optional[int] = 1
    current_revision: Optional[bool] = True
    attachment_path: Optional[str] = None
    current_editor: Optional[str] = Field(
        None,
        max_length=255,
        description="Who currently holds edit lock / turn (e.g. BUYER, VENDOR, user id)",
    )


class PurchaseOrderHeaderCreate(PurchaseOrderHeaderBase):
    created_by: Optional[int] = None

    @model_validator(mode="after")
    def _default_current_editor(self):
        """Omitted / null / blank current_editor → BUYER."""
        ce = self.current_editor
        if ce is None or (isinstance(ce, str) and not ce.strip()):
            self.current_editor = "BUYER"
        elif isinstance(ce, str):
            self.current_editor = ce.strip()
        return self


class PurchaseOrderHeaderUpdate(BaseModel):
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
    current_editor: Optional[str] = Field(None, max_length=255)
    updated_by: Optional[int] = None


class PurchaseOrderHeaderResponse(PurchaseOrderHeaderBase):
    po_id: int
    document_hash: Optional[str] = None
    business_hash: Optional[str] = None
    revision_no: Optional[int] = None
    current_revision: Optional[bool] = None
    attachment_path: Optional[str] = None
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# --- Purchase order lines ---


class PurchaseOrderLineBase(BaseModel):
    line_no: int
    item_id: Optional[int] = None
    item_no: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    uom: Optional[str] = Field(None, max_length=50)
    product_fingerprint: Optional[Dict[str, Any]] = Field(
        None,
        description="Copied from vendor_items.product_fingerprint on PO create",
    )
    qty_ordered: Optional[Decimal] = None
    suggested_qty: Optional[Decimal] = None
    buyer_qty: Optional[Decimal] = None
    supplier_qty: Optional[Decimal] = None
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


class PurchaseOrderLineCreate(PurchaseOrderLineBase):
    """Body for POST; composite PO key comes from path. po_id optional (filled from header)."""

    po_id: Optional[int] = None
    created_by: Optional[int] = None


class PurchaseOrderLineNestedCreate(PurchaseOrderLineBase):
    """Line payload for POST /purchase-order/complete (no po_id; set from header after insert)."""

    created_by: Optional[int] = None


class PurchaseOrderCompleteCreate(PurchaseOrderHeaderCreate):
    """Create PO header and all lines in one transaction."""

    lines: List[PurchaseOrderLineNestedCreate] = Field(default_factory=list)


class PurchaseOrderLineUpdate(BaseModel):
    po_id: Optional[int] = None
    item_id: Optional[int] = None
    item_no: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    uom: Optional[str] = Field(None, max_length=50)
    product_fingerprint: Optional[Dict[str, Any]] = None
    qty_ordered: Optional[Decimal] = None
    suggested_qty: Optional[Decimal] = None
    buyer_qty: Optional[Decimal] = None
    supplier_qty: Optional[Decimal] = None
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
    updated_by: Optional[int] = None


class PurchaseOrderLineResponse(PurchaseOrderLineBase):
    tenant_id: int
    company_id: int
    document_type: str
    po_number: str
    version_no: int
    line_id: int
    po_id: int
    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PurchaseOrderFullResponse(PurchaseOrderHeaderResponse):
    """Header plus lines (e.g. after complete create)."""

    lines: List[PurchaseOrderLineResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True
