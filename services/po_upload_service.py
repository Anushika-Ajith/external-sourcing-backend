"""PO upload orchestration: duplicate detection, revisions, history."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload

from models import PurchaseOrderHeader, PurchaseOrderLine, PurchaseOrderHeaderHistory
from services.po_hash import business_hash, sha256_document_hash
from services.po_history_snapshot import snapshot_po_to_history
from services.po_ocr_extract import extract_po_business_from_ocr_raw, ocr_po_from_bytes
from services.po_line_embedding_sync import create_embeddings_for_po_lines
from services.po_line_fingerprint import attach_product_fingerprint_to_line_dict
from services.po_line_qty import qty_fields_for_create
from services.po_amounts import recalculate_po_totals
from services.po_vendor_rate_enrich import resolve_vendor_and_enrich_po_business
from services.next_numbering import get_next_po_number_from_nest

PO_ATTACHMENT_DIR = Path("uploads") / "purchase_orders"


class PoUploadOutcome:
    def __init__(
        self,
        *,
        action: str,
        message: str,
        po_id: Optional[int] = None,
        po_number: Optional[str] = None,
        revision_no: Optional[int] = None,
        document_hash: Optional[str] = None,
        business_hash: Optional[str] = None,
        duplicate: bool = False,
        ocr_skipped: bool = False,
    ):
        self.action = action
        self.message = message
        self.po_id = po_id
        self.po_number = po_number
        self.revision_no = revision_no
        self.document_hash = document_hash
        self.business_hash = business_hash
        self.duplicate = duplicate
        self.ocr_skipped = ocr_skipped

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "message": self.message,
            "po_id": self.po_id,
            "po_number": self.po_number,
            "revision_no": self.revision_no,
            "document_hash": self.document_hash,
            "business_hash": self.business_hash,
            "duplicate": self.duplicate,
            "ocr_skipped": self.ocr_skipped,
        }


def _find_header_by_document_hash(
    db: Session, document_hash: str
) -> Optional[PurchaseOrderHeader]:
    header = (
        db.query(PurchaseOrderHeader)
        .filter(PurchaseOrderHeader.document_hash == document_hash)
        .first()
    )
    if header:
        return header

    hist = (
        db.query(PurchaseOrderHeaderHistory)
        .filter(PurchaseOrderHeaderHistory.document_hash == document_hash)
        .order_by(PurchaseOrderHeaderHistory.history_id.desc())
        .first()
    )
    if hist and hist.po_id:
        return (
            db.query(PurchaseOrderHeader)
            .filter(PurchaseOrderHeader.po_id == hist.po_id)
            .first()
        )
    return None


def _find_current_po(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    po_number: str,
    version_no: int,
) -> Optional[PurchaseOrderHeader]:
    return (
        db.query(PurchaseOrderHeader)
        .options(joinedload(PurchaseOrderHeader.po_lines))
        .filter(
            PurchaseOrderHeader.tenant_id == tenant_id,
            PurchaseOrderHeader.company_id == company_id,
            PurchaseOrderHeader.document_type == document_type,
            PurchaseOrderHeader.po_number == po_number,
            PurchaseOrderHeader.version_no == version_no,
        )
        .first()
    )


def _find_po_by_source_number(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    version_no: int,
    source_po_number: str,
) -> Optional[PurchaseOrderHeader]:
    """Match prior upload by printed/OCR PO number (document_number) or legacy po_number."""
    source = (source_po_number or "").strip()
    if not source:
        return None

    by_document_number = (
        db.query(PurchaseOrderHeader)
        .options(joinedload(PurchaseOrderHeader.po_lines))
        .filter(
            PurchaseOrderHeader.tenant_id == tenant_id,
            PurchaseOrderHeader.company_id == company_id,
            PurchaseOrderHeader.document_type == document_type,
            PurchaseOrderHeader.version_no == version_no,
            PurchaseOrderHeader.document_number == source,
        )
        .first()
    )
    if by_document_number:
        return by_document_number

    # Older uploads stored the OCR value as po_number itself.
    return _find_current_po(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        po_number=source,
        version_no=version_no,
    )


def _persist_attachment(
    file_bytes: bytes, po_id: int, filename: str
) -> str:
    dest_dir = PO_ATTACHMENT_DIR / str(po_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = os.path.splitext(filename or "")[1] or ".pdf"
    dest = dest_dir / f"{uuid.uuid4().hex}{ext}"
    dest.write_bytes(file_bytes)
    return str(dest)


def _to_rate(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _apply_lines_from_business(
    db: Session,
    header: PurchaseOrderHeader,
    lines: List[Dict[str, Any]],
    user_id: int,
) -> None:
    db.query(PurchaseOrderLine).filter(
        PurchaseOrderLine.tenant_id == header.tenant_id,
        PurchaseOrderLine.company_id == header.company_id,
        PurchaseOrderLine.document_type == header.document_type,
        PurchaseOrderLine.po_number == header.po_number,
        PurchaseOrderLine.version_no == header.version_no,
    ).delete(synchronize_session=False)

    inserted_rates: List[Dict[str, Any]] = []
    for line in lines:
        line_no = int(line.get("line_no") or 1)
        item_id = line.get("item_id")
        try:
            item_id_int = int(item_id) if item_id is not None and item_id != "" else None
        except (TypeError, ValueError):
            item_id_int = None

        po_item_rate = _to_rate(line.get("po_item_rate"))
        rate_contract_item_rate = _to_rate(line.get("rate_contract_item_rate"))
        unit_price = _to_rate(line.get("unit_price"))

        # Prefer explicit new columns; fall back so unit_price stays consistent.
        if po_item_rate is None and unit_price is not None and rate_contract_item_rate is None:
            po_item_rate = unit_price
        if unit_price is None:
            unit_price = (
                rate_contract_item_rate
                if rate_contract_item_rate is not None
                else po_item_rate
            )

        qty_fields = qty_fields_for_create(
            suggested_qty=line.get("suggested_qty"),
            qty_ordered=line.get("qty_ordered"),
            buyer_qty=line.get("buyer_qty"),
            supplier_qty=line.get("supplier_qty"),
        )
        line_payload = {
            "item_id": item_id_int,
            "item_no": line.get("item_no"),
            "description": line.get("description"),
            "uom": line.get("uom"),
            "unit_price": unit_price,
            "po_item_rate": po_item_rate,
            "rate_contract_item_rate": rate_contract_item_rate,
            "delivery_date": line.get("delivery_date"),
            "created_by": user_id,
            "updated_by": user_id,
            **qty_fields,
        }
        if header.vendor_id is not None:
            attach_product_fingerprint_to_line_dict(
                db, line_payload, vendor_id=header.vendor_id
            )
        db.add(
            PurchaseOrderLine(
                tenant_id=header.tenant_id,
                company_id=header.company_id,
                document_type=header.document_type,
                po_number=header.po_number,
                version_no=header.version_no,
                line_no=line_no,
                po_id=header.po_id,
                **line_payload,
            )
        )
        inserted_rates.append(
            {
                "line_no": line_no,
                "item_no": line.get("item_no"),
                "po_item_rate": str(po_item_rate) if po_item_rate is not None else None,
                "rate_contract_item_rate": (
                    str(rate_contract_item_rate)
                    if rate_contract_item_rate is not None
                    else None
                ),
                "unit_price": str(unit_price) if unit_price is not None else None,
            }
        )

    print(
        "[PO UPLOAD INSERT] Line rates written to purchase_order_line:",
        {
            "po_number": header.po_number,
            "po_id": header.po_id,
            "lines": inserted_rates,
        },
    )


def _apply_lines_and_embeddings(
    db: Session,
    header: PurchaseOrderHeader,
    lines: List[Dict[str, Any]],
    user_id: int,
) -> None:
    _apply_lines_from_business(db, header, lines, user_id)
    db.flush()
    created_lines = (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.po_id == header.po_id)
        .order_by(PurchaseOrderLine.line_no)
        .all()
    )
    recalculate_po_totals(header, created_lines)
    create_embeddings_for_po_lines(db, header, created_lines)


def _create_new_po(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    version_no: int,
    vendor_id: int,
    business: Dict[str, Any],
    document_hash: str,
    biz_hash: str,
    file_bytes: bytes,
    filename: str,
    user_id: int,
) -> PurchaseOrderHeader:
    header = PurchaseOrderHeader(
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        po_number=business["po_number"],
        version_no=version_no,
        po_date=business["po_date"],
        vendor_id=vendor_id,
        vendor_name_snapshot=business.get("vendor_name"),
        currency_code=business.get("currency_code") or "INR",
        remarks=business.get("remarks"),
        status="OPEN",
        approval_status="DRAFT",
        document_number=business.get("source_po_number") or business.get("document_number"),
        document_hash=document_hash,
        business_hash=biz_hash,
        revision_no=1,
        current_revision=True,
        attachment_count=1,
        current_editor="BUYER",
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(header)
    db.flush()
    header.attachment_path = _persist_attachment(
        file_bytes, po_id=header.po_id, filename=filename
    )
    _apply_lines_and_embeddings(db, header, business.get("lines") or [], user_id)
    return header


def process_po_upload(
    db: Session,
    *,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    tenant_id: int,
    company_id: int,
    document_type: str,
    version_no: int,
    vendor_id: int,
    user_id: int,
    vendor_name: Optional[str] = None,
    department_id: Optional[int] = 1,
) -> PoUploadOutcome:
    document_hash = sha256_document_hash(file_bytes)

    # Step 1–2: exact duplicate file — skip OCR
    existing_by_doc = _find_header_by_document_hash(db, document_hash)
    if existing_by_doc:
        return PoUploadOutcome(
            action="duplicate",
            message="Duplicate upload ignored — this exact file was already uploaded.",
            po_id=existing_by_doc.po_id,
            po_number=existing_by_doc.po_number,
            revision_no=getattr(existing_by_doc, "revision_no", None) or 1,
            document_hash=document_hash,
            business_hash=getattr(existing_by_doc, "business_hash", None),
            duplicate=True,
            ocr_skipped=True,
        )

    # Step 3: OCR / extraction (new document hash)
    raw, ocr_text, doc_classification = ocr_po_from_bytes(
        file_bytes, filename, content_type
    )
    business = extract_po_business_from_ocr_raw(
        raw,
        fallback_vendor_name=vendor_name,
        ocr_text=ocr_text,
        document_classification=doc_classification,
    )

    # Step 3b: resolve vendor by OCR vendor name, then apply vendor_rates
    resolved_vendor_id, business, _vendor_rate_meta = (
        resolve_vendor_and_enrich_po_business(
            db,
            business,
            fallback_vendor_id=vendor_id,
        )
    )
    vendor_id = resolved_vendor_id

    source_po_number = (business.get("source_po_number") or "").strip() or None

    # Business identity uses the printed/OCR PO number when available so
    # re-uploads of the same buyer PO still match after system numbering.
    biz_hash = business_hash(
        po_number=source_po_number or "",
        vendor=business.get("vendor_name") or vendor_name or "",
        lines=business.get("lines") or [],
    )

    existing_po = _find_po_by_source_number(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        document_type=document_type,
        version_no=version_no,
        source_po_number=source_po_number or "",
    )

    # Scenario A — new PO (assign system number from next-numbering)
    if not existing_po:
        try:
            po_number = get_next_po_number_from_nest(
                tenant_id=tenant_id,
                company_id=company_id,
                department_id=department_id,
            )
        except (RuntimeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to allocate PO number from next-numbering: {e}",
            ) from e

        business["po_number"] = po_number
        print(
            "[PO UPLOAD INSERT] Assigned po_number from next-numbering:",
            {
                "po_number": po_number,
                "source_po_number": source_po_number,
                "tenant_id": tenant_id,
                "company_id": company_id,
                "department_id": department_id,
            },
        )

        header = _create_new_po(
            db,
            tenant_id=tenant_id,
            company_id=company_id,
            document_type=document_type,
            version_no=version_no,
            vendor_id=vendor_id,
            business=business,
            document_hash=document_hash,
            biz_hash=biz_hash,
            file_bytes=file_bytes,
            filename=filename,
            user_id=user_id,
        )
        db.flush()
        db.refresh(header)
        created_lines = (
            db.query(PurchaseOrderLine)
            .filter(PurchaseOrderLine.po_id == header.po_id)
            .all()
        )

        snapshot_po_to_history(
            db,
            header,
            created_lines,
            history_action="CREATED",
            history_reason="Initial PO upload",
            history_created_by=user_id,
        )

        db.commit()
        db.refresh(header)
        return PoUploadOutcome(
            action="created",
            message=f"New PO {po_number} created (revision 1).",
            po_id=header.po_id,
            po_number=po_number,
            revision_no=1,
            document_hash=document_hash,
            business_hash=biz_hash,
        )

    po_number = existing_po.po_number
    business["po_number"] = po_number
    if source_po_number and not existing_po.document_number:
        existing_po.document_number = source_po_number

    existing_biz_hash = getattr(existing_po, "business_hash", None)

    # Same business data — attachment-only update (different scan/format/signature)
    if existing_biz_hash and existing_biz_hash == biz_hash:
        attachment_path = _persist_attachment(
            file_bytes, po_id=existing_po.po_id, filename=filename
        )
        existing_po.document_hash = document_hash
        existing_po.attachment_path = attachment_path
        existing_po.attachment_count = (existing_po.attachment_count or 0) + 1
        existing_po.updated_by = user_id
        existing_po.updated_at = datetime.now(timezone.utc)

        snapshot_po_to_history(
            db,
            existing_po,
            existing_po.po_lines or [],
            history_action="ATTACHMENT_UPDATED",
            history_reason="Same business data; document file replaced",
            history_created_by=user_id,
        )

        db.commit()
        return PoUploadOutcome(
            action="attachment_updated",
            message=f"PO {po_number} attachment updated (business data unchanged).",
            po_id=existing_po.po_id,
            po_number=po_number,
            revision_no=getattr(existing_po, "revision_no", None) or 1,
            document_hash=document_hash,
            business_hash=biz_hash,
        )

    # Business data changed — new revision
    current_revision = getattr(existing_po, "revision_no", None) or 1
    lines = list(existing_po.po_lines or [])

    snapshot_po_to_history(
        db,
        existing_po,
        lines,
        history_action="REVISED",
        history_reason="Business data changed on re-upload",
        history_created_by=user_id,
    )

    new_revision = current_revision + 1
    existing_po.po_date = business["po_date"]
    existing_po.vendor_id = vendor_id
    existing_po.vendor_name_snapshot = business.get("vendor_name")
    existing_po.currency_code = business.get("currency_code") or existing_po.currency_code
    existing_po.remarks = business.get("remarks")
    existing_po.document_hash = document_hash
    existing_po.business_hash = biz_hash
    existing_po.revision_no = new_revision
    existing_po.current_revision = True
    existing_po.attachment_path = _persist_attachment(
        file_bytes, po_id=existing_po.po_id, filename=filename
    )
    existing_po.attachment_count = (existing_po.attachment_count or 0) + 1
    existing_po.updated_by = user_id
    existing_po.updated_at = datetime.now(timezone.utc)

    _apply_lines_and_embeddings(
        db, existing_po, business.get("lines") or [], user_id
    )

    db.commit()
    db.refresh(existing_po)

    return PoUploadOutcome(
        action="revision_created",
        message=f"PO {po_number} revised to revision {new_revision}.",
        po_id=existing_po.po_id,
        po_number=po_number,
        revision_no=new_revision,
        document_hash=document_hash,
        business_hash=biz_hash,
    )
