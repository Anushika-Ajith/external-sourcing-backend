"""Create document_item_embeddings rows when purchase order lines are created."""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from models import DocumentItemEmbedding, PurchaseOrderHeader, PurchaseOrderLine
from services.document_line_embedding_sync import (
    build_fingerprint_for_line,
    persist_document_line_embedding,
)

logger = logging.getLogger(__name__)


def create_embedding_for_po_line(
    db: Session,
    header: PurchaseOrderHeader,
    line: PurchaseOrderLine,
) -> Optional[DocumentItemEmbedding]:
    """Build fingerprint + embedding for one PO line and persist it."""
    fingerprint = build_fingerprint_for_line(
        db,
        vendor_id=header.vendor_id,
        description=line.description,
        uom=line.uom,
        item_id=line.item_id,
        item_no=line.item_no,
        po_line=line,
        po_header=header,
    )

    return persist_document_line_embedding(
        db,
        tenant_id=header.tenant_id,
        company_id=header.company_id,
        source_document_type=header.document_type,
        source_document_no=header.po_number,
        source_line_no=line.line_no,
        item_id=line.item_id,
        fingerprint=fingerprint,
    )


def create_embeddings_for_po_lines(
    db: Session,
    header: PurchaseOrderHeader,
    lines: Iterable[PurchaseOrderLine],
) -> List[DocumentItemEmbedding]:
    """
    Create document_item_embeddings for each PO line.
    Failures on individual lines are logged; PO creation should continue.
    """
    created: List[DocumentItemEmbedding] = []
    for line in lines:
        try:
            row = create_embedding_for_po_line(db, header, line)
            if row is not None:
                created.append(row)
        except Exception as exc:
            logger.exception(
                "Failed to create embedding for PO %s line %s: %s",
                header.po_number,
                line.line_no,
                exc,
            )
    return created
