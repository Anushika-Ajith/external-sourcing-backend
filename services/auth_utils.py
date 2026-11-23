# from jose import JWTError, jwt
# from passlib.context import CryptContext
# from datetime import datetime, timedelta
# from fastapi import Depends, HTTPException, status
# from fastapi.security import OAuth2PasswordBearer
# from sqlalchemy.orm import Session
# from typing import Annotated, Optional

# from models import User
# from schemas.auth import TokenData
# from database import get_db
# from config import settings

# # Security configurations
# pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")

# def verify_password(plain_password: str, hashed_password: str) -> bool:
#     print("here")
#     print("PASSWORD TO VERIFY",plain_password)
#     return pwd_context.verify(plain_password, hashed_password)

# def get_password_hash(password: str) -> str:
#     print("PASSWORD: ",password)
#     return pwd_context.hash(password)

# def create_access_token(data: dict, expires_delta: timedelta = None):
#     to_encode = data.copy()
#     if expires_delta:
#         expire = datetime.utcnow() + expires_delta
#     else:
#         expire = datetime.utcnow() + timedelta(minutes=15)
#     to_encode.update({"exp": expire})
#     encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
#     return encoded_jwt

# def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
#     user = db.query(User).filter(User.email == email).first()
#     if not user or not verify_password(password, user.hashed_password):
#         return None
#     return user

# async def get_current_user(
#     token: str = Depends(oauth2_scheme),
#     db: Session = Depends(get_db)
# ):
#     credentials_exception = HTTPException(
#     status_code=status.HTTP_401_UNAUTHORIZED,  #
#     detail="Could not validate credentials",
#     headers={"WWW-Authenticate": "Bearer"},
# )
#     try:
#         payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
#         user_id: int = payload.get("user_id")
#         if user_id is None:
#             raise credentials_exception
#         token_data = TokenData(
#             email=payload.get("sub"),
#             user_id=user_id,
#             role=payload.get("role")
#         )
#     except JWTError:
#         raise credentials_exception
    
#     user = db.query(User).filter(User.id == token_data.user_id).first()
#     if user is None:
#         raise credentials_exception
#     return user

# async def get_current_admin(
#     current_user: Annotated[User, Depends(get_current_user)]
# ) -> User:
#     if current_user.role != "admin":
#         raise HTTPException(
#             status_code=status.HTTP_403_FORBIDDEN,
#             detail="Admin privileges required"
#         )
#     return current_user

# async def get_current_active_user(
#     current_user: Annotated[User, Depends(get_current_user)]
# ) -> User:
#     if not current_user.is_active:
#         raise HTTPException(
#             status_code=status.HTTP_403_FORBIDDEN,
#             detail="Inactive user"
#         )
#     return current_user

# async def get_current_vendor(
#     current_user: Annotated[User, Depends(get_current_user)]
# ) -> User:
#     if current_user.role != "vendor":
#         raise HTTPException(
#             status_code=status.HTTP_403_FORBIDDEN,
#             detail="Vendor access required"
#         )
#     return current_user

# def authenticate_vendor(db: Session, email: str, password: str) -> Optional[User]:
#     # First authenticate the user
#     user = authenticate_user(db, email, password)
    
#     if not user:
#         return None
    
#     # Check if user is a vendor
#     if user.role != "vendor":
#         return None
    
#     return user

# def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
#     user = db.query(User).filter(User.email == email).first()
#     if not user or not verify_password(password, user.hashed_password):
#         return None
    
#     return user


from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from pathlib import Path
import logging
import re
from typing import Annotated, Any, Dict, List, Optional

import requests
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import or_
from sqlalchemy.exc import ProgrammingError, OperationalError, IntegrityError
from sqlalchemy.orm import Session

from models import User
from schemas.auth import TokenData
from database import get_db
from config import settings
from services.db_sequences import create_user

# Security configurations
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token")
logger = logging.getLogger(__name__)


def _is_users_schema_mismatch(exc: BaseException) -> bool:
    """True when local `users` table does not match the collab User model."""
    msg = str(getattr(exc, "orig", exc)).lower()
    return (
        "users.email" in msg
        or "column users.email does not exist" in msg
        or "hashed_password" in msg
        or "undefinedcolumn" in msg
    )


def _read_pem_from_env_file(var_name: str, begin_marker: str, end_marker: str) -> Optional[str]:
    """Best-effort parser for multiline PEM values from backend/.env."""
    try:
        env_path = Path(__file__).resolve().parents[1] / ".env"
        text = env_path.read_text(encoding="utf-8")
        pattern = rf"{var_name}=(?P<val>{re.escape(begin_marker)}.*?{re.escape(end_marker)})"
        m = re.search(pattern, text, flags=re.DOTALL)
        if not m:
            return None
        return m.group("val").strip().strip('"').replace("\\n", "\n")
    except Exception:
        return None


def _jwt_signing_key() -> str:
    if settings.ALGORITHM == "RS256":
        key = settings.jwt_private_key
        if key:
            key = key.strip().strip('"').replace("\\n", "\n")
        if not key or "END PRIVATE KEY" not in key:
            key = _read_pem_from_env_file(
                "JWT_PRIVATE_KEY",
                "-----BEGIN PRIVATE KEY-----",
                "-----END PRIVATE KEY-----",
            )
        if not key:
            raise RuntimeError("RS256 requires JWT_PRIVATE_KEY")
        return key
    return settings.SECRET_KEY


def _jwt_verification_key() -> str:
    if settings.ALGORITHM == "RS256":
        key = settings.jwt_public_key
        if key:
            key = key.strip().strip('"').replace("\\n", "\n")
        if not key or "END PUBLIC KEY" not in key:
            key = _read_pem_from_env_file(
                "JWT_PUBLIC_KEY",
                "-----BEGIN PUBLIC KEY-----",
                "-----END PUBLIC KEY-----",
            )
        if not key:
            raise RuntimeError("RS256 requires JWT_PUBLIC_KEY")
        return key
    return settings.SECRET_KEY


# -------------------------------------------------------
# PASSWORD UTILITIES
# -------------------------------------------------------
def verify_password(plain_password: str, hashed_password: str) -> bool:
    if not hashed_password or not str(hashed_password).strip():
        return False
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        # Unknown/corrupt hash formats must not 500 the login path.
        return False


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# -------------------------------------------------------
# JWT TOKEN CREATION
# -------------------------------------------------------
def access_token_expires_delta() -> Optional[timedelta]:
    """
    Lifetime for newly minted access tokens.

    ACCESS_TOKEN_EXPIRE_MINUTES <= 0 means no expiry (session lasts until logout).
    """
    minutes = int(getattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", 0) or 0)
    if minutes <= 0:
        return None
    return timedelta(minutes=minutes)


def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()

    # Omit `exp` when no lifetime is configured so the token stays valid until logout.
    if expires_delta is not None and expires_delta.total_seconds() > 0:
        to_encode.update({"exp": datetime.utcnow() + expires_delta})

    encoded_jwt = jwt.encode(
        to_encode,
        _jwt_signing_key(),
        algorithm=settings.ALGORITHM
    )
    return encoded_jwt


# -------------------------------------------------------
# USER AUTHENTICATION
# -------------------------------------------------------
def authenticate_user(db: Session, email: str, password: str) -> Optional[User]:
    """
    Local collab-DB auth. On Render, DATABASE_URL may point at Foundation's
    `users` table (user_email / password_hash) which does not have `email` —
    never 500 in that case; return None so callers can use Nest instead.
    """
    try:
        user = db.query(User).filter(User.email == email).first()
    except (ProgrammingError, OperationalError) as exc:
        try:
            db.rollback()
        except Exception:
            pass
        if _is_users_schema_mismatch(exc):
            logger.warning(
                "[auth] Local users schema mismatch (missing email/hashed_password); "
                "skipping local authenticate_user. Set NESTJS_API_URL to Foundation."
            )
            return None
        raise

    if not user:
        return None

    if not verify_password(password, user.hashed_password):
        return None

    return user


def _pick_int(source: Dict[str, Any], *keys: str, default: int = 0) -> int:
    for k in keys:
        v = source.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return default


def _email_from_jwt_payload(payload: Dict[str, Any]) -> Optional[str]:
    email = payload.get("email")
    if email and "@" in str(email):
        return str(email).strip()
    sub = payload.get("sub")
    if sub and "@" in str(sub):
        return str(sub).strip()
    return None


def _role_from_jwt_payload(payload: Dict[str, Any]) -> Optional[str]:
    role = payload.get("role")
    if role:
        return str(role).strip().lower()
    roles = payload.get("roles")
    if isinstance(roles, list) and roles:
        r0 = roles[0]
        if isinstance(r0, dict):
            name = r0.get("roleName") or r0.get("role_name")
            if name:
                return str(name).strip().lower()
    role_names = payload.get("roleNames")
    if isinstance(role_names, list) and role_names:
        return str(role_names[0]).strip().lower()
    return None


def _role_from_nest_context(ctx: Dict[str, Any]) -> str:
    roles = ctx.get("roles")
    if isinstance(roles, list) and roles:
        r0 = roles[0]
        if isinstance(r0, dict):
            name = r0.get("roleName") or r0.get("role_name")
            if name:
                return str(name).strip().lower()
    role_assignments = ctx.get("roleAssignments")
    if isinstance(role_assignments, list) and role_assignments:
        r0 = role_assignments[0]
        if isinstance(r0, dict):
            name = r0.get("roleName") or r0.get("role_name")
            if name:
                return str(name).strip().lower()
    role_names = ctx.get("roleNames")
    if isinstance(role_names, list) and role_names:
        return str(role_names[0]).strip().lower()
    return "user"


def _bootstrap_nest_jwt(email: str = "bootstrap@external-sourcing.local") -> str:
    """Minimal access JWT accepted by Foundation JwtStrategy for service lookups."""
    return jwt.encode(
        {
            "sub": "1",
            "user_id": 1,
            "email": email.strip(),
            "tenant_id": 1,
            "company_id": 1,
            "role_id": 0,
            "role_ids": [],
            "roles": [],
            "department_id": None,
            "is_super_admin": False,
            "token_type": "access",
        },
        _jwt_signing_key(),
        algorithm=settings.ALGORITHM,
    )


def _fetch_nest_user_context_by_email(
    email: str,
    bearer_token: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    GET {NESTJS_API_URL}/auth/user-context/by-email — same as vendor login sync.
    """
    base = (settings.nestjs_api_url or "").strip().rstrip("/")
    if not base or not email.strip():
        return None

    url = f"{base}/auth/user-context/by-email"
    attempts: List[str] = []
    if bearer_token:
        attempts.append(bearer_token)
    bootstrap = _bootstrap_nest_jwt(email.strip())
    if bootstrap not in attempts:
        attempts.append(bootstrap)

    for token in attempts:
        try:
            resp = requests.get(
                url,
                params={"email": email.strip()},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            if resp.ok:
                data = resp.json() if resp.content else {}
                return data if isinstance(data, dict) else None
            logger.warning(
                "[auth] Nest user-context %s for %s: status=%s",
                url,
                email,
                resp.status_code,
            )
        except Exception as exc:
            logger.warning("[auth] Nest user-context request failed: %s", exc)
    return None


def _ensure_local_user_for_jwt(
    db: Session,
    payload: Dict[str, Any],
    user_id: int,
    bearer_token: Optional[str] = None,
) -> Optional[User]:
    """
    Resolve a collab `users` row for a valid JWT. If missing locally, pull
    Foundation user-context and insert/update (vendor login uses the same flow).
    Soft-fails when the connected DB does not have the collab users schema.
    """
    email = _email_from_jwt_payload(payload)
    filters = [User.user_id == user_id]
    if email:
        filters.append(User.email == email)

    try:
        user = db.query(User).filter(or_(*filters)).first()
    except (ProgrammingError, OperationalError) as exc:
        try:
            db.rollback()
        except Exception:
            pass
        if _is_users_schema_mismatch(exc):
            logger.warning(
                "[auth] Local users schema mismatch while resolving JWT user; "
                "returning None (Nest token still valid)."
            )
            return None
        raise

    ctx: Optional[Dict[str, Any]] = None
    if user is None and email:
        ctx = _fetch_nest_user_context_by_email(email, bearer_token=bearer_token)

    resolved_user_id = user_id
    if ctx:
        ctx_id = _pick_int(ctx, "user_id", "userId", "id", default=0)
        if ctx_id > 0:
            resolved_user_id = ctx_id

    role = _role_from_jwt_payload(payload) or (ctx and _role_from_nest_context(ctx)) or "user"
    resolved_email = email or (str(ctx.get("email")).strip() if ctx and ctx.get("email") else None)

    if user is None:
        if not resolved_email:
            return None
        try:
            user = create_user(
                db,
                user_id=int(resolved_user_id),
                email=resolved_email,
                hashed_password="",
                role=role,
                is_active=True,
                vendor_id=None,
            )
        except (ProgrammingError, OperationalError, IntegrityError) as exc:
            try:
                db.rollback()
            except Exception:
                pass
            if _is_users_schema_mismatch(exc) or isinstance(exc, IntegrityError):
                logger.warning(
                    "[auth] Could not provision local user (schema/constraint): %s",
                    exc,
                )
                return None
            raise
        logger.info(
            "[auth] provisioned local user user_id=%s email=%s role=%s from JWT/Nest context",
            user.user_id,
            user.email,
            user.role,
        )
        return user

    changed = False
    if resolved_user_id and user.user_id != resolved_user_id:
        user.user_id = int(resolved_user_id)
        changed = True
    if resolved_email and user.email != resolved_email:
        user.email = resolved_email
        changed = True
    if role and user.role != role:
        user.role = role
        changed = True
    if user.is_active is False or user.is_active is None:
        user.is_active = True
        changed = True
    if changed:
        try:
            db.commit()
            db.refresh(user)
        except (ProgrammingError, OperationalError) as exc:
            try:
                db.rollback()
            except Exception:
                pass
            if _is_users_schema_mismatch(exc):
                logger.warning("[auth] Could not update local user (schema mismatch)")
                return user
            raise
    return user


def _attach_token_tenant_company(user: User, payload: Dict[str, Any]) -> User:
    from services.company_scope import resolve_company_id

    token_tenant_id = payload.get("tenant_id")
    try:
        user.token_tenant_id = (
            int(token_tenant_id) if token_tenant_id is not None else None
        )
    except (TypeError, ValueError):
        user.token_tenant_id = None
    # Force single-company scope for all authenticated APIs.
    user.token_company_id = resolve_company_id(payload.get("company_id"))
    return user


# -------------------------------------------------------
# CURRENT USER FROM JWT TOKEN
# -------------------------------------------------------
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            _jwt_verification_key(),
            algorithms=[settings.ALGORITHM]
        )

        raw_user_id = payload.get("user_id", payload.get("sub"))
        if raw_user_id is None:
            raise credentials_exception
        user_id = int(raw_user_id)

        token_data = TokenData(
            email=payload.get("email"),
            user_id=user_id,
            role=payload.get("role"),
        )

    except (JWTError, ValueError):
        raise credentials_exception

    user = None
    try:
        user = db.query(User).filter(User.user_id == token_data.user_id).first()
    except (ProgrammingError, OperationalError) as exc:
        try:
            db.rollback()
        except Exception:
            pass
        if not _is_users_schema_mismatch(exc):
            raise
        logger.warning(
            "[auth] Local users schema mismatch in get_current_user; "
            "using JWT payload only"
        )
        user = None

    if user is None:
        user = _ensure_local_user_for_jwt(
            db, payload, token_data.user_id, bearer_token=token
        )

    # When collab `users` schema is unavailable (e.g. Render pointed at Foundation DB),
    # build an ephemeral in-memory user from the JWT so API auth still works.
    if user is None:
        email = _email_from_jwt_payload(payload) or token_data.email or ""
        role = _role_from_jwt_payload(payload) or token_data.role or "user"
        user = User(
            user_id=token_data.user_id,
            email=email,
            hashed_password="",
            role=role,
            is_active=True,
            vendor_id=None,
        )

    # Prefer role from token when present (Nest context role), while keeping DB as fallback.
    role_from_token = _role_from_jwt_payload(payload)
    if role_from_token:
        user.role = role_from_token
    elif token_data.role:
        user.role = token_data.role

    # Foundation JWT users often lack users.vendor_id; link by registration email so
    # /vendor/profile, documents, banks, and OCR see the vendor created at register.
    if getattr(user, "vendor_id", None) is None and (getattr(user, "email", None) or ""):
        try:
            from services.po_vendor_scope import resolve_vendor_for_user

            resolve_vendor_for_user(db, user, persist_link=True)
        except Exception as link_exc:
            logger.warning(
                "[auth] vendor email link skipped for user_id=%s: %s",
                getattr(user, "user_id", None),
                link_exc,
            )

    return _attach_token_tenant_company(user, payload)


# -------------------------------------------------------
# ROLE-BASED ACCESS HELPERS
# -------------------------------------------------------
async def get_current_admin(
    current_user: Annotated[User, Depends(get_current_user)]
) -> User:
    """Role gate removed — permissions API owns access. Kept for call-site compat."""
    return current_user


async def get_current_active_user(
    current_user: Annotated[User, Depends(get_current_user)]
) -> User:
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )
    return current_user


async def get_current_vendor(
    current_user: Annotated[User, Depends(get_current_user)]
) -> User:
    """Role gate removed — permissions API owns access. Kept for call-site compat."""
    return current_user


# -------------------------------------------------------
# VENDOR AUTHENTICATION (unchanged)
# -------------------------------------------------------
def authenticate_vendor(db: Session, email: str, password: str) -> Optional[User]:
    print(f"[authenticate_vendor] Attempting to authenticate: {email}")
    user = authenticate_user(db, email, password)
    if not user:
        print(f"[authenticate_vendor] User authentication failed for: {email}")
        return None
    print(f"[authenticate_vendor] User authenticated. Role: {user.role} (role check removed)")
    return user
