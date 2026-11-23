# backend/config.py
from __future__ import annotations

import os
import re
from typing import ClassVar, Optional
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_SCHEMA_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:,[A-Za-z_][A-Za-z0-9_]*)*$")


def _normalize_sqlalchemy_url(url: str) -> str:
    """Accept postgres:// / postgresql:// and ensure the psycopg2 SQLAlchemy driver.

    Prisma-style ``?schema=public`` is not a libpq option. psycopg2 raises
    ``invalid connection option "schema"`` and the process never starts.
    """
    u = (url or "").strip()
    if not u:
        return u
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://") :]
    if u.startswith("postgresql://") and "+psycopg2" not in u.split("://", 1)[0]:
        u = "postgresql+psycopg2://" + u[len("postgresql://") :]

    parts = urlsplit(u)
    query = parse_qsl(parts.query, keep_blank_values=True)
    schema: Optional[str] = None
    kept: list[tuple[str, str]] = []
    for key, value in query:
        if key.lower() == "schema":
            schema = value.strip()
            continue
        kept.append((key, value))

    if schema and _SCHEMA_NAME.fullmatch(schema):
        search_path = f"-csearch_path={schema}"
        replaced = False
        rewritten: list[tuple[str, str]] = []
        for key, value in kept:
            if key.lower() == "options" and "search_path" not in value:
                value = f"{value} {search_path}".strip()
                replaced = True
            elif key.lower() == "options":
                replaced = True
            rewritten.append((key, value))
        if not replaced:
            rewritten.append(("options", search_path))
        kept = rewritten

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _compose_postgres_url(
    *,
    user: Optional[str],
    password: Optional[str],
    host: Optional[str],
    port: Optional[str],
    name: Optional[str],
) -> Optional[str]:
    # password may be intentionally empty; only treat None as missing
    if not user or password is None or not host or not port or not name:
        return None
    return (
        f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{name}"
    )


def _resolve_db_url(
    *,
    url: Optional[str],
    user: Optional[str],
    password: Optional[str],
    host: Optional[str],
    port: Optional[str],
    name: Optional[str],
    label: str,
) -> str:
    if url and url.strip():
        return _normalize_sqlalchemy_url(url)
    composed = _compose_postgres_url(
        user=user, password=password, host=host, port=port, name=name
    )
    if composed:
        return composed
    raise ValueError(
        f"Missing {label} config: set {label} as a single URL "
        f"(e.g. DATABASE_URL) or provide the discrete USER/PASSWORD/HOST/PORT/NAME vars"
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # -------------------------------------------------------------------------
    # Main database — either DATABASE_URL or DB_USER / DB_PASSWORD / ...
    # -------------------------------------------------------------------------
    database_url_override: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL", "database_url_override"),
    )
    db_user: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("DB_USER", "db_user")
    )
    db_password: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("DB_PASSWORD", "db_password")
    )
    db_host: Optional[str] = Field(
        default="localhost", validation_alias=AliasChoices("DB_HOST", "db_host")
    )
    db_port: Optional[str] = Field(
        default="5432", validation_alias=AliasChoices("DB_PORT", "db_port")
    )
    db_name: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("DB_NAME", "db_name")
    )

    # -------------------------------------------------------------------------
    # Vector / pgvector — VECTOR_DB_URL / VECTOR_DATABASE_URL or VECTOR_DB_*
    # -------------------------------------------------------------------------
    vector_db_url_override: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "VECTOR_DB_URL",
            "VECTOR_DATABASE_URL",
            "vector_db_url_override",
        ),
    )
    vector_db_user: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("VECTOR_DB_USER", "vector_db_user")
    )
    vector_db_password: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("VECTOR_DB_PASSWORD", "vector_db_password"),
    )
    vector_db_host: Optional[str] = Field(
        default="localhost",
        validation_alias=AliasChoices("VECTOR_DB_HOST", "vector_db_host"),
    )
    vector_db_port: Optional[str] = Field(
        default="5432",
        validation_alias=AliasChoices("VECTOR_DB_PORT", "vector_db_port"),
    )
    vector_db_name: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("VECTOR_DB_NAME", "vector_db_name")
    )

    # -------------------------------------------------------------------------
    # ERP database — either ERP_DB_URL or ERP_DB_USER / ERP_DB_PASSWORD / ...
    # -------------------------------------------------------------------------
    erp_db_url_override: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ERP_DB_URL", "erp_db_url_override"),
    )
    erp_db_user: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("ERP_DB_USER", "erp_db_user")
    )
    erp_db_password: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ERP_DB_PASSWORD", "erp_db_password"),
    )
    erp_db_host: Optional[str] = Field(
        default="localhost",
        validation_alias=AliasChoices("ERP_DB_HOST", "erp_db_host"),
    )
    erp_db_port: Optional[str] = Field(
        default="5432", validation_alias=AliasChoices("ERP_DB_PORT", "erp_db_port")
    )
    erp_db_name: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("ERP_DB_NAME", "erp_db_name")
    )

    # NestJS Foundation + shared internal Nest services (GRN / Invoice / Items)
    nestjs_api_url: Optional[str] = Field(
        default="http://localhost:3000",
        validation_alias=AliasChoices("NESTJS_API_URL", "nestjs_api_url"),
    )
    # Foundation document type used for the vendor-registration approval workflow.
    vendor_registration_document_type_code: str = Field(
        default="VENDOR_REG",
        validation_alias=AliasChoices(
            "VENDOR_REGISTRATION_DOCUMENT_TYPE_CODE",
            "vendor_registration_document_type_code",
        ),
    )
    # One base URL for GRN, Invoice, and Items (same Nest process). Optional
    # GRN_API_URL / INVOICE_API_URL / ITEMS_API_URL still accepted for back-compat.
    internal_api_url: Optional[str] = Field(
        default="http://localhost:3003",
        validation_alias=AliasChoices(
            "INTERNAL_API_URL",
            "GRN_API_URL",
            "INVOICE_API_URL",
            "ITEMS_API_URL",
            "internal_api_url",
        ),
    )

    # CORS — comma-separated list in ALLOWED_ORIGINS
    allowed_origins_raw: str = Field(
        default=(
            "http://localhost:3000,"
            "http://127.0.0.1:3000,"
            "http://localhost:3004,"
            "http://127.0.0.1:3004,"
            "http://localhost:5173,"
            "http://127.0.0.1:5173,"
            "http://localhost:3001,"
            "http://localhost:3002,"
            "http://localhost:3003,"
            "https://ai-driven-p2p-collab-external-soruicng-fe.onrender.com"
        ),
        validation_alias=AliasChoices("ALLOWED_ORIGINS", "allowed_origins"),
    )

    # JWT Configuration
    SECRET_KEY: str = Field(
        ...,
        min_length=32,
        validation_alias=AliasChoices("SECRET_KEY", "JWT_SECRET"),
    )
    # Optional alias of SECRET_KEY — kept for legacy env files; unused at runtime.
    JWT_SECRET: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("JWT_SECRET")
    )
    ALGORITHM: str = "RS256"
    # 0 = no expiry (session lasts until explicit logout). Set > 0 to re-enable TTL.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 0
    handoff_token_ttl_seconds: int = Field(
        1800,
        ge=60,
        le=7200,
        validation_alias=AliasChoices(
            "HANDOFF_TOKEN_TTL_SECONDS", "handoff_token_ttl_seconds"
        ),
    )
    password_reset_token_ttl_seconds: int = Field(
        1800,
        ge=60,
        le=86400,
        validation_alias=AliasChoices(
            "PASSWORD_RESET_TOKEN_TTL_SECONDS",
            "password_reset_token_ttl_seconds",
        ),
    )
    # When true (default), forgot-password responses include reset_url so the UI
    # can continue without SMTP. Set false once email/WhatsApp delivery is live.
    password_reset_include_link: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "PASSWORD_RESET_INCLUDE_LINK",
            "password_reset_include_link",
        ),
    )
    handoff_jwt_leeway_seconds: int = Field(
        300,
        ge=0,
        le=3600,
        validation_alias=AliasChoices(
            "HANDOFF_JWT_LEEWAY_SECONDS", "handoff_jwt_leeway_seconds"
        ),
    )
    jwt_private_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("JWT_PRIVATE_KEY", "jwt_private_key"),
    )
    jwt_public_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("JWT_PUBLIC_KEY", "jwt_public_key"),
    )

    # Invite System Configuration
    REQUIRE_INVITE_CODE: bool = True
    ADMIN_INVITE_CODE: str = "ADMIN123"
    USER_INVITE_CODE: Optional[str] = None
    upload_dir: str = "uploads"
    allowed_image_types: list = ["jpg", "jpeg", "png", "webp"]
    max_upload_size: int = 4

    # API Keys — Gemini retained as optional for legacy tooling; OCR/embeddings use OpenAI.
    gemini_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("GEMINI_API_KEY", "gemini_api_key"),
    )
    gemini_model: Optional[str] = Field(
        default="gemini-2.0-flash",
        validation_alias=AliasChoices("GEMINI_MODEL", "gemini_model"),
    )
    gemini_fallback_model: Optional[str] = Field(
        default="gemini-2.0-flash-lite",
        validation_alias=AliasChoices("GEMINI_FALLBACK_MODEL", "gemini_fallback_model"),
    )
    gemini_max_retries: int = Field(
        3,
        ge=1,
        le=10,
        validation_alias=AliasChoices("GEMINI_MAX_RETRIES", "gemini_max_retries"),
    )
    openai_api_key: str = Field(
        ...,
        validation_alias=AliasChoices("OPENAI_API_KEY", "openai_api_key"),
    )
    openai_model: Optional[str] = Field(
        default="gpt-4o-mini",
        validation_alias=AliasChoices("OPENAI_MODEL", "openai_model"),
    )
    openai_fallback_model: Optional[str] = Field(
        default="gpt-4o",
        validation_alias=AliasChoices("OPENAI_FALLBACK_MODEL", "openai_fallback_model"),
    )
    openai_max_retries: int = Field(
        3,
        ge=1,
        le=10,
        validation_alias=AliasChoices("OPENAI_MAX_RETRIES", "openai_max_retries"),
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        validation_alias=AliasChoices("EMBEDDING_MODEL", "embedding_model"),
    )
    embedding_dimensions: int = Field(
        1536,
        ge=1,
        le=3072,
        validation_alias=AliasChoices("EMBEDDING_DIMENSIONS", "embedding_dimensions"),
    )
    cloudinary_cloud_name: str = Field(
        ...,
        validation_alias=AliasChoices("CLOUDINARY_CLOUD_NAME", "cloudinary_cloud_name"),
    )
    cloudinary_api_key: str = Field(
        ..., validation_alias=AliasChoices("CLOUDINARY_API_KEY", "cloudinary_api_key")
    )
    cloudinary_api_secret: str = Field(
        ...,
        validation_alias=AliasChoices("CLOUDINARY_API_SECRET", "cloudinary_api_secret"),
    )

    # Redis Configuration
    redis_url: Optional[str] = Field(
        default=(
            "redis://default:TJkFZTifRTXghQyUvCYEfYtEE7UeAyIa@"
            "redis-11884.c212.ap-south-1-1.ec2.redns.redis-cloud.com:11884"
        ),
        validation_alias=AliasChoices("REDIS_URL", "redis_url"),
    )

    # immudb
    immudb_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices("IMMUDB_ENABLED", "immudb_enabled"),
    )
    immudb_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices("IMMUDB_HOST", "immudb_host"),
    )
    immudb_port: str = Field(
        default="3322", validation_alias=AliasChoices("IMMUDB_PORT", "immudb_port")
    )
    immudb_username: str = Field(
        default="immudb",
        validation_alias=AliasChoices("IMMUDB_USERNAME", "immudb_username"),
    )
    immudb_password: str = Field(
        default="immudb",
        validation_alias=AliasChoices("IMMUDB_PASSWORD", "immudb_password"),
    )
    immudb_database: str = Field(
        default="mydb",
        validation_alias=AliasChoices("IMMUDB_DATABASE", "immudb_database"),
    )

    # Server Configuration
    port: int = Field(default=8001, validation_alias=AliasChoices("PORT", "port"))
    host: str = Field(default="0.0.0.0", validation_alias=AliasChoices("HOST", "host"))

    whatsapp_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("WHATSAPP_API_KEY", "whatsapp_api_key"),
    )

    # WhatsApp PO-created notify (server-side after Create PO).
    # Prefer WHATSAPP_BACKEND_URL → POST /po-notify; else Meta direct with token/phone id.
    whatsapp_backend_url: Optional[str] = Field(
        default="http://localhost:4000",
        validation_alias=AliasChoices(
            "WHATSAPP_BACKEND_URL", "whatsapp_backend_url"
        ),
    )
    whatsapp_token: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("WHATSAPP_TOKEN", "whatsapp_token"),
    )
    whatsapp_phone_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("WHATSAPP_PHONE_ID", "whatsapp_phone_id"),
    )
    whatsapp_graph_api_base: str = Field(
        default="https://graph.facebook.com/v22.0",
        validation_alias=AliasChoices(
            "WHATSAPP_GRAPH_API_BASE", "whatsapp_graph_api_base"
        ),
    )
    whatsapp_notify_template: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "WHATSAPP_NOTIFY_TEMPLATE", "whatsapp_notify_template"
        ),
    )
    whatsapp_notify_template_lang: str = Field(
        default="en_US",
        validation_alias=AliasChoices(
            "WHATSAPP_NOTIFY_TEMPLATE_LANG", "whatsapp_notify_template_lang"
        ),
    )
    whatsapp_notify_template_body_param_count: int = Field(
        default=2,
        validation_alias=AliasChoices(
            "WHATSAPP_NOTIFY_TEMPLATE_BODY_PARAM_COUNT",
            "whatsapp_notify_template_body_param_count",
        ),
    )
    whatsapp_notify_template_short_param_max_length: int = Field(
        default=30,
        validation_alias=AliasChoices(
            "WHATSAPP_NOTIFY_TEMPLATE_SHORT_PARAM_MAX_LENGTH",
            "whatsapp_notify_template_short_param_max_length",
        ),
    )
    whatsapp_po_notify_template: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "WHATSAPP_PO_NOTIFY_TEMPLATE", "whatsapp_po_notify_template"
        ),
    )
    whatsapp_po_notify_template_lang: str = Field(
        default="en",
        validation_alias=AliasChoices(
            "WHATSAPP_PO_NOTIFY_TEMPLATE_LANG", "whatsapp_po_notify_template_lang"
        ),
    )
    whatsapp_po_notify_template_body_param_count: int = Field(
        default=1,
        validation_alias=AliasChoices(
            "WHATSAPP_PO_NOTIFY_TEMPLATE_BODY_PARAM_COUNT",
            "whatsapp_po_notify_template_body_param_count",
        ),
    )
    whatsapp_po_handoff_notify_template: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "WHATSAPP_PO_HANDOFF_NOTIFY_TEMPLATE",
            "whatsapp_po_handoff_notify_template",
        ),
    )
    whatsapp_po_handoff_notify_template_lang: str = Field(
        default="en",
        validation_alias=AliasChoices(
            "WHATSAPP_PO_HANDOFF_NOTIFY_TEMPLATE_LANG",
            "whatsapp_po_handoff_notify_template_lang",
        ),
    )
    # Vendor approve → ACTIVE → set-password WhatsApp
    # Meta: account_creation_confirmation_3 (en_US)
    #   Body {{1}} = vendor/contact name
    #   URL button {{1}} = email (suffix for .../vendor/setup-password?email=)
    whatsapp_vendor_approved_notify_template: Optional[str] = Field(
        default="account_creation_confirmation_3",
        validation_alias=AliasChoices(
            "WHATSAPP_VENDOR_APPROVED_NOTIFY_TEMPLATE",
            "whatsapp_vendor_approved_notify_template",
        ),
    )
    whatsapp_vendor_approved_notify_template_lang: str = Field(
        default="en_US",
        validation_alias=AliasChoices(
            "WHATSAPP_VENDOR_APPROVED_NOTIFY_TEMPLATE_LANG",
            "whatsapp_vendor_approved_notify_template_lang",
        ),
    )
    # Set true only if Meta URL button uses a dynamic {{1}} suffix.
    whatsapp_vendor_approved_notify_url_button_dynamic: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "WHATSAPP_VENDOR_APPROVED_NOTIFY_URL_BUTTON_DYNAMIC",
            "whatsapp_vendor_approved_notify_url_button_dynamic",
        ),
    )
    po_notify_number: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "PO_NOTIFY_NUMBER",
            "WHATSAPP_TEST_NUMBER",
            "po_notify_number",
        ),
    )
    invoice_notify_number: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "INVOICE_NOTIFY_NUMBER",
            "invoice_notify_number",
        ),
    )
    grn_notify_number: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "GRN_NOTIFY_NUMBER",
            "grn_notify_number",
        ),
    )
    buyer_notify_number: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "BUYER_NOTIFY_NUMBER",
            "buyer_notify_number",
        ),
    )
    vendor_notify_number: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "VENDOR_NOTIFY_NUMBER",
            "vendor_notify_number",
        ),
    )
    vendor_frontend_url: str = Field(
        default="http://localhost:3002",
        validation_alias=AliasChoices(
            "VENDOR_FRONTEND_URL",
            "EXTERNAL_SOURCING_FE_URL",
            "FRONTEND_URL",
            "vendor_frontend_url",
        ),
    )

    ocr_service_url: str = Field(
        default="http://localhost:8010",
        validation_alias=AliasChoices("OCR_SERVICE_URL", "ocr_service_url"),
    )
    internal_sourcing_api_url: str = Field(
        default="http://localhost:8001",
        validation_alias=AliasChoices(
            "INTERNAL_SOURCING_API_URL",
            "internal_sourcing_api_url",
        ),
    )
    ai_agent_api_url: str = Field(
        default="http://localhost:8000",
        validation_alias=AliasChoices("AI_AGENT_API_URL", "ai_agent_api_url"),
    )
    ocr_service_api_key: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("OCR_SERVICE_API_KEY", "ocr_service_api_key"),
    )
    ocr_service_timeout_seconds: int = Field(
        300,
        ge=30,
        le=900,
        validation_alias=AliasChoices(
            "OCR_SERVICE_TIMEOUT_SECONDS", "ocr_service_timeout_seconds"
        ),
    )
    ocr_max_workers: int = Field(
        4,
        ge=1,
        le=16,
        validation_alias=AliasChoices("OCR_MAX_WORKERS", "ocr_max_workers"),
    )
    ocr_dpi: int = Field(
        200,
        ge=72,
        le=400,
        validation_alias=AliasChoices("OCR_DPI", "ocr_dpi"),
    )
    ocr_dpi_fallback: int = Field(
        300,
        ge=150,
        le=600,
        validation_alias=AliasChoices("OCR_DPI_FALLBACK", "ocr_dpi_fallback"),
    )
    sales_stock_sync_async: bool = Field(
        True,
        validation_alias=AliasChoices(
            "SALES_STOCK_SYNC_ASYNC", "sales_stock_sync_async"
        ),
    )

    document_upload_folder: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DOCUMENT_UPLOAD_FOLDER", "document_upload_folder"
        ),
    )
    document_pickup_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_ENABLED", "document_pickup_enabled"
        ),
    )
    document_pickup_vendor_id: Optional[int] = Field(
        default=None,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_VENDOR_ID", "document_pickup_vendor_id"
        ),
    )
    document_pickup_tenant_id: int = Field(
        default=1,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_TENANT_ID", "document_pickup_tenant_id"
        ),
    )
    document_pickup_company_id: int = Field(
        default=3,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_COMPANY_ID", "document_pickup_company_id"
        ),
    )
    # Used by resolve_company_id() for all API create/list scoping.
    default_company_id: int = Field(
        default=3,
        validation_alias=AliasChoices(
            "DEFAULT_COMPANY_ID",
            "default_company_id",
        ),
    )
    document_pickup_user_id: int = Field(
        default=1,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_USER_ID", "document_pickup_user_id"
        ),
    )
    document_pickup_stable_seconds: float = Field(
        2.0,
        ge=0.5,
        le=30.0,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_STABLE_SECONDS", "document_pickup_stable_seconds"
        ),
    )
    document_pickup_poll_interval_seconds: float = Field(
        15.0,
        ge=0.0,
        le=300.0,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_POLL_INTERVAL_SECONDS",
            "document_pickup_poll_interval_seconds",
        ),
    )
    document_pickup_event_debounce_seconds: float = Field(
        2.0,
        ge=0.5,
        le=30.0,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_EVENT_DEBOUNCE_SECONDS",
            "document_pickup_event_debounce_seconds",
        ),
    )
    document_pickup_document_type_name: str = Field(
        default="REQUISITION",
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_DOCUMENT_TYPE_NAME",
            "document_pickup_document_type_name",
        ),
    )
    document_pickup_process_id: int = Field(
        default=3,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_PROCESS_ID", "document_pickup_process_id"
        ),
    )
    document_pickup_api_timeout_seconds: float = Field(
        30.0,
        ge=5.0,
        le=120.0,
        validation_alias=AliasChoices(
            "DOCUMENT_PICKUP_API_TIMEOUT_SECONDS",
            "document_pickup_api_timeout_seconds",
        ),
    )

    DEFAULT_PAGE_SIZE: ClassVar[int] = 20

    _DEFAULT_ALLOWED_ORIGINS: ClassVar[str] = (
        "http://localhost:3000,"
        "http://127.0.0.1:3000,"
        "http://localhost:3004,"
        "http://127.0.0.1:3004,"
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:3001,"
        "http://localhost:3002,"
        "http://localhost:3003,"
        "https://ai-driven-p2p-collab-external-soruicng-fe.onrender.com"
    )

    @staticmethod
    def _split_origins(raw: str) -> list[str]:
        return [origin.strip() for origin in (raw or "").split(",") if origin.strip()]

    @property
    def allowed_origins(self) -> list[str]:
        """
        Browser CORS origins for external-sourcing-backend.

        Priority: ALLOWED_ORIGINS → CORS_ORIGINS → configured FE URLs → localhost defaults.
        Also merges VENDOR_FRONTEND_URL / EXTERNAL_SOURCING_FE_URL / SITE_URL when set.
        """
        origins: list[str] = []
        seen: set[str] = set()

        def _add(raw: Optional[str]) -> None:
            url = (raw or "").strip().rstrip("/")
            if not url or url in seen:
                return
            seen.add(url)
            origins.append(url)

        for chunk in (
            self.allowed_origins_raw,
            os.getenv("CORS_ORIGINS", ""),
            self._DEFAULT_ALLOWED_ORIGINS,
        ):
            for origin in self._split_origins(chunk):
                _add(origin)

        for env_key in (
            "EXTERNAL_SOURCING_FE_URL",
            "VENDOR_FRONTEND_URL",
            "FRONTEND_URL",
            "SITE_URL",
            "NEXT_PUBLIC_SITE_URL",
        ):
            _add(os.getenv(env_key))

        _add(self.vendor_frontend_url)

        return origins

    @property
    def cors_origin_regex(self) -> Optional[str]:
        """Optional regex for extra browser origins (e.g. AWS EC2 hostnames)."""
        explicit = (os.getenv("CORS_ORIGIN_REGEX") or "").strip()
        if explicit:
            return explicit
        allow_aws = (os.getenv("CORS_ALLOW_AWS_EC2") or "true").strip().lower()
        if allow_aws in ("1", "true", "yes", "on"):
            return r"https?://[\w.-]+\.compute-\d+\.[\w-]+\.amazonaws\.com(:\d+)?"
        return None

    # Back-compat aliases — all resolve to INTERNAL_API_URL
    @property
    def grn_api_url(self) -> Optional[str]:
        return self.internal_api_url

    @property
    def invoice_api_url(self) -> Optional[str]:
        return self.internal_api_url

    @property
    def items_api_url(self) -> Optional[str]:
        return self.internal_api_url

    @property
    def whatsapp_test_number(self) -> Optional[str]:
        """Legacy alias for po_notify_number."""
        return self.po_notify_number

    @property
    def grn_page_url(self) -> str:
        """Public vendor portal URL for creating a GRN (Add GRN page)."""
        base = (self.vendor_frontend_url or "http://localhost:3002").strip().rstrip("/")
        return f"{base}/vendor/add-grn"

    @property
    def database_url(self) -> str:
        return _resolve_db_url(
            url=self.database_url_override,
            user=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            name=self.db_name,
            label="DATABASE_URL",
        )

    @property
    def vector_database_url(self) -> str:
        return _resolve_db_url(
            url=self.vector_db_url_override,
            user=self.vector_db_user,
            password=self.vector_db_password,
            host=self.vector_db_host,
            port=self.vector_db_port,
            name=self.vector_db_name,
            label="VECTOR_DB_URL",
        )

    @property
    def erp_database_url(self) -> Optional[str]:
        """Resolved ERP DB URL, or None if ERP is not configured."""
        try:
            return _resolve_db_url(
                url=self.erp_db_url_override,
                user=self.erp_db_user,
                password=self.erp_db_password,
                host=self.erp_db_host,
                port=self.erp_db_port,
                name=self.erp_db_name,
                label="ERP_DB_URL",
            )
        except ValueError:
            return None

    @model_validator(mode="after")
    def _validate_required_db_urls(self):
        _ = self.database_url
        _ = self.vector_database_url
        if not self.JWT_SECRET:
            object.__setattr__(self, "JWT_SECRET", self.SECRET_KEY)
        return self


settings = Settings()
