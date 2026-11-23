from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WhatsAppStartRegistrationResponse(BaseModel):
    vendor_id: int
    vendor_registration_no: Optional[str] = None
    email: str
    mobile: str
    status: str
    vendor_created: bool
    message: str


class WhatsAppProvisionResponse(BaseModel):
    vendor_id: int
    email: str
    password: str
    message: str
    vendor_status: str
    access_token: str
    token_type: str = "bearer"
    next_step: str


class WhatsAppRegisterResponse(BaseModel):
    vendor_id: int
    vendor_created: bool
    email: Optional[str] = None
    password: Optional[str] = None
    access_token: Optional[str] = None
    vendor_status: str
    message: str
    ocr: Dict[str, Any] = Field(default_factory=dict)
    next_steps: List[str] = Field(default_factory=list)
    email_required: bool = False


class WhatsAppSetEmailResponse(BaseModel):
    vendor_id: int
    email: str
    vendor_status: str
    message: str


class VendorPhoneCheckResponse(BaseModel):
    exists: bool = Field(..., description="True if a vendor row matches this phone")
    mobile: str = Field(..., description="Phone number from the request")
    vendor_id: Optional[int] = None
    email: Optional[str] = None
    status: Optional[str] = None
    vendor_registration_no: Optional[str] = None
    has_login_account: bool = False
    message: str


class VendorEmailCheckResponse(BaseModel):
    exists: bool = Field(..., description="True if a vendor row matches this email")
    email: str = Field(..., description="Normalized email from the request")
    vendor_id: Optional[int] = None
    status: Optional[str] = None
    vendor_registration_no: Optional[str] = None
    company_name: Optional[str] = None
    has_login_account: bool = False
    message: str


class VendorSetupPasswordByEmailRequest(BaseModel):
    email: str = Field(..., description="Vendor business email")
    password: str = Field(..., min_length=8, description="New login password")


class VendorForgotPasswordRequest(BaseModel):
    email: str = Field(..., description="Vendor business email")


class VendorForgotPasswordResponse(BaseModel):
    message: str
    """Present when PASSWORD_RESET_INCLUDE_LINK is enabled (no SMTP delivery)."""
    reset_url: Optional[str] = None
    expires_in: Optional[int] = None


class VendorResetPasswordRequest(BaseModel):
    token: str = Field(..., description="Password-reset JWT from the reset link")
    password: str = Field(..., min_length=8, description="New login password")


class VendorResetPasswordResponse(BaseModel):
    message: str
    email: str
