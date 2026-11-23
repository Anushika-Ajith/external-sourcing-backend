"""Shared helpers for document_item_embeddings (PO, invoice, GRN, etc.)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from models import (
    DocumentItemEmbedding,
    PurchaseOrderHeader,
    PurchaseOrderLine,
    VendorItem,
)
from services.embedding_service import embed_text
from services.item_fingerprint import (
    build_embedding_text,
    build_fingerprint_from_line_data,
    build_fingerprint_from_po_line,
    normalize_fingerprint,
)

logger = logging.getLogger(__name__)


def resolve_vendor_item(
    db: Session,
    vendor_id: int,
    *,
    item_id: Optional[int] = None,
    item_no: Optional[str] = None,
) -> Optional[VendorItem]:
    q = db.query(VendorItem).filter(VendorItem.vendor_id == vendor_id)
    if item_id is not None:
        hit = q.filter(VendorItem.item_id == item_id).first()
        if hit:
            return hit
    if item_no:
        hit = q.filter(VendorItem.erp_code == item_no).first()
        if hit:
            return hit
        hit = q.filter(VendorItem.vendor_product_code == item_no).first()
        if hit:
            return hit
    return None


def find_po_line(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    po_number: str,
    po_line_no: int,
    version_no: Optional[int] = None,
) -> Optional[PurchaseOrderLine]:
    q = db.query(PurchaseOrderLine).filter(
        PurchaseOrderLine.tenant_id == tenant_id,
        PurchaseOrderLine.company_id == company_id,
        PurchaseOrderLine.po_number == po_number,
        PurchaseOrderLine.line_no == po_line_no,
    )
    if version_no is not None:
        q = q.filter(PurchaseOrderLine.version_no == version_no)
    return q.order_by(PurchaseOrderLine.version_no.desc()).first()


def get_po_header_for_line(
    db: Session,
    po_line: PurchaseOrderLine,
) -> Optional[PurchaseOrderHeader]:
    return (
        db.query(PurchaseOrderHeader)
        .filter(
            PurchaseOrderHeader.tenant_id == po_line.tenant_id,
            PurchaseOrderHeader.company_id == po_line.company_id,
            PurchaseOrderHeader.document_type == po_line.document_type,
            PurchaseOrderHeader.po_number == po_line.po_number,
            PurchaseOrderHeader.version_no == po_line.version_no,
        )
        .first()
    )


def get_existing_po_line_embedding(
    db: Session,
    po_line: PurchaseOrderLine,
) -> Optional[DocumentItemEmbedding]:
    return (
        db.query(DocumentItemEmbedding)
        .filter(
            DocumentItemEmbedding.tenant_id == po_line.tenant_id,
            DocumentItemEmbedding.company_id == po_line.company_id,
            DocumentItemEmbedding.source_document_type == po_line.document_type,
            DocumentItemEmbedding.source_document_no == po_line.po_number,
            DocumentItemEmbedding.source_line_no == po_line.line_no,
        )
        .first()
    )


def delete_document_line_embedding(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    source_document_type: str,
    source_document_no: str,
    source_line_no: int,
) -> None:
    db.query(DocumentItemEmbedding).filter(
        DocumentItemEmbedding.tenant_id == tenant_id,
        DocumentItemEmbedding.company_id == company_id,
        DocumentItemEmbedding.source_document_type == source_document_type,
        DocumentItemEmbedding.source_document_no == source_document_no,
        DocumentItemEmbedding.source_line_no == source_line_no,
    ).delete(synchronize_session=False)


def persist_document_line_embedding(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    source_document_type: str,
    source_document_no: str,
    source_line_no: int,
    item_id: Optional[int],
    fingerprint: Dict[str, Any],
    embedding_text: Optional[str] = None,
    embedding: Optional[list] = None,
    recompute_embedding: bool = True,
) -> Optional[DocumentItemEmbedding]:
    text = (embedding_text or build_embedding_text(fingerprint)).strip()
    if not text:
        logger.info(
            "Skipping embedding for %s %s line %s — no fingerprint text",
            source_document_type,
            source_document_no,
            source_line_no,
        )
        return None

    vector = embedding
    if vector is None and recompute_embedding:
        try:
            vector = embed_text(text)
        except Exception as exc:
            logger.warning(
                "Embedding failed for %s %s line %s: %s",
                source_document_type,
                source_document_no,
                source_line_no,
                exc,
            )
            vector = None

    delete_document_line_embedding(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        source_document_type=source_document_type,
        source_document_no=source_document_no,
        source_line_no=source_line_no,
    )

    row = DocumentItemEmbedding(
        tenant_id=tenant_id,
        company_id=company_id,
        source_document_type=source_document_type,
        source_document_no=source_document_no,
        source_line_no=source_line_no,
        item_id=item_id,
        fingerprint=fingerprint,
        embedding_text=text,
        embedding=vector,
    )
    db.add(row)
    return row


def build_fingerprint_for_line(
    db: Session,
    *,
    vendor_id: int,
    description: Optional[str],
    uom: Optional[str],
    item_id: Optional[int] = None,
    item_no: Optional[str] = None,
    po_line: Optional[PurchaseOrderLine] = None,
    po_header: Optional[PurchaseOrderHeader] = None,
) -> Dict[str, Any]:
    """Prefer PO line / existing PO embedding fingerprint; else parse line text."""
    if po_line is not None:
        stored_fp = getattr(po_line, "product_fingerprint", None)
        if isinstance(stored_fp, dict) and stored_fp:
            return normalize_fingerprint(stored_fp, uom_fallback=po_line.uom or uom)

        existing = get_existing_po_line_embedding(db, po_line)
        if existing and isinstance(existing.fingerprint, dict) and existing.fingerprint:
            return existing.fingerprint

        header = po_header or get_po_header_for_line(db, po_line)
        vendor_item = None
        if header is not None:
            vendor_item = resolve_vendor_item(
                db,
                header.vendor_id,
                item_id=po_line.item_id or item_id,
                item_no=po_line.item_no or item_no,
            )
        return build_fingerprint_from_po_line(po_line, vendor_item)

    vendor_item = resolve_vendor_item(
        db,
        vendor_id,
        item_id=item_id,
        item_no=item_no,
    )
    return build_fingerprint_from_line_data(
        description=description,
        uom=uom,
        item_no=item_no,
        vendor_item=vendor_item,
    )
