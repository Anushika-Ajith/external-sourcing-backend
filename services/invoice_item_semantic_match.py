"""
Semantic invoice line matching: description -> item_id -> vendor_items -> rate.

Uses cosine similarity against document_item_embeddings and vendor_item_fingerprint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models import DocumentItemEmbedding, VendorItem, VendorItemFingerprint, VendorRate
from database import VectorSessionLocal
from services.document_line_embedding_sync import resolve_vendor_item
from services.embedding_service import embed_text
from services.item_fingerprint import build_embedding_text, build_fingerprint_from_line_data

logger = logging.getLogger(__name__)

DEFAULT_MATCH_LIMIT = 5
DEFAULT_MAX_DISTANCE = 0.4


@dataclass
class SemanticMatchCandidate:
    distance: float
    match_source: str
    item_id: Optional[int] = None
    vendor_item_id: Optional[int] = None
    embedding_id: Optional[int] = None
    fingerprint_id: Optional[int] = None
    fingerprint: Optional[Dict[str, Any]] = None
    embedding_text: Optional[str] = None


@dataclass
class InvoiceLineVendorMatch:
    description: str
    vendor_item_id: Optional[int] = None
    vendor_item_name: Optional[str] = None
    item_id: Optional[int] = None
    rate: Optional[Decimal] = None
    currency: Optional[str] = None
    distance: Optional[float] = None
    match_source: Optional[str] = None
    fingerprint: Optional[Dict[str, Any]] = None
    matched: bool = False
    details: Dict[str, Any] = field(default_factory=dict)


def embed_invoice_line_description(
    description: str,
    *,
    uom: Optional[str] = None,
) -> List[float]:
    return _embed_description(description, uom=uom)


def _embed_description(
    description: str,
    *,
    uom: Optional[str] = None,
) -> List[float]:
    fingerprint = build_fingerprint_from_line_data(
        description=description,
        uom=uom,
        use_gemini=True,
    )
    text = build_embedding_text(fingerprint)
    if not text.strip():
        text = description.strip()
    return embed_text(text)


def _search_document_item_embeddings(
    db: Session,
    query_vector: List[float],
    *,
    tenant_id: int,
    company_id: int,
    limit: int,
    source_document_type: Optional[str] = None,
    source_document_no: Optional[str] = None,
) -> List[SemanticMatchCandidate]:
    distance = DocumentItemEmbedding.embedding.cosine_distance(query_vector).label(
        "distance"
    )
    q = db.query(DocumentItemEmbedding, distance).filter(
        DocumentItemEmbedding.tenant_id == tenant_id,
        DocumentItemEmbedding.company_id == company_id,
        DocumentItemEmbedding.embedding.isnot(None),
    )
    if source_document_type:
        q = q.filter(
            DocumentItemEmbedding.source_document_type == source_document_type
        )
    if source_document_no:
        q = q.filter(DocumentItemEmbedding.source_document_no == source_document_no)

    rows = q.order_by(distance).limit(limit).all()
    return [
        SemanticMatchCandidate(
            distance=float(dist),
            match_source="document_item_embeddings",
            item_id=row.item_id,
            embedding_id=row.embedding_id,
            fingerprint=row.fingerprint if isinstance(row.fingerprint, dict) else None,
            embedding_text=row.embedding_text,
        )
        for row, dist in rows
    ]


def _search_vendor_item_fingerprints(
    db: Session,
    query_vector: List[float],
    *,
    vendor_id: int,
    limit: int,
) -> List[SemanticMatchCandidate]:
    """Search VECTOR_DB.vendor_item_fingerprint; filter by vendor via main-DB vendor_items."""
    vendor_item_ids = [
        row[0]
        for row in db.query(VendorItem.id).filter(VendorItem.vendor_id == vendor_id).all()
    ]
    if not vendor_item_ids:
        return []

    distance = VendorItemFingerprint.embedding.cosine_distance(query_vector).label(
        "distance"
    )
    vector_db = VectorSessionLocal()
    try:
        rows = (
            vector_db.query(VendorItemFingerprint, distance)
            .filter(
                VendorItemFingerprint.vendor_item_id.in_(vendor_item_ids),
                VendorItemFingerprint.embedding.isnot(None),
            )
            .order_by(distance)
            .limit(limit)
            .all()
        )
        return [
            SemanticMatchCandidate(
                distance=float(dist),
                match_source="vendor_item_fingerprint",
                vendor_item_id=row.vendor_item_id,
                fingerprint_id=row.id,
                fingerprint=row.fingerprint_json
                if isinstance(row.fingerprint_json, dict)
                else None,
            )
            for row, dist in rows
        ]
    finally:
        vector_db.close()


def get_latest_vendor_rate(
    db: Session,
    *,
    vendor_item_id: int,
    vendor_id: int,
) -> Optional[VendorRate]:
    rates = (
        db.query(VendorRate)
        .filter(
            VendorRate.item_id == vendor_item_id,
            VendorRate.vendor_id == vendor_id,
        )
        .all()
    )
    if not rates:
        return (
            db.query(VendorRate)
            .filter(VendorRate.item_id == vendor_item_id)
            .order_by(VendorRate.created_at.desc())
            .first()
        )

    def _sort_key(rate: VendorRate) -> datetime:
        if rate.valid_from is not None:
            return datetime.combine(rate.valid_from, datetime.min.time())
        if rate.created_at is not None:
            return rate.created_at
        return datetime.min

    return sorted(rates, key=_sort_key, reverse=True)[0]


def match_invoice_description_to_vendor_rate(
    db: Session,
    *,
    description: str,
    vendor_id: int,
    tenant_id: int,
    company_id: int,
    uom: Optional[str] = None,
    limit: int = DEFAULT_MATCH_LIMIT,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    po_number: Optional[str] = None,
    po_document_type: Optional[str] = None,
) -> InvoiceLineVendorMatch:
    """
    Match an invoice line description to a vendor item and contract rate via
    semantic search over document_item_embeddings and vendor_item_fingerprint.

  Returns vendor_items.id, buyer item_id, and the latest vendor_rates row when found.
    """
    result = InvoiceLineVendorMatch(description=description.strip(), matched=False)
    if not result.description:
        result.details["reason"] = "empty_description"
        return result

    try:
        query_vector = _embed_description(result.description, uom=uom)
    except Exception as exc:
        logger.warning("Could not embed invoice description for matching: %s", exc)
        result.details["reason"] = "embedding_failed"
        result.details["error"] = str(exc)
        return result

    candidates: List[SemanticMatchCandidate] = []
    try:
        candidates.extend(
            _search_document_item_embeddings(
                db,
                query_vector,
                tenant_id=tenant_id,
                company_id=company_id,
                limit=limit,
                source_document_type=po_document_type,
                source_document_no=po_number,
            )
        )
        candidates.extend(
            _search_vendor_item_fingerprints(
                db,
                query_vector,
                vendor_id=vendor_id,
                limit=limit,
            )
        )
    except Exception as exc:
        logger.warning("Semantic search failed for invoice line: %s", exc)
        result.details["reason"] = "search_failed"
        result.details["error"] = str(exc)
        return result

    if not candidates:
        result.details["reason"] = "no_candidates"
        return result

    candidates.sort(key=lambda c: c.distance)
    best = candidates[0]
    if best.distance > max_distance:
        result.distance = best.distance
        result.details["reason"] = "below_similarity_threshold"
        result.details["best_distance"] = best.distance
        result.details["max_distance"] = max_distance
        return result

    vendor_item: Optional[VendorItem] = None
    if best.vendor_item_id is not None:
        vendor_item = (
            db.query(VendorItem)
            .filter(
                VendorItem.id == best.vendor_item_id,
                VendorItem.vendor_id == vendor_id,
            )
            .first()
        )
    elif best.item_id is not None:
        vendor_item = resolve_vendor_item(
            db,
            vendor_id,
            item_id=best.item_id,
        )

    if vendor_item is None and best.item_id is not None:
        vendor_item = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == vendor_id,
                VendorItem.item_id == best.item_id,
            )
            .first()
        )

    if vendor_item is None:
        result.distance = best.distance
        result.item_id = best.item_id
        result.match_source = best.match_source
        result.details["reason"] = "vendor_item_not_found"
        return result

    rate_row = get_latest_vendor_rate(
        db,
        vendor_item_id=vendor_item.id,
        vendor_id=vendor_id,
    )

    result.matched = True
    result.vendor_item_id = vendor_item.id
    result.vendor_item_name = vendor_item.item_name
    result.item_id = vendor_item.item_id or best.item_id
    result.distance = best.distance
    result.match_source = best.match_source
    result.fingerprint = best.fingerprint
    result.details = {
        "embedding_id": best.embedding_id,
        "fingerprint_id": best.fingerprint_id,
        "embedding_text": best.embedding_text,
    }

    if rate_row is not None:
        result.rate = rate_row.rate
        result.currency = rate_row.currency
    else:
        result.details["reason"] = "rate_not_found"

    return result


def enrich_invoice_payload_with_semantic_matches(
    db: Session,
    payload: Dict[str, Any],
    *,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> Dict[str, Any]:
    """
    For each invoice line missing itemId/unitPrice, run semantic matching and fill
    itemId + unitPrice from vendor_items / vendor_rates when a match is found.
    """
    tenant_id = payload.get("tenantId") or payload.get("tenant_id")
    company_id = payload.get("companyId") or payload.get("company_id")
    vendor_id = payload.get("vendorId") or payload.get("vendor_id")
    po_number = payload.get("poNumber") or payload.get("po_number")
    po_document_type = (
        payload.get("poDocumentType")
        or payload.get("po_document_type")
        or ("PO" if po_number else None)
    )
    lines = payload.get("lines")

    if not all([tenant_id, company_id, vendor_id]) or not isinstance(lines, list):
        logger.info(
            "[INVOICE MATCH] step=semantic_rate_skip reason=missing_scope_or_lines"
        )
        return payload

    logger.info(
        "[INVOICE MATCH] step=semantic_rate_start vendor_id=%s po_number=%s lines=%s",
        vendor_id,
        po_number,
        len(lines),
    )
    matches: List[Dict[str, Any]] = []
    for line in lines:
        if not isinstance(line, dict):
            continue

        description = line.get("description") or line.get("itemDescription")
        if not description or not str(description).strip():
            continue

        has_item = line.get("itemId") is not None or line.get("item_id") is not None
        has_price = line.get("unitPrice") is not None or line.get("unit_price") is not None
        line_status = str(line.get("lineStatus") or line.get("line_status") or "").upper()
        line_no = line.get("lineNo") or line.get("line_no")
        if line_status.startswith("PENDING_"):
            logger.info(
                "[INVOICE MATCH] step=semantic_rate_line line_no=%s skipped "
                "reason=pending_match_review status=%s",
                line_no,
                line_status,
            )
            continue
        if has_item and has_price:
            logger.info(
                "[INVOICE MATCH] step=semantic_rate_line line_no=%s skipped "
                "reason=already_has_item_and_price",
                line_no,
            )
            continue

        logger.info(
            "[INVOICE MATCH] step=semantic_rate_line line_no=%s description=%r",
            line_no,
            str(description)[:80],
        )
        match = match_invoice_description_to_vendor_rate(
            db,
            description=str(description),
            vendor_id=int(vendor_id),
            tenant_id=int(tenant_id),
            company_id=int(company_id),
            uom=line.get("uom"),
            max_distance=max_distance,
            po_number=str(po_number).strip() if po_number else None,
            po_document_type=str(po_document_type).strip() if po_document_type else None,
        )
        matches.append(
            {
                "lineNo": line_no,
                "description": match.description,
                "matched": match.matched,
                "item_id": match.item_id,
                "vendor_item_id": match.vendor_item_id,
                "rate": float(match.rate) if match.rate is not None else None,
                "distance": match.distance,
                "match_source": match.match_source,
                "details": match.details,
            }
        )

        if match.matched:
            logger.info(
                "[INVOICE MATCH] step=semantic_rate_line line_no=%s result=matched "
                "item_id=%s vendor_item_id=%s rate=%s source=%s distance=%s",
                line_no,
                match.item_id,
                match.vendor_item_id,
                match.rate,
                match.match_source,
                match.distance,
            )
            if not has_item and match.item_id is not None:
                line["itemId"] = match.item_id
            if not has_price and match.rate is not None:
                line["unitPrice"] = float(match.rate)
        else:
            logger.info(
                "[INVOICE MATCH] step=semantic_rate_line line_no=%s result=no_match "
                "details=%s",
                line_no,
                match.details,
            )

    if matches:
        payload["semantic_matches"] = matches
    logger.info(
        "[INVOICE MATCH] step=semantic_rate_done checked=%s matched=%s",
        len(matches),
        sum(1 for m in matches if m.get("matched")),
    )
    return payload
