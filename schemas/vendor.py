from pydantic import BaseModel, Field, EmailStr
from datetime import datetime, date
from decimal import Decimal
from typing import Optional, List, Dict, Any
from datetime import date

# Vendor Base Schema
class VendorBase(BaseModel):
    email: EmailStr = Field(..., description="Business email address")
    mobile: str = Field(..., description="Business mobile number")
    company_name: Optional[str] = Field(None, description="Company name")
    vendor_type: Optional[str] = Field(None, description="Vendor type (Service, Product, Trading, Mixed)")
    gst_number: Optional[str] = Field(None, description="GST number")
    pan_number: Optional[str] = Field(None, description="PAN number")
    contact_person: Optional[str] = Field(None, description="Contact person name")
    address_line1: Optional[str] = Field(None, description="Address line 1")
    address_line2: Optional[str] = Field(None, description="Address line 2")
    city: Optional[str] = Field(None, description="City")
    state: Optional[str] = Field(None, description="State")
    country: Optional[str] = Field("India", description="Country")
    postal_code: Optional[str] = Field(None, description="Postal code")
    is_branch: Optional[bool] = Field(False, description="Is branch vendor")
    parent_vendor_id: Optional[int] = Field(None, description="Parent vendor ID if branch")
    status: Optional[str] = Field("pending", description="Vendor status")
    company_code: Optional[str] = Field(None, description="Company code")
    tenant_id: Optional[int] = Field(1, description="Tenant ID")
    company_id: Optional[int] = Field(3, description="Company ID")
    vendor_registration_no: Optional[str] = Field(
        None,
        description="Nest next-numbering registration id; also used as vendors.vendor_code (FK for items)",
    )
    vendor_code: Optional[str] = Field(
        None,
        max_length=20,
        description="Unique vendor business code — same as vendor_registration_no from next numbering",
    )
    created_by_a: Optional[str] = Field(None, description="Audit created by")
    created_ip_a: Optional[str] = Field(None, description="Audit created IP")
    modified_by_a: Optional[str] = Field(None, description="Audit modified by")
    modified_ip_a: Optional[str] = Field(None, description="Audit modified IP")

    class Config:
        from_attributes = True


class VendorCreate(VendorBase):
    pass


class VendorUpdate(BaseModel):
    email: Optional[EmailStr] = None
    mobile: Optional[str] = None
    company_name: Optional[str] = None
    vendor_type: Optional[str] = None
    gst_number: Optional[str] = None
    pan_number: Optional[str] = None
    contact_person: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    is_branch: Optional[bool] = None
    parent_vendor_id: Optional[int] = None
    status: Optional[str] = None
    company_code: Optional[str] = None
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    vendor_registration_no: Optional[str] = None
    vendor_code: Optional[str] = Field(None, max_length=20)
    created_by_a: Optional[str] = None
    created_ip_a: Optional[str] = None
    modified_by_a: Optional[str] = None
    modified_ip_a: Optional[str] = None

    class Config:
        from_attributes = True


class Vendor(VendorBase):
    vendor_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by_a: Optional[str] = None
    created_on_a: Optional[datetime] = None
    created_ip_a: Optional[str] = None
    modified_by_a: Optional[str] = None
    modified_on_a: Optional[datetime] = None
    modified_ip_a: Optional[str] = None

    class Config:
        from_attributes = True


class VendorDirectoryItem(BaseModel):
    """Compact vendor row: name, GST category, status (see field descriptions for DB mapping)."""

    vendor_id: int
    vendor_name: Optional[str] = Field(None, description="Vendor display name (vendors.company_name)")
    vendor_code: Optional[str] = Field(
        None,
        description="Business / next-numbering code (vendors.vendor_code or vendor_registration_no)",
    )
    gst_category: Optional[str] = Field(
        None,
        description="GST classification; currently mapped from vendors.vendor_type (e.g. Service, Product, Trading, Mixed).",
    )
    status: Optional[str] = Field(None, description="Vendor lifecycle status (vendors.status)")
    # Optional: latest vendor_rates.rate for this vendor + the group's erp_code
    rate: Optional[float] = Field(
        None,
        description="Latest contract rate for this vendor on the requested product code",
    )
    uom: Optional[str] = Field(
        None,
        description="UOM from the matched vendor_items row (when rate is resolved)",
    )
    currency: Optional[str] = Field(
        None,
        description="Currency from the matched vendor_rates row (default INR)",
    )
    valid_from: Optional[date] = Field(
        None,
        description="Rate contract valid-from (vendor_rates.valid_from)",
    )
    valid_to: Optional[date] = Field(
        None,
        description="Rate contract expiry / valid-to (vendor_rates.valid_to)",
    )

    class Config:
        from_attributes = True


class VendorsForErpCodeOut(BaseModel):
    """Vendors that carry a given product code via vendor_items."""

    erp_code: str = Field(
        ...,
        description=(
            "Requested stock.product_code; matched against vendor_items "
            "erp_code, company_code, or vendor_product_code, then expanded "
            "to all vendors sharing the same internal erp/company code"
        ),
    )
    vendors: List[VendorDirectoryItem] = Field(
        default_factory=list,
        description="Distinct vendors linked to that product code",
    )


# Vendor Bank Schemas
class VendorBankBase(BaseModel):
    bank_name: str = Field(..., description="Name of the bank")
    account_number: str = Field(..., description="Bank account number")
    ifsc_code: str = Field(..., description="IFSC code of the bank branch")
    branch_name: str = Field(..., description="Name of the bank branch")
    account_type: str = Field(..., description="Type of account (Savings/Current/OD)")
    is_primary: bool = Field(default=False, description="Whether this is the primary bank account")

    class Config:
        from_attributes = True


class VendorBankCreate(VendorBankBase):
    pass


class VendorBank(VendorBankBase):
    id: int
    vendor_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Vendor Item Schemas
class VendorItemBase(BaseModel):
    item_name: Optional[str] = Field(None, description="Name of the item")
    item_type: Optional[str] = Field(None, description="Type of the item (Service/Product)")
    uom: Optional[str] = Field(None, description="Unit of measurement (e.g., kg, pcs, box)")
    description: Optional[str] = Field(None, description="Item description")
    item_id: Optional[int] = Field(
        None, description="Buyer/master catalog item id (not vendor_items.id)"
    )
    erp_code: Optional[str] = Field(None, description="ERP item code")
    vendor_product_code: Optional[str] = Field(None, description="Vendor product code")
    barcode: Optional[str] = Field(None, description="Unique product barcode")
    product_fingerprint: Optional[Dict[str, Any]] = Field(
        None, description="Structured product identity fingerprint (JSON)"
    )
    brand: Optional[str] = Field(None, description="Product brand")
    manufacturer: Optional[str] = Field(
        None, description="Product manufacturer"
    )
    product_name: Optional[str] = Field(None, description="Display product name")
    category: Optional[str] = Field(None, description="Product category")
    consumer_unit: Optional[str] = Field(None, description="Consumer/sell unit")
    consumer_unit_size: Optional[str] = Field(
        None, description="Consumer unit size (e.g. 500ml)"
    )
    purchase_unit: Optional[str] = Field(None, description="Purchase / order unit")
    pack_size: Optional[Decimal] = Field(None, description="Units per purchase pack")
    conversion_factor: Optional[Decimal] = Field(
        None, description="Conversion factor between purchase and consumer units"
    )
    uom_p: Optional[str] = Field(
        None, description="Primary unit of measure (CHAR(3))"
    )
    uom_s: Optional[str] = Field(
        None, description="Secondary unit of measure (CHAR(3))"
    )
    conv_f: Optional[Decimal] = Field(
        None, description="Conversion factor from primary UOM (from)"
    )
    conv_t: Optional[Decimal] = Field(
        None, description="Conversion factor to secondary UOM (to)"
    )
    status: Optional[str] = Field(
        "Active", description="Item status (Active/Inactive/etc.)"
    )
    vendor_code: Optional[str] = Field(
        None, description="Owning vendor business code (vendors.vendor_code)"
    )
    tenant_id: Optional[str] = Field(None, description="Tenant identifier")
    company_code: Optional[str] = Field(None, description="Company code")

    class Config:
        from_attributes = True


class VendorItemCreate(VendorItemBase):
    pass


class VendorItemUpdate(BaseModel):
    """Partial update for vendor_items."""

    item_name: Optional[str] = None
    item_type: Optional[str] = None
    uom: Optional[str] = None
    description: Optional[str] = None
    item_id: Optional[int] = None
    erp_code: Optional[str] = Field(None)
    vendor_product_code: Optional[str] = Field(None)
    barcode: Optional[str] = Field(None)
    product_fingerprint: Optional[Dict[str, Any]] = None
    brand: Optional[str] = Field(None)
    manufacturer: Optional[str] = Field(None)
    product_name: Optional[str] = Field(None)
    category: Optional[str] = Field(None)
    consumer_unit: Optional[str] = Field(None)
    consumer_unit_size: Optional[str] = Field(None)
    purchase_unit: Optional[str] = Field(None)
    pack_size: Optional[Decimal] = None
    conversion_factor: Optional[Decimal] = None
    uom_p: Optional[str] = Field(None)
    uom_s: Optional[str] = Field(None)
    conv_f: Optional[Decimal] = None
    conv_t: Optional[Decimal] = None
    status: Optional[str] = Field(None)
    vendor_code: Optional[str] = Field(None)
    tenant_id: Optional[str] = Field(None)
    company_code: Optional[str] = Field(None)

    class Config:
        from_attributes = True


class VendorItem(VendorItemBase):
    id: int
    vendor_id: int
    created_by: Optional[str] = None
    created_ip: Optional[str] = None
    modified_by: Optional[str] = None
    modified_ip: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Vendor Document Schemas
class VendorDocumentBase(BaseModel):
    document_type: str = Field(..., description="Type of document (GST, PAN, Registration, MSME, Other)")
    document_number: Optional[str] = Field(None, description="Document number if applicable")
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None

    class Config:
        from_attributes = True


class VendorDocumentCreate(VendorDocumentBase):
    pass


class VendorDocument(VendorDocumentBase):
    id: int
    vendor_id: int
    document_path: Optional[str] = None
    status: str
    uploaded_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Vendor Rate Schemas
class VendorRateBase(BaseModel):
    item_id: int = Field(..., description="ID of the item this rate applies to")
    vendor_id: Optional[int] = Field(
        None,
        description="Owning vendor (same as vendor_items.vendor_id)",
    )
    rate: Optional[float] = Field(None, description="Rate for the item")
    currency: Optional[str] = Field("INR", description="Currency")
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    status: Optional[str] = Field("pending", description="Rate status")

    class Config:
        from_attributes = True


class VendorRateCreate(VendorRateBase):
    pass


class VendorRate(VendorRateBase):
    id: int
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class VendorRateCreateBody(BaseModel):
    """Create/update rate without item_id (supplied via URL)."""

    rate: Optional[float] = Field(None, description="Rate for the item")
    currency: Optional[str] = Field("INR", description="Currency code")
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    status: Optional[str] = Field("pending", description="Rate status")


class VendorItemWithRateCreate(VendorItemCreate):
    """Create a vendor_items row and one vendor_rates row in a single request."""

    rate: VendorRateCreateBody = Field(
        ...,
        description="Initial rate for the new item (item_id is set server-side)",
    )


class VendorItemResponse(VendorItemBase):
    """Item as returned from API, including nested rates."""

    id: int
    vendor_id: int
    created_by: Optional[str] = None
    created_ip: Optional[str] = None
    modified_by: Optional[str] = None
    modified_ip: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    rates: List[VendorRate] = Field(default_factory=list)

    class Config:
        from_attributes = True


# OTP Verification Schema
class OTPVerify(BaseModel):
    email: EmailStr
    mobile: str
    otp_code: str


# Audit Log Schema (for responses)
class AuditLog(BaseModel):
    id: int
    table_name: str
    record_id: int
    action: str
    field_name: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    changed_by: int
    changed_at: datetime

    class Config:
        from_attributes = True


# Schemas matching current SQLAlchemy models.Vendor for profile endpoints
class VendorProfileUpdate(BaseModel):
    email: Optional[EmailStr] = None
    mobile: Optional[str] = None
    company_name: Optional[str] = None
    vendor_type: Optional[str] = None
    gst_number: Optional[str] = None
    pan_number: Optional[str] = None
    contact_person: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    is_branch: Optional[bool] = None
    parent_vendor_id: Optional[int] = None
    status: Optional[str] = None
    company_code: Optional[str] = None
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    vendor_registration_no: Optional[str] = None
    vendor_code: Optional[str] = Field(None, max_length=20)
    created_by_a: Optional[str] = None
    created_ip_a: Optional[str] = None
    modified_by_a: Optional[str] = None
    modified_ip_a: Optional[str] = None

    class Config:
        from_attributes = True


class VendorProfileOut(BaseModel):
    vendor_id: int
    email: Optional[str] = None
    mobile: Optional[str] = None
    company_name: Optional[str] = None
    vendor_type: Optional[str] = None
    gst_number: Optional[str] = None
    pan_number: Optional[str] = None
    contact_person: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    is_branch: Optional[bool] = None
    parent_vendor_id: Optional[int] = None
    status: Optional[str] = None
    company_code: Optional[str] = None
    tenant_id: Optional[int] = None
    company_id: Optional[int] = None
    vendor_registration_no: Optional[str] = None
    vendor_code: Optional[str] = None
    created_by_a: Optional[str] = None
    created_on_a: Optional[datetime] = None
    created_ip_a: Optional[str] = None
    modified_by_a: Optional[str] = None
    modified_on_a: Optional[datetime] = None
    modified_ip_a: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class VendorRegistrationUpdate(VendorProfileUpdate):
    # Plain str so Save Draft accepts partial emails; the route validates on Submit.
    email: Optional[str] = None


# All optional so Save Draft accepts half-filled rows; the route validates on Submit.
class VendorRegistrationBank(BaseModel):
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    ifsc_code: Optional[str] = None
    branch_name: Optional[str] = None
    account_type: Optional[str] = None
    is_primary: Optional[bool] = False


class VendorRegistrationDocument(BaseModel):
    document_type: Optional[str] = None
    document_number: Optional[str] = None
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None


# Compound application schema used by complete-registration endpoint
class VendorApplication(BaseModel):
    vendor: VendorRegistrationUpdate
    banks: List[VendorRegistrationBank] = []
    items: List[VendorItemBase] = []
    documents: List[VendorRegistrationDocument] = []

    class Config:
        from_attributes = True


# Detailed vendor schema for admin views
class VendorDetailOut(VendorProfileOut):
    banks: Optional[List[VendorBank]] = []
    documents: Optional[List[VendorDocument]] = []
    items: Optional[List[VendorItem]] = []

    class Config:
        from_attributes = True


class VendorWithProductsAndRatesOut(VendorProfileOut):
    """Vendor with products (vendor_items) and nested vendor_rates on each product."""

    items: List[VendorItemResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class VendorWithBanksProductsAndRatesOut(VendorProfileOut):
    """Vendor with bank accounts and products (with nested vendor_rates)."""

    banks: List[VendorBank] = Field(default_factory=list)
    items: List[VendorItemResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class VendorRegistrationReviewOut(BaseModel):
    """Master + banks + items (with rates) + documents for onboarding review / prefill."""

    vendor: VendorProfileOut
    banks: List[VendorBank] = Field(default_factory=list)
    items: List[VendorItemResponse] = Field(default_factory=list)
    documents: List[VendorDocument] = Field(default_factory=list)


class VendorStatusUpdate(BaseModel):
    """PATCH /vendor/{vendor_id}/status"""

    status: str = Field(..., min_length=1, max_length=20, description="New vendor lifecycle status")


class MediaAttachmentCreate(BaseModel):
    media_name: str
    media_type: str
    media_url: str
    document_type_id: Optional[int] = None
    context_registry_id: int
    ocr_raw_text: Optional[Dict] = None
    cleaned_text: Optional[Dict] = None
    tenant_id: int
    company_id: int
    effective_date: Optional[date] = None
    expiry_date: Optional[date] = None
    created_by: Optional[str] = None
    
class MediaAttachmentResponse(MediaAttachmentCreate):
    media_attachment_id: int

    class Config:
        from_attributes = True
