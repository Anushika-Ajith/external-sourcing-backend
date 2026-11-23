# In backend/routers/otp.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_
from datetime import datetime, timedelta
import random
import string

from database import get_db
from models import OTP, Vendor
from schemas.otp import OTPRequest, OTPVerify, OTPOut

router = APIRouter(tags=["OTP"])

def generate_otp(length=6):
    return ''.join(random.choices(string.digits, k=length))

@router.post("/vendor/request", response_model=OTPOut)
async def request_vendor_otp(
    otp_request: OTPRequest,
    db: Session = Depends(get_db)
):
    # Check if vendor already exists with completed registration
    existing_vendor = db.query(Vendor).filter(
        or_(
            Vendor.email == otp_request.email,
            Vendor.mobile == otp_request.mobile
        ),
        Vendor.status != "pending_verification"
    ).first()
    
    if existing_vendor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vendor already registered"
        )
    
    # Generate OTP
    otp_code = generate_otp()
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    
    # Check if there's an existing unused OTP for this email/mobile
    existing_otp = db.query(OTP).filter(
        OTP.email == otp_request.email,
        OTP.mobile == otp_request.mobile,
        OTP.is_used == False,
        OTP.expires_at > datetime.utcnow()
    ).first()
    
    if existing_otp:
        # Return existing OTP instead of creating a new one
        print(f"Using existing OTP for {otp_request.email}: {existing_otp.otp_code}")
        return existing_otp
    
    db_otp = OTP(
        email=otp_request.email,
        mobile=otp_request.mobile,
        otp_code=otp_code,
        otp_type="vendor_registration",
        expires_at=expires_at
    )
    
    db.add(db_otp)
    db.commit()
    db.refresh(db_otp)
    
    print(f"OTP for {otp_request.email}: {otp_code}")
    return db_otp

@router.post("/verify")
async def verify_otp(otp_verify: OTPVerify, db: Session = Depends(get_db)):
    otp = db.query(OTP).filter(
        OTP.email == otp_verify.email,
        OTP.mobile == otp_verify.mobile,
        OTP.otp_code == otp_verify.otp_code,
        OTP.expires_at > datetime.utcnow(),
        OTP.is_used == False
    ).first()
    
    if not otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired OTP"
        )
    
    otp.is_used = True
    db.commit()
    
    return {"message": "OTP verified successfully", "verified": True}