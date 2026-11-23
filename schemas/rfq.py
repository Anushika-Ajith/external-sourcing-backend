from pydantic import BaseModel, model_validator
from typing import List, Optional
from datetime import date, datetime
from decimal import Decimal

class RFQLineBase(BaseModel):
    line_no: int
    item_id: Optional[int] = None
    item_description: Optional[str] = None
    uom: Optional[str] = None
    quantity: Decimal
    estimated_rate: Optional[Decimal] = None
    estimated_amount: Optional[Decimal] = None
    required_date: Optional[date] = None
    remarks: Optional[str] = None


class RFQLineCreate(RFQLineBase):
    created_by: Optional[int] = None


class RFQLineUpdate(BaseModel):
    item_id: Optional[int] = None
    item_description: Optional[str] = None
    uom: Optional[str] = None
    quantity: Optional[Decimal] = None
    estimated_rate: Optional[Decimal] = None
    estimated_amount: Optional[Decimal] = None
    required_date: Optional[date] = None
    remarks: Optional[str] = None
    updated_by: Optional[int] = None


class RFQLineResponse(RFQLineBase):
    tenant_id: int
    company_id: int
    document_type: str
    rfq_no: str
    version_no: int
    created_by: int
    updated_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True

class RFQVendorBase(BaseModel):
    vendor_id: int
    status: Optional[str] = None
    current_editor: Optional[str] = None


class RFQVendorCreate(RFQVendorBase):
    created_by: Optional[int] = None

    @model_validator(mode="after")
    def _default_current_editor(self):
        """JSON null / omitted optional becomes None; persist as BUYER, not SQL NULL."""
        ce = self.current_editor
        if ce is None or (isinstance(ce, str) and not ce.strip()):
            self.current_editor = "BUYER"
        return self


class RFQVendorUpdate(BaseModel):
    status: Optional[str] = None
    current_editor: Optional[str] = None
    updated_by: Optional[int] = None


class RFQVendorResponse(RFQVendorBase):
    tenant_id: int
    company_id: int
    document_type: str
    rfq_no: str
    version_no: int
    created_by: int
    updated_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class RFQVendorMatchedItemRate(BaseModel):
    item_id: Optional[int] = None
    erp_code: Optional[str] = None
    vendor_item_id: Optional[int] = None
    rate: Optional[Decimal] = None
    currency: Optional[str] = None


class RFQVendorWorkflowStateResponse(BaseModel):
    """Workflow fields for a vendor row on an RFQ."""

    tenant_id: int
    company_id: int
    document_type: str
    rfq_no: str
    version_no: int
    vendor_id: int
    status: Optional[str] = None
    current_editor: Optional[str] = None
    updated_by: Optional[int] = None
    updated_at: Optional[datetime] = None
    matched_item_rates: List[RFQVendorMatchedItemRate] = []

    class Config:
        orm_mode = True


class RFQVendorWorkflowPatchRequest(BaseModel):
    """
    Update `status` and/or `current_editor` on `rfq_vendor` matched by `vendor_id` and `rfq_no`.
    If more than one row matches, pass tenant_id, company_id, document_type, and optionally version_no.
    """

    vendor_id: int
    rfq_no: str
    status: Optional[str] = None
    current_editor: Optional[str] = None
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    document_type: Optional[str] = None
    version_no: Optional[int] = None
    updated_by: Optional[int] = None


class RFQHeaderBase(BaseModel):
    tenant_id: int
    company_id: int
    document_type: str
    rfq_no: str
    version_no: int = 1
    rfq_date: date
    department_id: Optional[int] = None
    requester_id: int
    status: Optional[str] = None
    priority: Optional[str] = None
    source_type: Optional[str] = None
    remarks: Optional[str] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    rejected_by: Optional[int] = None
    rejected_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    document_number: Optional[str] = None
    current_editor: Optional[str] = None


class RFQHeaderCreate(RFQHeaderBase):
    """
    Schema for creating a complete RFQ with header, lines, and vendors.
    created_by is required (no authentication, so must be provided explicitly).
    rfq_no is assigned by the Nest next-numbering service — omit or leave null.
    """
    rfq_no: Optional[str] = None
    created_by: int
    lines: Optional[List[RFQLineCreate]] = []
    vendors: Optional[List[RFQVendorCreate]] = []

    @model_validator(mode="after")
    def _default_header_current_editor(self):
        """Omitted / null / blank header current_editor → BUYER (matches rfq_vendor default)."""
        ce = self.current_editor
        if ce is None or (isinstance(ce, str) and not ce.strip()):
            self.current_editor = "BUYER"
        elif isinstance(ce, str):
            self.current_editor = ce.strip()
        return self


class RFQHeaderUpdate(BaseModel):
    rfq_date: Optional[date] = None
    department_id: Optional[int] = None
    requester_id: Optional[int] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    source_type: Optional[str] = None
    remarks: Optional[str] = None
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    rejected_by: Optional[int] = None
    rejected_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    document_number: Optional[str] = None
    current_editor: Optional[str] = None
    updated_by: Optional[int] = None


class RFQHeaderResponse(RFQHeaderBase):
    rfq_id: int
    created_by: int
    updated_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    lines: Optional[List[RFQLineResponse]] = []
    vendors: Optional[List[RFQVendorResponse]] = []

    class Config:
        orm_mode = True


class PendingRFQCountResponse(BaseModel):
    """Count of RFQ headers with status PENDING."""

    pending_count: int


class SubmittedRFQCountResponse(BaseModel):
    """Count of RFQ headers with status SUBMITTED."""

    submitted_count: int


class VendorRFQWorkbench(BaseModel):
    rfq_no: str
    version_no: int
    rfq_date: date
    due_date: Optional[date]      # ✅ add this
    document_number: Optional[str]
    status: Optional[str]
    vendor_name: Optional[str]
    buyer_name: Optional[str]     # ✅ add this

    class Config:
        orm_mode = True


class RFQWorkflowStateResponse(BaseModel):
    """Workflow snapshot from RFQ header + last editor (User.email for updated_by)."""

    tenant_id: int
    company_id: int
    document_type: str
    rfq_no: str
    version_no: int
    rfq_id: int
    status: Optional[str] = None
    updated_by: Optional[int] = None
    editor_email: Optional[str] = None
    updated_at: Optional[datetime] = None


class FreezedVendorRateItemVendor(BaseModel):
    vendor_id: int
    rate: Optional[Decimal] = None
    currency: Optional[str] = None


class FreezedVendorRateItem(BaseModel):
    item_id: Optional[int] = None
    item_description: Optional[str] = None
    uom: Optional[str] = None
    rfq_line_rate: Optional[Decimal] = None
    vendors: List[FreezedVendorRateItemVendor] = []


class FreezedVendorRatesItemWiseResponse(BaseModel):
    tenant_id: int
    company_id: int
    document_type: str
    rfq_no: str
    version_no: int
    items: List[FreezedVendorRateItem] = []


class FreezedRFQItemWiseResponse(BaseModel):
    tenant_id: int
    company_id: int
    document_type: str
    rfq_no: str
    version_no: int
    items: List[FreezedVendorRateItem] = []


class FreezedVendorRatesAllRFQsResponse(BaseModel):
    tenant_id: int
    company_id: int
    document_type: str
    rfqs: List[FreezedRFQItemWiseResponse] = []
