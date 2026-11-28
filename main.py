from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy.orm import Session
from sqlalchemy import text

from database import engine, get_db, Base, vector_engine, VectorBase
import models  # needed for SQLAlchemy model registration
from config import settings

import logging
import sys
import uvicorn

from services.cache_manager import cache_manager
from services.error_handling import install_error_handling
from forecasting_service.forecast import router as forecast_router
from services.folder_pickup import (
    start_folder_pickup_service,
    stop_folder_pickup_service,
)

from immudb import ImmudbClient

from routers import (
    auth,
    otp,
    vendor,
    rfq,
    purchase_order,
    purchase_order_history,
    vendor_item_fingerprint,
    document_item_embedding,
    mcp_vector,
    api_catalog,
    whatsapp_vendor,
    whatsapp_procurement,
    ocr,
    stock,
    stock_upload_header,
    stock_upload_detail,
    sales_transaction,
    project_progress,
    invoice_line_match,
    invoice,
    grn,
)


sys.setrecursionlimit(10000)

# Create DB tables (pgvector required for document_item_embeddings.embedding).
# Sequences must exist before create_all — PO models use nextval(...) defaults.
with engine.connect() as conn:
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    conn.execute(text("CREATE SEQUENCE IF NOT EXISTS purchase_order_header_po_id_seq"))
    conn.execute(text("CREATE SEQUENCE IF NOT EXISTS purchase_order_line_line_id_seq"))
    conn.commit()
Base.metadata.create_all(bind=engine)

# Align users.user_id serial after JWT/Foundation explicit-id inserts.
from services.db_sequences import ensure_users_id_sequence_guard

ensure_users_id_sequence_guard(engine)

# Vector DB tables (vendor_item_fingerprint, item_embeddings) — non-fatal if VECTOR_DB is down
try:
    with vector_engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        conn.commit()
    VectorBase.metadata.create_all(bind=vector_engine)
except Exception as exc:
    logging.getLogger(__name__).warning(
        "Vector DB unavailable (%s); vendor_item_fingerprint / item_embeddings features will fail until it is reachable",
        exc,
    )

_upload_root = Path(settings.upload_dir or "uploads").resolve()
_upload_root.mkdir(parents=True, exist_ok=True)
(_upload_root / "vendor_documents").mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Hospital Demand Forecasting API",
    description="API for forecasting medical supply demand",
    version="1.0.0"
)

# Must be installed BEFORE CORSMiddleware so error responses (incl. 500s) still get CORS headers.
install_error_handling(app)

ALLOWED_ORIGINS = settings.allowed_origins
CORS_ORIGIN_REGEX = settings.cors_origin_regex

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def init_immudb():
    if not settings.immudb_enabled:
        print("immudb init skipped (IMMUDB_ENABLED=false)")
        return
    try:
        addr = f"{settings.immudb_host}:{settings.immudb_port}"
        client = ImmudbClient(addr)
        client.login(settings.immudb_username, settings.immudb_password)
        client.useDatabase(settings.immudb_database.encode("utf-8"))

        create_sql = """
        CREATE TABLE IF NOT EXISTS media_attachment_ledger (
            ledger_id INTEGER AUTO_INCREMENT,
            media_name VARCHAR,
            media_type VARCHAR,
            media_url VARCHAR,
            context_registry_id INTEGER,
            ocr_raw_text JSON,
            tenant_id INTEGER,
            company_id INTEGER,
            created_by VARCHAR,
            created_at TIMESTAMP,
            PRIMARY KEY (ledger_id)
        );
        """

        client.sqlExec(create_sql)
        client.logout()

    except Exception as e:
        print("immudb init failed:", e)

app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(otp.router, prefix="/otp", tags=["OTP"])
app.include_router(vendor.router)
app.include_router(rfq.router)
app.include_router(purchase_order.router)
app.include_router(purchase_order_history.router)
app.include_router(vendor_item_fingerprint.router)
app.include_router(document_item_embedding.router)
app.include_router(mcp_vector.router)
app.include_router(api_catalog.router)
app.include_router(whatsapp_vendor.router)
app.include_router(whatsapp_procurement.router)
app.include_router(ocr.router)
app.include_router(stock.router)
app.include_router(stock_upload_header.router)
app.include_router(stock_upload_detail.router)
app.include_router(sales_transaction.router)
app.include_router(project_progress.router)
app.include_router(invoice_line_match.router)
app.include_router(invoice.router)
app.include_router(grn.router)
app.include_router(
    forecast_router,
    prefix="/forecast",
    tags=["Forecast"]
)


app.mount("/uploads", StaticFiles(directory=str(_upload_root)), name="uploads")

@app.on_event("startup")
async def startup_event():
    stats = await cache_manager.get_cache_stats()
    print(f"Redis Cache Status: {stats.get('status', 'unknown')}")
    start_folder_pickup_service()


@app.on_event("shutdown")
def shutdown_event():
    stop_folder_pickup_service()

@app.get("/")
async def root():
    return {"message": "Welcome to Hospital Forecasting API"}

@app.get("/health")
async def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "healthy"
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    cache_stats = await cache_manager.get_cache_stats()

    return {
        "status": "healthy"
        if db_status == "healthy" and cache_stats.get("status") == "connected"
        else "unhealthy",
        "database": db_status,
        "cache": cache_stats.get("status", "unknown"),
        "cache_stats": cache_stats,
    }

if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=True)
