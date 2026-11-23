"""Create document_item_embeddings when invoice lines are created."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from models import DocumentItemEmbedding
from services.document_line_embedding_sync import (
    build_fingerprint_for_line,
    find_po_line,
    get_existing_po_line_embedding,
    get_po_header_for_line,
    persist_document_line_embedding,
)

logger = logging.getLogger(__name__)


def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_invoice_no(
    payload: Dict[str, Any],
    api_response: Any = None,
) -> Optional[str]:
    for key in ("invoiceNo", "invoice_no"):
        if _present(payload.get(key)):
            return str(payload[key]).strip()

    if isinstance(api_response, dict):
        for key in ("invoiceNo", "invoice_no", "documentNumber", "document_number"):
            if _present(api_response.get(key)):
                return str(api_response[key]).strip()
        nested = api_response.get("header")
        if isinstance(nested, dict):
            for key in ("invoiceNo", "invoice_no"):
                if _present(nested.get(key)):
                    return str(nested[key]).strip()

    po_number = payload.get("poNumber") or payload.get("po_number")
    if _present(po_number):
        return f"IN-{str(po_number).strip()}"
    return None


def _iter_invoice_lines(payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    lines = payload.get("lines") or []
    if not isinstance(lines, list):
        return []
    return [line for line in lines if isinstance(line, dict)]


def create_embedding_for_invoice_line(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    document_type: str,
    invoice_no: str,
    vendor_id: int,
    po_number: Optional[str],
    line: Dict[str, Any],
    version_no: Optional[int] = None,
) -> Optional[DocumentItemEmbedding]:
    line_no = _int_or_none(line.get("lineNo") or line.get("line_no")) or 1
    po_line_no = _int_or_none(line.get("poLineNo") or line.get("po_line_no")) or line_no
    item_id = _int_or_none(line.get("itemId") or line.get("item_id"))
    description = line.get("description")
    uom = line.get("uom")
    item_no = line.get("itemNo") or line.get("item_no")

    po_line = None
    po_header = None
    existing_po_embedding = None
    if _present(po_number):
        po_line = find_po_line(
            db,
            tenant_id=tenant_id,
            company_id=company_id,
            po_number=str(po_number).strip(),
            po_line_no=po_line_no,
            version_no=version_no,
        )
        if po_line is not None:
            po_header = get_po_header_for_line(db, po_line)
            existing_po_embedding = get_existing_po_line_embedding(db, po_line)

    fingerprint = build_fingerprint_for_line(
        db,
        vendor_id=vendor_id,
        description=description,
        uom=uom,
        item_id=item_id,
        item_no=item_no,
        po_line=po_line,
        po_header=po_header,
    )

    reuse_text = None
    reuse_vector = None
    if existing_po_embedding is not None:
        if existing_po_embedding.embedding_text:
            reuse_text = existing_po_embedding.embedding_text
        if existing_po_embedding.embedding is not None:
            reuse_vector = list(existing_po_embedding.embedding)

    return persist_document_line_embedding(
        db,
        tenant_id=tenant_id,
        company_id=company_id,
        source_document_type=document_type,
        source_document_no=invoice_no,
        source_line_no=line_no,
        item_id=item_id,
        fingerprint=fingerprint,
        embedding_text=reuse_text,
        embedding=reuse_vector,
        recompute_embedding=reuse_vector is None,
    )


def create_embeddings_for_invoice_payload(
    db: Session,
    payload: Dict[str, Any],
    *,
    api_response: Any = None,
) -> List[DocumentItemEmbedding]:
    """
    Create document_item_embeddings for each invoice line in the ERP payload.
    Fingerprints prefer linked PO lines when poNumber/poLineNo are present.
    """
    tenant_id = _int_or_none(payload.get("tenantId") or payload.get("tenant_id"))
    company_id = _int_or_none(payload.get("companyId") or payload.get("company_id"))
    vendor_id = _int_or_none(payload.get("vendorId") or payload.get("vendor_id"))
    document_type = str(payload.get("documentType") or payload.get("document_type") or "IN")
    po_number = payload.get("poNumber") or payload.get("po_number")
    version_no = _int_or_none(payload.get("versionNo") or payload.get("version_no"))

    if tenant_id is None or company_id is None or vendor_id is None:
        logger.warning("Skipping invoice embeddings — missing tenant/company/vendor ids")
        return []

    invoice_no = _pick_invoice_no(payload, api_response)
    if not invoice_no:
        logger.warning("Skipping invoice embeddings — invoice number unavailable")
        return []

    created: List[DocumentItemEmbedding] = []
    for line in _iter_invoice_lines(payload):
        try:
            row = create_embedding_for_invoice_line(
                db,
                tenant_id=tenant_id,
                company_id=company_id,
                document_type=document_type,
                invoice_no=invoice_no,
                vendor_id=vendor_id,
                po_number=str(po_number).strip() if _present(po_number) else None,
                line=line,
                version_no=version_no,
            )
            if row is not None:
                created.append(row)
        except Exception as exc:
            logger.exception(
                "Failed to create embedding for invoice %s line %s: %s",
                invoice_no,
                line.get("lineNo") or line.get("line_no"),
                exc,
            )

    return created
