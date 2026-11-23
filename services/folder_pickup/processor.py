"""Route pickup files into existing OCR / PO / invoice processing flows."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from config import settings
from database import SessionLocal
from models import Vendor
from services.document_ocr.pipeline import run_ocr_pipeline
from services.document_ocr.utils import classified_document_type
from services.document_ocr.vendor_service import process_vendor_documents
from services.folder_pickup.file_utils import guess_content_type
from services.po_upload_service import process_po_upload

logger = logging.getLogger(__name__)

PO_DOCUMENT_TYPES = {"purchase order", "po"}


def _pickup_context() -> Dict[str, Any]:
    vendor_id = settings.document_pickup_vendor_id
    if vendor_id is None:
        raise ValueError("DOCUMENT_PICKUP_VENDOR_ID is not configured")
    return {
        "vendor_id": int(vendor_id),
        "tenant_id": int(settings.document_pickup_tenant_id),
        "company_id": int(settings.document_pickup_company_id),
        "user_id": int(settings.document_pickup_user_id),
    }


def _classify_pickup_file(file_path: Path) -> str:
    saved_files: List[str] = [str(file_path.resolve())]
    _, document_classification, _ = run_ocr_pipeline(saved_files)
    return classified_document_type(
        document_classification if isinstance(document_classification, dict) else None
    )


def _process_po_pickup(
    db: Session,
    file_path: Path,
    *,
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    vendor = db.query(Vendor).filter(Vendor.vendor_id == ctx["vendor_id"]).first()
    vendor_name = vendor.company_name if vendor else None
    content_type = guess_content_type(file_path)
    file_bytes = file_path.read_bytes()

    outcome = process_po_upload(
        db,
        file_bytes=file_bytes,
        filename=file_path.name,
        content_type=content_type,
        tenant_id=ctx["tenant_id"],
        company_id=ctx["company_id"],
        document_type="PO",
        version_no=1,
        vendor_id=ctx["vendor_id"],
        user_id=ctx["user_id"],
        vendor_name=vendor_name,
        department_id=1,
    )
    return {
        "flow": "purchase_order_upload",
        "action": outcome.action,
        "message": outcome.message,
        "po_id": outcome.po_id,
        "po_number": outcome.po_number,
        "duplicate": outcome.duplicate,
    }


def _process_vendor_pickup(
    db: Session,
    file_path: Path,
    *,
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    saved_files = [str(file_path.resolve())]
    received_files = [
        {
            "filename": file_path.name,
            "content_type": guess_content_type(file_path),
        }
    ]
    result = process_vendor_documents(
        db,
        vendor_id=ctx["vendor_id"],
        saved_files=saved_files,
        received_files=received_files,
        tenant_id=ctx["tenant_id"],
        company_id=ctx["company_id"],
        created_by=str(ctx["user_id"]),
    )
    result["flow"] = "vendor_document_ocr"
    return result


def process_pickup_file(file_path: Path) -> Dict[str, Any]:
    """
    Run an incoming folder file through the existing OCR workflows.

    Purchase orders -> process_po_upload
    All other supported docs -> process_vendor_documents (invoice, GRN, rate contract, etc.)
    """
    ctx = _pickup_context()
    db = SessionLocal()
    try:
        logger.info("[folder-pickup] OCR started: %s", file_path)
        document_type = _classify_pickup_file(file_path)
        logger.info(
            "[folder-pickup] OCR completed: %s (classified as %r)",
            file_path,
            document_type or "unknown",
        )

        if document_type in PO_DOCUMENT_TYPES:
            result = _process_po_pickup(db, file_path, ctx=ctx)
        else:
            result = _process_vendor_pickup(db, file_path, ctx=ctx)

        logger.info("[folder-pickup] Processing successful: %s", file_path)
        return {
            "status": "success",
            "file": file_path.name,
            "document_type": document_type,
            "result": result,
        }
    except Exception as exc:
        db.rollback()
        logger.exception("[folder-pickup] Processing failed: %s", file_path)
        return {
            "status": "failed",
            "file": file_path.name,
            "error": str(exc),
        }
    finally:
        db.close()
