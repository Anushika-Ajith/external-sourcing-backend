from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import Optional

class OTPRequest(BaseModel):
    email: EmailStr
    mobile: str

class OTPVerify(BaseModel):
    email: EmailStr
    mobile: str
    otp_code: str

class OTPOut(BaseModel):
    id: int
    email: str
    mobile: str
    otp_type: str
    expires_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True