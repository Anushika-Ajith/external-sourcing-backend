"""
Resolve purchase order for invoices missing po_number via semantic matching.

Compares invoice line embeddings against document_item_embeddings on pending POs
for the same vendor; the PO with the strongest line-level matches wins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import DocumentItemEmbedding, PurchaseOrderHeader
from services.invoice_item_semantic_match import embed_invoice_line_description

logger = logging.getLogger(__name__)

PENDING_PO_STATUSES = ("PENDING", "OPEN")
DEFAULT_MAX_DISTANCE = 0.4


@dataclass
class PendingPoMatchScore:
    po_id: int
    po_number: str
    document_type: str
    version_no: int
    matched_line_count: int = 0
    total_invoice_lines: int = 0
    average_distance: Optional[float] = None
    line_distances: List[Optional[float]] = field(default_factory=list)

    @property
    def match_ratio(self) -> float:
        if self.total_invoice_lines <= 0:
            return 0.0
        return self.matched_line_count / self.total_invoice_lines


@dataclass
class InvoicePoSemanticMatchResult:
    matched: bool = False
    po_number: Optional[str] = None
    po_id: Optional[int] = None
    document_type: Optional[str] = None
    version_no: Optional[int] = None
    matched_line_count: int = 0
    total_invoice_lines: int = 0
    average_distance: Optional[float] = None
    match_ratio: Optional[float] = None
    candidate_scores: List[Dict[str, Any]] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def list_pending_purchase_orders(
    db: Session,
    *,
    vendor_id: int,
    tenant_id: int,
    company_id: int,
) -> List[PurchaseOrderHeader]:
    pending_statuses = [status.upper() for status in PENDING_PO_STATUSES]
    return (
        db.query(PurchaseOrderHeader)
        .filter(
            PurchaseOrderHeader.vendor_id == vendor_id,
            PurchaseOrderHeader.tenant_id == tenant_id,
            PurchaseOrderHeader.company_id == company_id,
            or_(
                PurchaseOrderHeader.current_revision.is_(True),
                PurchaseOrderHeader.current_revision.is_(None),
            ),
            func.upper(PurchaseOrderHeader.status).in_(pending_statuses),
        )
        .order_by(PurchaseOrderHeader.created_at.desc())
        .all()
    )


def _iter_invoice_line_descriptions(
    raw_text: Dict[str, Any],
) -> List[Tuple[str, Optional[str]]]:
    invoice = raw_text.get("invoice")
    if not isinstance(invoice, dict):
        return []

    lines = invoice.get("lines")
    if not isinstance(lines, list):
        return []

    result: List[Tuple[str, Optional[str]]] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        description = line.get("description") or line.get("item_name")
        if not description or not str(description).strip():
            continue
        uom = line.get("uom")
        result.append((str(description).strip(), uom))
    return result


def _best_embedding_distance_for_po(
    db: Session,
    query_vector: List[float],
    po: PurchaseOrderHeader,
) -> Optional[float]:
    distance_expr = DocumentItemEmbedding.embedding.cosine_distance(query_vector).label(
        "distance"
    )
    row = (
        db.query(distance_expr)
        .filter(
            DocumentItemEmbedding.tenant_id == po.tenant_id,
            DocumentItemEmbedding.company_id == po.company_id,
            DocumentItemEmbedding.source_document_type == po.document_type,
            DocumentItemEmbedding.source_document_no == po.po_number,
            DocumentItemEmbedding.embedding.isnot(None),
        )
        .order_by(distance_expr)
        .first()
    )
    if not row or row[0] is None:
        return None
    return float(row[0])


def score_pending_po_against_invoice_lines(
    db: Session,
    po: PurchaseOrderHeader,
    invoice_lines: List[Tuple[str, Optional[str]]],
    *,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> PendingPoMatchScore:
    score = PendingPoMatchScore(
        po_id=int(po.po_id),
        po_number=str(po.po_number),
        document_type=str(po.document_type),
        version_no=int(po.version_no or 1),
        total_invoice_lines=len(invoice_lines),
    )

    distances: List[Optional[float]] = []
    matched = 0
    for description, uom in invoice_lines:
        try:
            query_vector = embed_invoice_line_description(description, uom=uom)
        except Exception as exc:
            logger.warning(
                "Could not embed invoice line for PO matching (%s): %s",
                description[:80],
                exc,
            )
            distances.append(None)
            continue

        best_distance = _best_embedding_distance_for_po(db, query_vector, po)
        distances.append(best_distance)
        if best_distance is not None and best_distance <= max_distance:
            matched += 1

    score.matched_line_count = matched
    score.line_distances = distances
    valid = [d for d in distances if d is not None]
    score.average_distance = (
        sum(valid) / len(valid) if valid else None
    )
    return score


def resolve_po_for_invoice_without_number(
    db: Session,
    raw_text: Dict[str, Any],
    *,
    vendor_id: int,
    tenant_id: int,
    company_id: int,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> InvoicePoSemanticMatchResult:
    """
    When invoice OCR has no po_number, match invoice lines against embeddings on
    pending POs for the vendor and pick the PO with the strongest match.
    """
    result = InvoicePoSemanticMatchResult()
    invoice_lines = _iter_invoice_line_descriptions(raw_text)
    result.total_invoice_lines = len(invoice_lines)

    logger.info(
        "[INVOICE MATCH] step=po_semantic_start vendor_id=%s tenant_id=%s "
        "company_id=%s invoice_lines=%s",
        vendor_id,
        tenant_id,
        company_id,
        len(invoice_lines),
    )

    if not invoice_lines:
        result.details["reason"] = "no_invoice_lines"
        logger.warning(
            "[INVOICE MATCH] step=po_semantic_done result=failed reason=no_invoice_lines"
        )
        return result

    pending_pos = list_pending_purchase_orders(
        db,
        vendor_id=vendor_id,
        tenant_id=tenant_id,
        company_id=company_id,
    )
    logger.info(
        "[INVOICE MATCH] step=po_semantic_pending_pos count=%s",
        len(pending_pos),
    )
    if not pending_pos:
        result.details["reason"] = "no_pending_purchase_orders"
        logger.warning(
            "[INVOICE MATCH] step=po_semantic_done result=failed "
            "reason=no_pending_purchase_orders"
        )
        return result

    scored: List[PendingPoMatchScore] = []
    for po in pending_pos:
        score = score_pending_po_against_invoice_lines(
            db,
            po,
            invoice_lines,
            max_distance=max_distance,
        )
        logger.info(
            "[INVOICE MATCH] step=po_semantic_score po_number=%s matched_lines=%s/%s "
            "avg_distance=%s",
            score.po_number,
            score.matched_line_count,
            score.total_invoice_lines,
            score.average_distance,
        )
        if score.average_distance is None:
            continue
        scored.append(score)

    result.candidate_scores = [
        {
            "po_id": s.po_id,
            "po_number": s.po_number,
            "document_type": s.document_type,
            "version_no": s.version_no,
            "matched_line_count": s.matched_line_count,
            "total_invoice_lines": s.total_invoice_lines,
            "match_ratio": s.match_ratio,
            "average_distance": s.average_distance,
            "line_distances": s.line_distances,
        }
        for s in scored
    ]

    if not scored:
        result.details["reason"] = "no_po_embeddings_for_semantic_match"
        return result

    scored.sort(
        key=lambda s: (
            -s.matched_line_count,
            s.average_distance if s.average_distance is not None else 999.0,
            -s.match_ratio,
        )
    )
    best = scored[0]

    if best.matched_line_count <= 0:
        result.details["reason"] = "no_line_matches_below_threshold"
        result.details["max_distance"] = max_distance
        result.average_distance = best.average_distance
        return result

    result.matched = True
    result.po_number = best.po_number
    result.po_id = best.po_id
    result.document_type = best.document_type
    result.version_no = best.version_no
    result.matched_line_count = best.matched_line_count
    result.average_distance = best.average_distance
    result.match_ratio = best.match_ratio
    result.details = {
        "match_method": "pending_po_semantic_embedding",
        "pending_po_count": len(pending_pos),
        "scored_po_count": len(scored),
    }

    best_match_print = {
        "po_number": best.po_number,
        "po_id": best.po_id,
        "document_type": best.document_type,
        "version_no": best.version_no,
        "matched_line_count": best.matched_line_count,
        "total_invoice_lines": best.total_invoice_lines,
        "match_ratio": best.match_ratio,
        "average_distance": best.average_distance,
        "line_distances": best.line_distances,
        "max_distance": max_distance,
        "pending_po_count": len(pending_pos),
        "scored_po_count": len(scored),
        "top_candidates": result.candidate_scores[:5],
    }
    print(
        "[INVOICE PO SEMANTIC MATCH] Best match:",
        best_match_print,
    )
    logger.info(
        "[INVOICE MATCH] step=po_semantic_done result=matched po_number=%s po_id=%s "
        "matched_lines=%s/%s avg_distance=%.4f",
        result.po_number,
        result.po_id,
        result.matched_line_count,
        result.total_invoice_lines,
        result.average_distance or -1.0,
    )
    return result


def apply_resolved_po_to_invoice_raw_text(
    raw_text: Dict[str, Any],
    match: InvoicePoSemanticMatchResult,
) -> Dict[str, Any]:
    if not match.matched or not match.po_number:
        return raw_text

    if not isinstance(raw_text, dict):
        return raw_text

    invoice = raw_text.get("invoice")
    if not isinstance(invoice, dict):
        invoice = {}
        raw_text["invoice"] = invoice

    header = invoice.get("header")
    if not isinstance(header, dict):
        header = {}
        invoice["header"] = header

    header["po_number"] = match.po_number
    if match.po_id is not None:
        header["po_id"] = match.po_id

    lines = invoice.get("lines")
    if isinstance(lines, list):
        for line in lines:
            if not isinstance(line, dict):
                continue
            line.setdefault("po_number", match.po_number)
            if match.po_id is not None:
                line.setdefault("po_id", match.po_id)

    return raw_text
