"""Registration-phase OCR (onboarding docs only, no GRN/invoice/dispatch imports)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import MediaAttachment, Vendor
from services.document_ocr.pipeline import run_ocr_pipeline
from services.document_ocr.utils import build_ocr_serializable, make_json_serializable
from services.immudb_service import insert_into_immudb
from services.ocr_bank_import import insert_ocr_vendor_banks
from services.ocr_document_import import insert_ocr_vendor_documents
from services.ocr_item_rate_import import insert_ocr_items_and_rates
from services.ocr_vendor_merge import merge_ocr_vendor_master
from services.vendor_codes import (
    VENDOR_NOT_APPROVED_OCR_DETAIL,
    ensure_vendor_code_from_registration,
)
from services.vendor_document_storage import persist_vendor_upload_files


def process_registration_documents(
    db: Session,
    *,
    vendor_id: int,
    saved_files: List[str],
    received_files: List[Dict[str, str]],
    tenant_id: int = 1,
    company_id: int = 3,
    created_by: str = "whatsapp",
) -> Dict[str, Any]:
    """
    OCR + persist vendor master, banks, items, documents.
    Skips GRN / invoice / dispatch imports (registration phase only).
    """
    vendor_row = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )

    if not ensure_vendor_code_from_registration(vendor_row):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=VENDOR_NOT_APPROVED_OCR_DETAIL,
        )

    _, document_classification, raw_text = run_ocr_pipeline(saved_files)

    vendor_items_inserted = 0
    vendor_rates_inserted = 0
    vendor_banks_inserted = 0
    vendor_documents_inserted = 0

    upload_names = [r["filename"] for r in received_files]
    persisted_paths, upload_names = persist_vendor_upload_files(
        vendor_id, saved_files, upload_names
    )
    ocr_payload = raw_text if isinstance(raw_text, dict) else {}

    if isinstance(raw_text, dict):
        merge_ocr_vendor_master(db, vendor_row, raw_text)
        vendor_items_inserted, vendor_rates_inserted, _, _ = insert_ocr_items_and_rates(
            db,
            vendor_id,
            raw_text,
            tenant_id=tenant_id,
            company_id=company_id,
            department_id=1,
            created_by=int(created_by) if str(created_by).isdigit() else None,
        )
        vendor_banks_inserted = insert_ocr_vendor_banks(db, vendor_id, raw_text)

    vendor_documents_inserted = insert_ocr_vendor_documents(
        db,
        vendor_id,
        ocr_payload,
        persisted_paths,
        upload_names,
        document_classification=document_classification
        if isinstance(document_classification, dict)
        else None,
    )

    ocr_serializable = build_ocr_serializable(raw_text, document_classification)
    classification_serializable = make_json_serializable(document_classification)

    new_media = MediaAttachment(
        media_name=received_files[0]["filename"],
        media_type=received_files[0]["content_type"],
        media_url="temporary",
        context_registry_id=1,
        ocr_raw_text=ocr_serializable,
        tenant_id=tenant_id,
        company_id=company_id,
        created_by=created_by,
    )
    db.add(new_media)
    db.commit()
    db.refresh(new_media)

    try:
        insert_into_immudb(new_media, ocr_serializable)
    except Exception:
        pass

    return {
        "vendor_id": vendor_id,
        "status": "completed",
        "files_received": received_files,
        "document_classification": classification_serializable,
        "vendor_items_inserted": vendor_items_inserted,
        "vendor_rates_inserted": vendor_rates_inserted,
        "vendor_banks_inserted": vendor_banks_inserted,
        "vendor_documents_inserted": vendor_documents_inserted,
        "raw_text": make_json_serializable(raw_text),
    }
