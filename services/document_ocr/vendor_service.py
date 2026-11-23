"""Operational vendor document OCR (invoice/GRN/dispatch/rate contract/etc.).

Vendor master, banks, and registration documents are intentionally NOT updated
here — those writes belong only in ``process_registration_documents``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import MediaAttachment, Vendor
from services.document_ocr.pipeline import run_ocr_pipeline
from services.document_ocr.utils import (
    build_ocr_serializable,
    classification_includes,
    make_json_serializable,
)
from services.immudb_service import insert_into_immudb
from services.invoice_upload_notify import notify_invoice_uploaded
from services.ocr_dispatch_note_import import insert_ocr_dispatch_note
from services.ocr_grn_import import insert_ocr_grn
from services.ocr_invoice_import import insert_ocr_invoice
from services.ocr_item_rate_import import insert_ocr_items_and_rates
from services.ocr_sales_import import insert_ocr_sales_transactions
from services.rfq_rate_contract_sync import apply_rate_contract_rfq_revisions
from services.vendor_codes import (
    VENDOR_NOT_APPROVED_OCR_DETAIL,
    ensure_vendor_code_from_registration,
)
from services.vendor_document_storage import persist_vendor_upload_files


def process_vendor_documents(
    db: Session,
    *,
    vendor_id: int,
    saved_files: List[str],
    received_files: List[Dict[str, str]],
    tenant_id: int = 1,
    company_id: int = 3,
    created_by: str,
    po_number: Optional[str] = None,
    po_id: Optional[int] = None,
    bearer_token: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run OCR and persist operational imports (invoice/GRN/dispatch/sales/items).

    Does not update vendor master fields, banks, or registration document rows.
    Use ``process_registration_documents`` for onboarding / registration docs.
    """
    vendor_row = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor not found",
        )

    # Rate-contract / item OCR needs the Nest next-numbering vendor_code, which is
    # minted on approval. Cancel before calling the OCR microservice.
    if not ensure_vendor_code_from_registration(vendor_row):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=VENDOR_NOT_APPROVED_OCR_DETAIL,
        )

    _, document_classification, raw_text = run_ocr_pipeline(saved_files)

    vendor_items_inserted = 0
    vendor_rates_inserted = 0
    vendor_fingerprints_synced = 0
    vendor_banks_inserted = 0
    vendor_documents_inserted = 0
    grn_import_result: Optional[Dict[str, Any]] = None
    invoice_import_result: Optional[Dict[str, Any]] = None
    invoice_notifications: Optional[Dict[str, Any]] = None
    dispatch_note_import_result: Optional[Dict[str, Any]] = None
    sales_import_result: Optional[Dict[str, Any]] = None
    rfq_revisions: List[Dict[str, Any]] = []
    rate_contract_match_reviews: List[Dict[str, Any]] = []

    upload_names = [r["filename"] for r in received_files]
    persisted_paths, upload_names = persist_vendor_upload_files(
        vendor_id, saved_files, upload_names
    )

    if isinstance(raw_text, dict):
        doc_classification = (
            document_classification if isinstance(document_classification, dict) else None
        )
        # Never merge vendor master / banks / registration docs on this path.
        # Registration-only OCR owns those writes.
        is_rate_contract = classification_includes(
            doc_classification,
            "rate contract",
            "price list",
            "boq",
        )
        vendor_items_inserted, vendor_rates_inserted, vendor_fingerprints_synced, rate_contract_match_reviews = (
            insert_ocr_items_and_rates(
                db,
                vendor_id,
                raw_text,
                sync_item_fingerprints=is_rate_contract,
                tenant_id=tenant_id,
                company_id=company_id,
                department_id=1,
                created_by=int(created_by) if str(created_by).isdigit() else None,
            )
        )

        if classification_includes(doc_classification, "grn") or isinstance(raw_text.get("grn"), dict):
            grn_import_result = insert_ocr_grn(
                raw_text,
                tenant_id=tenant_id,
                company_id=company_id,
                fallback_vendor_id=vendor_id,
                created_by=int(created_by),
                request_po_number=po_number,
                request_po_id=po_id,
            )
        if classification_includes(doc_classification, "invoice") or isinstance(raw_text.get("invoice"), dict):
            invoice_attachment = persisted_paths[0] if persisted_paths else None
            invoice_import_result = insert_ocr_invoice(
                raw_text,
                db=db,
                tenant_id=tenant_id,
                company_id=company_id,
                fallback_vendor_id=vendor_id,
                created_by=int(created_by),
                request_po_number=po_number,
                request_po_id=po_id,
                attachment_path=invoice_attachment,
            )
            if bearer_token and (invoice_import_result or {}).get("status") == "inserted":
                invoice_payload = invoice_import_result.get("payload") or {}
                invoice_notifications = notify_invoice_uploaded(
                    bearer_token=bearer_token,
                    invoice_no=invoice_payload.get("invoiceNo"),
                    po_number=invoice_payload.get("poNumber") or po_number,
                    vendor_name=vendor_row.company_name,
                    line_count=len(invoice_payload.get("lines") or []) or None,
                )
        if classification_includes(doc_classification, "dispatch note") or isinstance(
            raw_text.get("dispatch_note"), dict
        ):
            dispatch_note_import_result = insert_ocr_dispatch_note(
                raw_text,
                tenant_id=tenant_id,
                company_id=company_id,
                fallback_vendor_id=vendor_id,
                created_by=int(created_by),
                request_po_number=po_number,
                request_po_id=po_id,
            )
        if classification_includes(
            doc_classification, "sales report", "sales"
        ) or isinstance(raw_text.get("sales"), dict):
            sales_import_result = insert_ocr_sales_transactions(
                db,
                raw_text,
                tenant_id=tenant_id,
                company_id=company_id,
                created_by=int(created_by) if str(created_by).isdigit() else None,
            )
        if is_rate_contract:
            rfq_revisions = apply_rate_contract_rfq_revisions(db, vendor_id, raw_text)

    ocr_serializable = build_ocr_serializable(raw_text, document_classification)
    classification_serializable = make_json_serializable(document_classification)

    new_media = MediaAttachment(
        media_name=received_files[0]["filename"],
        media_type=received_files[0]["content_type"],
        media_url=(persisted_paths[0] if persisted_paths else "temporary"),
        context_registry_id=1,
        ocr_raw_text=ocr_serializable,
        tenant_id=tenant_id,
        company_id=company_id,
        created_by=str(created_by),
    )
    db.add(new_media)
    db.commit()
    db.refresh(new_media)

    insert_into_immudb(new_media, ocr_serializable)

    return {
        "vendor_id": vendor_id,
        "status": "completed",
        "files_received": received_files,
        "document_classification": classification_serializable,
        "vendor_items_inserted": vendor_items_inserted,
        "vendor_rates_inserted": vendor_rates_inserted,
        "vendor_fingerprints_synced": vendor_fingerprints_synced,
        "items_rates_inserted": vendor_rates_inserted,
        "vendor_banks_inserted": vendor_banks_inserted,
        "vendor_documents_inserted": vendor_documents_inserted,
        "grn_import": grn_import_result,
        "invoice_import": invoice_import_result,
        "invoice_notifications": invoice_notifications,
        "dispatch_note_import": dispatch_note_import_result,
        "sales_import": sales_import_result,
        "rfq_revisions": rfq_revisions,
        "rate_contract_match_reviews": make_json_serializable(
            rate_contract_match_reviews
        ),
        "raw_text": make_json_serializable(raw_text),
    }
