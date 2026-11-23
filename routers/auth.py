# from fastapi import APIRouter, Depends, HTTPException, status
# from fastapi.security import OAuth2PasswordRequestForm
# from sqlalchemy.orm import Session
# from datetime import datetime, timedelta
# from typing import Annotated

# from services.auth_utils import (
#     authenticate_user,
#     create_access_token,
#     get_current_user,
#     get_current_active_user,
#     get_password_hash,
#     authenticate_vendor  
# )
# from schemas.auth import Token, UserRegister, UserInDB
# from models import User, Invite
# from database import get_db
# from config import settings

# router = APIRouter(tags=["Authentication"])

# @router.post("/register", response_model=UserInDB)
# async def register(
#     user_data: UserRegister, 
#     db: Session = Depends(get_db)
# ):
#     # Check if user exists
#     print("IN REGISTER...")
#     existing_user = db.query(User).filter(User.email == user_data.email).first()
#     if existing_user:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Email already registered"
#         )
    
   
    
#     role = "user"  # Default role
    
#     # Validate invite code - force boolean check
#     if bool(settings.REQUIRE_INVITE_CODE):
        
#         if not user_data.invite_code:
#             raise HTTPException(
#                 status_code=400,
#                 detail="Invite code is required"
#             )
        
#         # Check against admin invite code
#         if user_data.invite_code == settings.ADMIN_INVITE_CODE:
#             role = "admin"
#         # Check against static user invite code if configured
#         elif getattr(settings, "USER_INVITE_CODE", None) and user_data.invite_code == settings.USER_INVITE_CODE:
#             role = "user"
#         else:
#             # Check database for valid invite codes
#             invite = db.query(Invite).filter(
#                 Invite.code == user_data.invite_code,
#                 Invite.used == False,
#             ).first()
            
#             if not invite:
#                 raise HTTPException(
#                     status_code=400,
#                     detail="Invalid or expired invite code"
#                 )
            
#             role = "admin" if invite.is_admin else "user"
#             invite.used = True  # Mark invite as used
#             db.add(invite)
#     else:
#         role = "user"  # Default role if invite system is disabled

#     # Create user
#     hashed_password = get_password_hash(user_data.password)
#     print("HASHED PASSWORD: ",hashed_password)
#     db_user = User(
#         email=user_data.email,
#         hashed_password=hashed_password,
#         role=role,  # Use the determined role
#         is_active=True
#     )
    
#     db.add(db_user)
#     db.commit()
#     db.refresh(db_user)    
#     return db_user
# # @router.post("/token", response_model=Token)
# # async def login_for_access_token(
# #     form_data: OAuth2PasswordRequestForm = Depends(),
# #     db: Session = Depends(get_db)
# # ):
# #     user = authenticate_user(db, form_data.username, form_data.password)
# #     if not user:
# #         raise HTTPException(...)
    
# #     access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
# #     access_token = create_access_token(
# #         data={
# #             "sub": user.email,
# #             "user_id": user.id, 
# #             "role": user.role
# #         },
# #         expires_delta=access_token_expires
# #     )
# #     return {"access_token": access_token, "token_type": "bearer", "user_id": user.id}
# # @router.get("/users/me", response_model=UserInDB)
# # async def read_users_me(
# #     current_user: Annotated[User, Depends(get_current_active_user)]
# # ):
# #     return current_user
# @router.post("/token")
# async def login_for_access_token(
#     form_data: OAuth2PasswordRequestForm = Depends(),
#     db: Session = Depends(get_db)
# ):
#     user = authenticate_user(db, form_data.username, form_data.password)
#     if not user:
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Incorrect username or password",
#             headers={"WWW-Authenticate": "Bearer"},
#         )

#     access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
#     access_token = create_access_token(
#         data={
#             "sub": user.email,
#             "user_id": user.id,
#             "role": user.role,
#         },
#         expires_delta=access_token_expires,
#     )

#     return {
#         "access_token": access_token,
#         "token_type": "bearer",
#         "user": {
#             "id": user.id,
#             "email": user.email,
#             "role": user.role,
#             "is_active": user.is_active,
#             "vendor_id": user.vendor_id,
#         }
#     }

# @router.post("/users/activate/{user_id}")
# async def activate_user(
#     user_id: int,
#     current_user: Annotated[User, Depends(get_current_user)],  # Moved before default args
#     db: Session = Depends(get_db)
# ):
#     if current_user.role != "admin":
#         raise HTTPException(
#             status_code=status.HTTP_403_FORBIDDEN,
#             detail="Only admin can activate users"
#         )
    
#     user = db.query(User).filter(User.id == user_id).first()
#     if not user:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="User not found"
#         )
    
#     user.is_active = True
#     db.commit()
#     db.refresh(user)
#     return user


from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from typing import Annotated, Optional

from services.auth_utils import (
    authenticate_user,
    access_token_expires_delta,
    create_access_token,
    get_current_user,
    get_current_active_user,
    get_password_hash,
    authenticate_vendor,
)
from services.foundation_user import register_user_in_foundation
from services.handoff_tokens import (
    create_handoff_token,
    consume_handoff_token,
)
from services.company_scope import resolve_company_id
from services.db_sequences import create_user

from schemas.auth import Token, UserRegister, UserInDB, UserOut
from schemas.handoff import (
    CreateHandoffTokenRequest,
    CreateHandoffTokenResponse,
    ExchangeHandoffTokenRequest,
    ExchangeHandoffTokenResponse,
    ExchangedUser,
)
from models import User, Invite
from database import get_db
from config import settings

router = APIRouter(tags=["Authentication"])


# -------------------------------------------------------
# HANDOFF-TOKEN HELPERS
# -------------------------------------------------------
# Maps target_app → roles that are allowed to be handed off there.
# Use {"*"} to allow any authenticated, active user.
_TARGET_APP_ALLOWED_ROLES: dict[str, set[str]] = {
    # Vendors may open the internal sourcing app after handoff (e.g. Generate PCD).
    "internal_sourcing": {"admin", "user", "buyer", "vendor"},
    "buyer": {"admin", "user", "buyer"},
    "vendor": {"vendor"},
}


def _user_has_access_to_target_app(user: User, target_app: str) -> bool:
    allowed = _TARGET_APP_ALLOWED_ROLES.get(target_app)
    if allowed is None:
        return False
    if "*" in allowed:
        return True
    role = (getattr(user, "role", "") or "").strip().lower()
    return role in allowed


def _handoff_claim_int(value: object) -> Optional[int]:
    """Parse tenant_id / company_id from handoff JWT claims."""
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------
# REGISTER USER
# -------------------------------------------------------
@router.post("/register", response_model=UserOut)
async def register(
    user_data: UserRegister,
    db: Session = Depends(get_db)
):
    # Check if user exists
    existing_user = db.query(User).filter(User.email == user_data.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )

    role = "user"

    # Invite code validation (if enabled)
    if bool(settings.REQUIRE_INVITE_CODE):

        if not user_data.invite_code:
            raise HTTPException(
                status_code=400,
                detail="Invite code is required"
            )

        # Admin invite by static code
        if user_data.invite_code == settings.ADMIN_INVITE_CODE:
            role = "admin"

        # Static user invite
        elif getattr(settings, "USER_INVITE_CODE", None) and user_data.invite_code == settings.USER_INVITE_CODE:
            role = "user"

        else:
            # Invite from DB
            invite = db.query(Invite).filter(
                Invite.code == user_data.invite_code,
                Invite.used == False
            ).first()

            if not invite:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid or expired invite code"
                )

            role = "admin" if invite.is_admin else "user"
            invite.used = True
            db.add(invite)

    # Create user in foundation_db first (existing Nest POST /auth/register)
    register_user_in_foundation(
        user_data.email,
        user_data.password,
    )

    # Create local collab user
    hashed_password = get_password_hash(user_data.password)
    db_user = create_user(
        db,
        email=user_data.email,
        hashed_password=hashed_password,
        role=role,
        is_active=True,
    )
    return db_user


# -------------------------------------------------------
# LOGIN / TOKEN
# -------------------------------------------------------
@router.post("/token")
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = access_token_expires_delta()

    access_token = create_access_token(
        data={
            "sub": user.email,
            "user_id": user.user_id,   # FIXED
            "role": user.role,
        },
        expires_delta=access_token_expires,
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "user_id": user.user_id,   # FIXED
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "vendor_id": user.vendor_id,
        }
    }


# -------------------------------------------------------
# ACTIVATE USER (ADMIN ONLY)
# -------------------------------------------------------
@router.post("/users/activate/{user_id}")
async def activate_user(
    user_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Session = Depends(get_db)
):
    # Role gate removed — permissions API owns access.

    # FIXED: replaced User.id → User.user_id
    user = db.query(User).filter(User.user_id == user_id).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    user.is_active = True
    db.commit()
    db.refresh(user)
    return user
@router.get("/users/me", response_model=UserOut)
async def read_users_me(
    current_user: User = Depends(get_current_active_user)
):
    return current_user


# -------------------------------------------------------
# HANDOFF-TOKEN FLOW (Foundation / Auth API)
# -------------------------------------------------------
#
# STEP 1 (Vendor App): user clicks "Generate PCD".
# STEP 2 (this endpoint): Foundation API validates the JWT, that the user is
#         active, and that the user is allowed to be handed off to `target_app`.
# STEP 3 (this endpoint): Foundation API mints a short-lived (≤60s) handoff
#         JWT signed with the same key, returns it to the caller.
# STEP 4 (Vendor App): redirects the browser to
#         `http://<internal-app>/auth-bridge?handoff=<token>` so the Internal
#         App's bridge page can exchange it.
# STEP 5 (Internal App): bridge calls POST /auth/exchange-handoff-token.
# STEP 6 (this endpoint): Foundation API validates the handoff token (signature
#         + expiry + one-time-use via Redis JTI marker) and returns a real,
#         longer-lived access token.
# STEP 7 (Internal App): stores `access_token` in localStorage and continues.
#
@router.post(
    "/create-handoff-token",
    response_model=CreateHandoffTokenResponse,
    summary="Mint a short-lived handoff token for the current user",
)
async def create_handoff_token_route(
    body: CreateHandoffTokenRequest,
    current_user: User = Depends(get_current_active_user),
):
    if not _user_has_access_to_target_app(current_user, body.target_app):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not have access to {body.target_app!r}",
        )

    token, ttl = create_handoff_token(
        user_id=int(current_user.user_id),
        email=current_user.email,
        role=current_user.role,
        target_app=body.target_app,
        return_to=body.return_to,
        tenant_id=body.tenant_id,
        company_id=resolve_company_id(body.company_id),
    )

    return CreateHandoffTokenResponse(
        handoff_token=token,
        target_app=body.target_app,
        return_to=body.return_to,
        expires_in=ttl,
    )


@router.post(
    "/exchange-handoff-token",
    response_model=ExchangeHandoffTokenResponse,
    summary="Exchange a handoff token for a real access token",
)
async def exchange_handoff_token_route(
    body: ExchangeHandoffTokenRequest,
    db: Session = Depends(get_db),
):
    payload = consume_handoff_token(body.handoff_token)

    raw_user_id = payload.get("user_id", payload.get("sub"))
    try:
        user_id = int(raw_user_id) if raw_user_id is not None else None
    except (TypeError, ValueError):
        user_id = None

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Handoff token missing user_id/sub",
        )

    user = db.query(User).filter(User.user_id == user_id).first()
    if user is None or user.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User is no longer active",
        )

    target_app = payload.get("target_app") or payload.get("targetApp")
    return_to = payload.get("return_to")

    tenant_id = _handoff_claim_int(payload.get("tenant_id"))
    if tenant_id is None:
        tenant_id = _handoff_claim_int(payload.get("tenantId"))
    company_id = _handoff_claim_int(payload.get("company_id"))
    if company_id is None:
        company_id = _handoff_claim_int(payload.get("companyId"))
    company_id = resolve_company_id(company_id)

    if not _user_has_access_to_target_app(user, str(target_app)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not have access to {target_app!r}",
        )

    # Mint a regular access token (no exp when ACCESS_TOKEN_EXPIRE_MINUTES <= 0).
    access_token_expires = access_token_expires_delta()
    access_claims: dict = {
        "sub": user.email,
        "user_id": int(user.user_id),
        "email": user.email,
        "role": user.role,
        "target_app": target_app,
    }
    if tenant_id is not None:
        access_claims["tenant_id"] = tenant_id
    if company_id is not None:
        access_claims["company_id"] = company_id

    access_token = create_access_token(
        data=access_claims,
        expires_delta=access_token_expires,
    )

    return ExchangeHandoffTokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=(
            int(access_token_expires.total_seconds()) if access_token_expires else 0
        ),
        target_app=target_app,
        return_to=return_to,
        tenant_id=tenant_id,
        company_id=company_id,
        user=ExchangedUser(
            user_id=int(user.user_id),
            email=user.email,
            role=user.role,
            is_active=bool(user.is_active),
            vendor_id=getattr(user, "vendor_id", None),
        ),
    )
