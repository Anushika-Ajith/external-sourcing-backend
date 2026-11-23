from datetime import datetime
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

class Token(BaseModel):
    access_token: str
    token_type: str

# class TokenData(BaseModel):
#     email: Optional[EmailStr] = None
#     user_id: int | None = None  

#     role: Optional[str] = None
class TokenData(BaseModel):
    email: Optional[EmailStr] = None
    user_id: Optional[int] = None
    role: Optional[str] = None


class UserBase(BaseModel):
    email: EmailStr
    role: Optional[str] = None
    is_active: Optional[bool] = None

    class Config:
        from_attributes = True


class UserRegister(BaseModel):
    email: str
    password: str
    invite_code: str

class UserOut(BaseModel):
    user_id: int
    email: EmailStr
    role: str
    is_active: bool
    vendor_id: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True



class UserInDB(UserBase):
    id: int
    role: str
    is_active: bool
# Add to existing schemas
class InviteCreate(BaseModel):
    is_admin: bool = False

class InviteOut(BaseModel):
    id: int
    code: str
    is_admin: bool
    used: bool
    created_by: int
    created_at: datetime

    class Config:
        from_attributes = True
  
    class Config:
        from_attributes = True