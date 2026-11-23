"""
Invoice product matching engine.

Candidate pool is always scoped to the related PO products for that vendor.

Engine loop for each invoice line:
  1. Product-name / alias search against the PO pool
  2. If that fails:
       a. Build product_fingerprint via OpenAI (from OCR line text)
       b. Run fingerprint filters IN ORDER:
            category → manufacturer → brand → pack_size → uom
       c. After each filter, if candidate count == 1 AND an identity field
          matched (brand / generic / normalized_name / pack_size) → AUTO
  3. If still not unique / identity not verified → ask the user:
       - remaining candidates → ranked list (PENDING_SELECT)
       - none / unusable → all PO lines (PENDING_MANUAL)
  4. Loop ends when a verified unique match is found OR user confirms
     (CONFIRM updates ERP invoice_line.line_status → CONFIRMED)

itemFingerprint top-level fields always reflect the OCR invoice line;
suggested PO product details live under matchReview.suggested.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy.orm import Session

from models import PurchaseOrderHeader, PurchaseOrderLine, VendorItem
from services.item_fingerprint import build_invoice_line_fingerprint_with_openai
from services.ocr_alias_dictionary import list_product_aliases_for_item_ids
from services.product_matching_common import (
    LINE_STATUS_CONFIRMED,
    LINE_STATUS_NO_MATCH,
    LINE_STATUS_PENDING_CONFIRM,
    LINE_STATUS_PENDING_MANUAL,
    LINE_STATUS_PENDING_SELECT,
    MATCH_COLOR_GREEN,
    MATCH_COLOR_RED,
    MATCH_COLOR_YELLOW,
    MATCH_TYPE_ALIAS,
    MATCH_TYPE_AUTO,
    MATCH_TYPE_MANUAL,
    MATCH_TYPE_MULTIPLE,
    MATCH_TYPE_NONE,
    build_match_review,
    clean_text,
    collect_product_codes,
    dedupe_aliases,
    embedding_acceptable_for_match,
    evaluate_product_code_embedding_gate,
    extract_line_product_code,
    normalize_product_name,
    product_code_in_candidate_codes,
    rank_candidates_by_embedding,
    resolve_match_state_from_count,
    score_alias_match,
)

logger = logging.getLogger(__name__)

# Progressive fingerprint search order (PO/vendor pool first, then these fields).
PROGRESSIVE_FINGERPRINT_FIELDS = (
    "category",
    "manufacturer",
    "brand",
    "pack_size",
    "uom",
)

# AUTO requires at least one identity field match — category/uom alone is too weak
# (e.g. Oreo vs Marie Gold both "BISCUITS" / "PACK" on a single-line PO).
AUTO_IDENTITY_FIELDS = ("brand", "generic", "normalized_name", "pack_size")


@dataclass
class PoProductCandidate:
    """One PO line product that invoice lines may match against."""

    po_line_no: int
    item_id: Optional[int]
    item_no: Optional[str]
    description: Optional[str]
    uom: Optional[str]
    product_fingerprint: Optional[Dict[str, Any]] = None
    aliases: List[str] = field(default_factory=list)


@dataclass
class InvoiceLineAliasMatch:
    line_no: Optional[int]
    product_name: str
    matched: bool = False
    item_id: Optional[int] = None
    po_line_no: Optional[int] = None
    matched_alias: Optional[str] = None
    match_method: Optional[str] = None
    product_fingerprint: Optional[Dict[str, Any]] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProgressiveFingerprintMatch:
    matched: bool = False
    match_type: Optional[str] = None
    item_id: Optional[int] = None
    po_line_no: Optional[int] = None
    product_fingerprint: Optional[Dict[str, Any]] = None
    matched_fields: List[str] = field(default_factory=list)
    skipped_fields: List[str] = field(default_factory=list)
    remaining_after_filters: int = 0
    ranked_candidates: List[Dict[str, Any]] = field(default_factory=list)
    all_po_lines: List[Dict[str, Any]] = field(default_factory=list)
    match_method: str = "progressive_fingerprint"
    details: Dict[str, Any] = field(default_factory=dict)


def _normalize_fp_value(value: Any) -> str:
    return normalize_product_name(value)


def _pack_size_tokens(value: Any) -> Set[str]:
    text = _normalize_fp_value(value)
    if not text:
        return set()
    # Keep digits/units comparable: "500 gm" ↔ "500g"
    compact = text.replace(" ", "")
    tokens = {text, compact}
    return {t for t in tokens if t}


def _fingerprint_values_match(field: str, left: Any, right: Any) -> bool:
    if field == "pack_size":
        left_tokens = _pack_size_tokens(left)
        right_tokens = _pack_size_tokens(right)
        if not left_tokens or not right_tokens:
            return False
        if left_tokens & right_tokens:
            return True
        # Soft contains for values like "10x10" vs "10 X 10"
        ln = _normalize_fp_value(left)
        rn = _normalize_fp_value(right)
        return bool(ln and rn and (ln in rn or rn in ln))

    ln = _normalize_fp_value(left)
    rn = _normalize_fp_value(right)
    if not ln or not rn:
        return False
    return ln == rn or ln in rn or rn in ln


def candidate_fingerprint_view(
    candidate: PoProductCandidate,
) -> Dict[str, Optional[str]]:
    """Comparable fingerprint fields for a PO product candidate."""
    fp = (
        candidate.product_fingerprint
        if isinstance(candidate.product_fingerprint, dict)
        else {}
    )
    view: Dict[str, Optional[str]] = {}
    for key in PROGRESSIVE_FINGERPRINT_FIELDS:
        value = clean_text(fp.get(key)) if fp else None
        if not value and key == "uom":
            value = clean_text(candidate.uom)
        view[key] = value
    return view


def candidate_product_name(candidate: PoProductCandidate) -> str:
    fp = candidate.product_fingerprint if isinstance(candidate.product_fingerprint, dict) else {}
    for key in ("normalized_name", "product_name", "brand", "generic"):
        val = clean_text(fp.get(key))
        if val:
            return val
    return clean_text(candidate.description) or clean_text(candidate.item_no) or ""


def po_candidate_product_codes(candidate: PoProductCandidate) -> List[str]:
    return collect_product_codes(candidate.item_no)


def _po_candidates_matching_product_code(
    product_code: Optional[str],
    candidates: Sequence[PoProductCandidate],
) -> List[PoProductCandidate]:
    if not clean_text(product_code):
        return []
    return [
        candidate
        for candidate in candidates
        if product_code_in_candidate_codes(
            product_code,
            po_candidate_product_codes(candidate),
        )
    ]


def candidate_to_review_option(
    candidate: PoProductCandidate,
    *,
    rank: Optional[int] = None,
    distance: Optional[float] = None,
) -> Dict[str, Any]:
    option: Dict[str, Any] = {
        "poLineNo": candidate.po_line_no,
        "itemId": candidate.item_id,
        "itemNo": candidate.item_no,
        "description": candidate.description,
        "uom": candidate.uom,
        "productName": candidate_product_name(candidate),
        "productFingerprint": candidate.product_fingerprint,
    }
    if rank is not None:
        option["rank"] = rank
    if distance is not None:
        option["distance"] = distance
    return option


def rank_candidates_by_product_name_embedding(
    product_name: str,
    candidates: Sequence[PoProductCandidate],
) -> List[Dict[str, Any]]:
    """Rank PO products by embedding similarity to the invoice product name."""
    acceptable, _ = rank_acceptable_po_candidates_by_embedding(product_name, candidates)
    return acceptable


def rank_acceptable_po_candidates_by_embedding(
    product_name: str,
    candidates: Sequence[PoProductCandidate],
) -> tuple[List[Dict[str, Any]], List[tuple[PoProductCandidate, float]]]:
    """
    Rank PO candidates and keep only semantically acceptable matches.

    Returns (review_options, all_ranked_pairs). When every distance is above
    the shared embedding threshold, review_options is empty; callers fall back
    to all_ranked for yellow user-pick unless invoice category excludes PO pool.
    """
    ranked_pairs = rank_candidates_by_embedding(
        product_name,
        candidates,
        candidate_product_name,
        tie_breaker=lambda candidate: candidate.po_line_no,
        log_context="invoice product",
    )
    acceptable_pairs = [
        (candidate, dist)
        for candidate, dist in ranked_pairs
        if embedding_acceptable_for_match(
            product_name,
            candidate_product_name(candidate),
            dist,
        )
    ]
    review_options = [
        candidate_to_review_option(
            candidate,
            rank=idx + 1,
            distance=round(float(distance), 6),
        )
        for idx, (candidate, distance) in enumerate(acceptable_pairs)
    ]
    return review_options, ranked_pairs


def build_item_fingerprint_with_match_review(
    fingerprint: Optional[Dict[str, Any]],
    *,
    product_name: str,
    match_type: str,
    line_status: str,
    suggested: Optional[Dict[str, Any]] = None,
    ranked_candidates: Optional[List[Dict[str, Any]]] = None,
    all_po_lines: Optional[List[Dict[str, Any]]] = None,
    match_color: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach UI match-review metadata to invoice line itemFingerprint."""
    fp = dict(fingerprint) if isinstance(fingerprint, dict) else {}
    fp["matchReview"] = build_match_review(
        product_name=product_name,
        match_type=match_type,
        line_status=line_status,
        suggested=suggested,
        ranked_candidates=ranked_candidates,
        all_po_lines=all_po_lines,
        match_color=match_color,
        extra=extra,
    )
    return fp


def _has_auto_identity_match(matched_fields: Sequence[str]) -> bool:
    matched = {str(f).strip().lower() for f in matched_fields if f}
    return bool(matched & {f.lower() for f in AUTO_IDENTITY_FIELDS})


def _ranked_options_from_pairs(
    ranked_pairs: Sequence[tuple[PoProductCandidate, float]],
) -> List[Dict[str, Any]]:
    return [
        candidate_to_review_option(
            candidate,
            rank=idx + 1,
            distance=round(float(distance), 6),
        )
        for idx, (candidate, distance) in enumerate(ranked_pairs)
    ]


def _has_domain_conflict_from_stages(
    stages: Optional[Sequence[Dict[str, Any]]],
) -> bool:
    """
    True when invoice category clearly excludes every PO candidate.

    Example: invoice category MEDICAL SUPPLIES vs biscuit-only PO lines.
    Brand-only mismatches (Marie Gold vs Good Day) are not domain conflicts.
    """
    if not stages:
        return False
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        if (
            stage.get("status") == "skipped_empty_result"
            and stage.get("field") == "category"
            and clean_text(stage.get("invoice_value"))
        ):
            return True
    return False


def _apply_weak_embedding_fallback(
    result: ProgressiveFingerprintMatch,
    *,
    all_ranked: Sequence[tuple[PoProductCandidate, float]],
    filtered_pool_size: int,
    domain_conflict: bool,
) -> None:
    """Red on domain mismatch; otherwise yellow pick-list from weak embeddings."""
    best_dist = all_ranked[0][1] if all_ranked else None
    if best_dist is not None:
        result.details["top_rank_distance"] = round(float(best_dist), 6)

    if domain_conflict or not all_ranked:
        result.match_type = MATCH_TYPE_MANUAL
        result.matched = False
        result.ranked_candidates = []
        result.details["reason"] = "no_semantic_match"
        result.details["filtered_pool_size"] = filtered_pool_size
        result.details["stop_reason"] = "no_confident_match"
        return

    result.match_type = MATCH_TYPE_MULTIPLE
    result.matched = False
    result.ranked_candidates = _ranked_options_from_pairs(all_ranked)
    result.details["reason"] = "weak_semantic_match"
    result.details["filtered_pool_size"] = filtered_pool_size
    result.details["stop_reason"] = "awaiting_user_confirm"


def _finalize_fingerprint_pool(
    result: ProgressiveFingerprintMatch,
    *,
    pool: Sequence[PoProductCandidate],
    all_candidates: Sequence[PoProductCandidate],
    invoice_fingerprint: Dict[str, Any],
    product_name: Optional[str],
) -> ProgressiveFingerprintMatch:
    """Map remaining candidate pool to auto / multiple / manual outcomes."""
    result.remaining_after_filters = len(pool)
    result.all_po_lines = [candidate_to_review_option(c) for c in all_candidates]
    query_name = (
        _clean(product_name)
        or _clean(invoice_fingerprint.get("normalized_name"))
        or _clean(invoice_fingerprint.get("brand"))
        or ""
    )
    domain_conflict = _has_domain_conflict_from_stages(result.details.get("stages"))

    # Unique remaining candidate is AUTO only when identity fields actually matched.
    # A single-line PO must not auto-bind Oreo → Marie Gold just because pool size is 1.
    if len(pool) == 1 and _has_auto_identity_match(result.matched_fields):
        winner = pool[0]
        result.match_type = MATCH_TYPE_AUTO
        result.matched = True
        result.item_id = winner.item_id
        result.po_line_no = winner.po_line_no
        result.product_fingerprint = (
            winner.product_fingerprint
            if isinstance(winner.product_fingerprint, dict)
            else invoice_fingerprint
        )
        result.details["winner_item_no"] = winner.item_no
        result.details["winner_description"] = winner.description
        result.details["stop_reason"] = "candidate_count_1"
        return result

    if len(pool) == 1:
        winner = pool[0]
        acceptable, all_ranked = rank_acceptable_po_candidates_by_embedding(
            query_name,
            pool,
        )
        if not acceptable:
            _apply_weak_embedding_fallback(
                result,
                all_ranked=all_ranked,
                filtered_pool_size=1,
                domain_conflict=domain_conflict,
            )
            return result
        result.match_type = MATCH_TYPE_MULTIPLE
        result.matched = False
        result.ranked_candidates = acceptable
        result.details["reason"] = "unique_candidate_without_identity_match"
        result.details["filtered_pool_size"] = 1
        result.details["stop_reason"] = "awaiting_user_confirm"
        result.details["winner_item_no"] = winner.item_no
        result.details["winner_description"] = winner.description
        return result

    if len(pool) > 1 and result.matched_fields:
        acceptable, all_ranked = rank_acceptable_po_candidates_by_embedding(
            query_name,
            pool,
        )
        if not acceptable:
            _apply_weak_embedding_fallback(
                result,
                all_ranked=all_ranked,
                filtered_pool_size=len(pool),
                domain_conflict=domain_conflict,
            )
            return result
        result.match_type = MATCH_TYPE_MULTIPLE
        result.matched = False
        result.ranked_candidates = acceptable
        result.details["reason"] = "multiple_candidates"
        result.details["filtered_pool_size"] = len(pool)
        result.details["stop_reason"] = "awaiting_user_confirm"
        if acceptable:
            result.details["top_rank_distance"] = acceptable[0].get("distance")
        return result

    result.match_type = MATCH_TYPE_MANUAL
    result.matched = False
    result.details["reason"] = (
        "no_candidates_after_filters"
        if not pool
        else "no_comparable_fingerprint_fields"
    )
    result.details["stop_reason"] = "awaiting_user_confirm"
    return result


def progressive_match_fingerprint_to_po_products(
    invoice_fingerprint: Dict[str, Any],
    candidates: Sequence[PoProductCandidate],
    *,
    product_name: Optional[str] = None,
) -> ProgressiveFingerprintMatch:
    """
    Fingerprint matching engine loop (runs only after product-name search fails).

    For each field in order (category → manufacturer → brand → pack_size → uom):
      - filter the current PO candidate pool
      - if a filter would empty the pool, skip it (soft progressive)
      - STOP immediately when candidate count becomes 1

    Otherwise continue until the sequence ends, then:
      - multiple remaining → semantic ranking for user select
      - none / unusable → all PO lines for manual select
    """
    result = ProgressiveFingerprintMatch()
    if not isinstance(invoice_fingerprint, dict) or not invoice_fingerprint:
        result.details["reason"] = "empty_invoice_fingerprint"
        result.match_type = MATCH_TYPE_MANUAL
        result.all_po_lines = [candidate_to_review_option(c) for c in candidates]
        logger.info(
            "[INVOICE MATCH] step=fingerprint_filter result=manual "
            "reason=empty_invoice_fingerprint"
        )
        return result
    if not candidates:
        result.details["reason"] = "no_po_products"
        result.match_type = MATCH_TYPE_MANUAL
        logger.info(
            "[INVOICE MATCH] step=fingerprint_filter result=manual reason=no_po_products"
        )
        return result

    pool: List[PoProductCandidate] = list(candidates)
    result.details["initial_pool"] = len(pool)
    stages: List[Dict[str, Any]] = []
    logger.info(
        "[INVOICE MATCH] step=fingerprint_filter_start initial_pool=%s product_name=%r",
        len(pool),
        product_name,
    )

    # Matching-engine loop: always verify fields against the pool.
    # Do not short-circuit on initial pool size 1 — that caused false AUTO matches.
    for field in PROGRESSIVE_FINGERPRINT_FIELDS:
        invoice_value = _clean(invoice_fingerprint.get(field))
        if not invoice_value:
            result.skipped_fields.append(field)
            stages.append(
                {
                    "field": field,
                    "status": "skipped_missing_invoice_value",
                    "remaining": len(pool),
                }
            )
            logger.info(
                "[INVOICE MATCH] step=fingerprint_filter field=%s "
                "status=skipped_missing_invoice_value remaining=%s",
                field,
                len(pool),
            )
            continue

        narrowed = [
            c
            for c in pool
            if _fingerprint_values_match(
                field,
                invoice_value,
                candidate_fingerprint_view(c).get(field),
            )
        ]
        if not narrowed:
            # Soft progressive: keep previous pool when filter eliminates all.
            result.skipped_fields.append(field)
            stages.append(
                {
                    "field": field,
                    "status": "skipped_empty_result",
                    "invoice_value": invoice_value,
                    "remaining": len(pool),
                }
            )
            logger.info(
                "[INVOICE MATCH] step=fingerprint_filter field=%s "
                "status=skipped_empty_result invoice_value=%r remaining=%s",
                field,
                invoice_value,
                len(pool),
            )
            continue

        pool = narrowed
        result.matched_fields.append(field)
        stages.append(
            {
                "field": field,
                "status": "applied",
                "invoice_value": invoice_value,
                "remaining": len(pool),
            }
        )
        logger.info(
            "[INVOICE MATCH] step=fingerprint_filter field=%s status=applied "
            "invoice_value=%r remaining=%s",
            field,
            invoice_value,
            len(pool),
        )

        # Stop early only after a real filter narrowed to one candidate with identity.
        if len(pool) == 1 and _has_auto_identity_match(result.matched_fields):
            stages.append(
                {
                    "field": field,
                    "status": "stopped_unique_candidate",
                    "remaining": 1,
                }
            )
            logger.info(
                "[INVOICE MATCH] step=fingerprint_filter field=%s "
                "status=stopped_unique remaining=1 matched_fields=%s",
                field,
                result.matched_fields,
            )
            break

    result.details["stages"] = stages
    finalized = _finalize_fingerprint_pool(
        result,
        pool=pool,
        all_candidates=candidates,
        invoice_fingerprint=invoice_fingerprint,
        product_name=product_name,
    )
    logger.info(
        "[INVOICE MATCH] step=fingerprint_filter_finalize match_type=%s "
        "remaining=%s stop_reason=%s",
        finalized.match_type,
        finalized.remaining_after_filters,
        finalized.details.get("stop_reason") or finalized.details.get("reason"),
    )
    return finalized


@dataclass
class MatchingEngineLineResult:
    """Result of running the matching engine for one invoice line."""

    product_name: str
    match_type: str
    line_status: str
    matched: bool = False
    item_id: Optional[int] = None
    po_line_no: Optional[int] = None
    fingerprint: Optional[Dict[str, Any]] = None
    item_fingerprint: Optional[Dict[str, Any]] = None
    engine_path: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


class InvoiceMatchingEngine:
    """
    Invoice line matching engine.

    Loop:
      product_name search
        └─ fail → OpenAI fingerprint → fingerprint field sequence
              └─ stop when candidates == 1 OR hand off to user confirm
    """

    FINGERPRINT_SEQUENCE = PROGRESSIVE_FINGERPRINT_FIELDS

    def __init__(
        self,
        *,
        candidates: Sequence[PoProductCandidate],
        vendor_id: Optional[int] = None,
    ):
        self.candidates = list(candidates)
        self.vendor_id = vendor_id

    def _build_pending_select_embedding_result(
        self,
        *,
        product_name: str,
        line: Dict[str, Any],
        engine_path: List[str],
        fingerprint: Optional[Dict[str, Any]],
        line_no: Any,
        candidates: Sequence[PoProductCandidate],
        stop_reason: str,
        product_code: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> MatchingEngineLineResult:
        ranked_pairs = rank_candidates_by_embedding(
            product_name,
            list(candidates),
            candidate_product_name,
            tie_breaker=lambda candidate: candidate.po_line_no,
            log_context="invoice product",
        )
        ranked = [
            candidate_to_review_option(candidate, rank=idx + 1, distance=dist)
            for idx, (candidate, dist) in enumerate(ranked_pairs)
        ]
        all_po_lines = [candidate_to_review_option(c) for c in self.candidates]
        review_extra: Dict[str, Any] = {
            "enginePath": engine_path,
            "stopReason": stop_reason,
        }
        if product_code:
            review_extra["productCode"] = product_code
        if extra:
            review_extra.update(extra)
        item_fp = build_item_fingerprint_with_match_review(
            fingerprint if isinstance(fingerprint, dict) else {},
            product_name=product_name,
            match_type=MATCH_TYPE_MULTIPLE,
            line_status=LINE_STATUS_PENDING_SELECT,
            match_color=MATCH_COLOR_YELLOW,
            ranked_candidates=ranked,
            all_po_lines=all_po_lines,
            extra=review_extra,
        )
        logger.info(
            "[INVOICE MATCH] step=line_engine_done line_no=%s match_type=%s "
            "line_status=%s stop_reason=%s ranked=%s product_code=%r",
            line_no,
            MATCH_TYPE_MULTIPLE,
            LINE_STATUS_PENDING_SELECT,
            stop_reason,
            len(ranked),
            product_code,
        )
        return MatchingEngineLineResult(
            product_name=product_name,
            match_type=MATCH_TYPE_MULTIPLE,
            line_status=LINE_STATUS_PENDING_SELECT,
            matched=False,
            fingerprint=fingerprint if isinstance(fingerprint, dict) else None,
            item_fingerprint=item_fp,
            engine_path=engine_path,
            details={
                "stopReason": stop_reason,
                "rankedCandidates": ranked,
                "productCode": product_code,
            },
        )

    def _apply_product_code_embedding_gate(
        self,
        *,
        product_name: str,
        line: Dict[str, Any],
        candidate: PoProductCandidate,
        engine_path: List[str],
        fingerprint: Optional[Dict[str, Any]],
        line_no: Any,
    ) -> Optional[MatchingEngineLineResult]:
        product_code = extract_line_product_code(line)
        if not product_code:
            return None

        code_hits = _po_candidates_matching_product_code(
            product_code,
            self.candidates,
        )
        if not code_hits:
            return None

        candidate_codes = po_candidate_product_codes(candidate)
        catalog_name = candidate_product_name(candidate)
        code_present, ok, distance, stop_reason = evaluate_product_code_embedding_gate(
            product_name=product_name,
            product_code=product_code,
            candidate_name=catalog_name,
            candidate_codes=candidate_codes,
            log_context="invoice product",
        )
        if not code_present:
            return None

        if product_code_in_candidate_codes(product_code, candidate_codes):
            if ok:
                return None
            engine_path.append("product_code_embedding_mismatch")
            return self._build_pending_select_embedding_result(
                product_name=product_name,
                line=line,
                engine_path=engine_path,
                fingerprint=fingerprint,
                line_no=line_no,
                candidates=self.candidates,
                stop_reason=stop_reason or "product_code_embedding_mismatch",
                product_code=product_code,
                extra={
                    "embeddingDistance": round(float(distance), 6)
                    if distance is not None
                    else None,
                    "matchedProductCode": product_code,
                },
            )

        engine_path.append("product_code_name_conflict")
        return self._build_pending_select_embedding_result(
            product_name=product_name,
            line=line,
            engine_path=engine_path,
            fingerprint=fingerprint,
            line_no=line_no,
            candidates=code_hits,
            stop_reason="product_code_name_conflict",
            product_code=product_code,
            extra={"matchedProductCode": product_code},
        )

    def _try_product_code_first_match(
        self,
        *,
        product_name: str,
        line: Dict[str, Any],
        engine_path: List[str],
        line_no: Any,
        db: Optional[Session],
    ) -> Optional[MatchingEngineLineResult]:
        product_code = extract_line_product_code(line)
        if not product_code:
            return None

        engine_path.append("product_code_search")
        code_hits = _po_candidates_matching_product_code(
            product_code,
            self.candidates,
        )
        if not code_hits:
            logger.info(
                "[INVOICE MATCH] step=product_code_search line_no=%s result=no_po_code_match code=%r",
                line_no,
                product_code,
            )
            return None

        if len(code_hits) > 1:
            engine_path.append(f"product_code_multiple:{len(code_hits)}")
            return self._build_pending_select_embedding_result(
                product_name=product_name,
                line=line,
                engine_path=engine_path,
                fingerprint=None,
                line_no=line_no,
                candidates=code_hits,
                stop_reason="multiple_product_code_matches",
                product_code=product_code,
            )

        candidate = code_hits[0]
        catalog_name = candidate_product_name(candidate)
        _, ok, distance, stop_reason = evaluate_product_code_embedding_gate(
            product_name=product_name,
            product_code=product_code,
            candidate_name=catalog_name,
            candidate_codes=po_candidate_product_codes(candidate),
            log_context="invoice product",
        )
        if not ok:
            engine_path.append("product_code_embedding_mismatch")
            return self._build_pending_select_embedding_result(
                product_name=product_name,
                line=line,
                engine_path=engine_path,
                fingerprint=candidate.product_fingerprint,
                line_no=line_no,
                candidates=self.candidates,
                stop_reason=stop_reason or "product_code_embedding_mismatch",
                product_code=product_code,
                extra={
                    "embeddingDistance": round(float(distance), 6)
                    if distance is not None
                    else None,
                },
            )

        fingerprint = candidate.product_fingerprint
        if (not fingerprint) and db is not None:
            fingerprint = _fingerprint_from_matched_candidate(
                db,
                InvoiceLineAliasMatch(
                    line_no=line_no,
                    product_name=product_name,
                    matched=True,
                    item_id=candidate.item_id,
                    po_line_no=candidate.po_line_no,
                    product_fingerprint=fingerprint,
                ),
                vendor_id=self.vendor_id,
            )
        suggested = candidate_to_review_option(candidate)
        item_fp = build_item_fingerprint_with_match_review(
            fingerprint if isinstance(fingerprint, dict) else {},
            product_name=product_name,
            match_type=MATCH_TYPE_ALIAS,
            line_status=LINE_STATUS_PENDING_CONFIRM,
            match_color=MATCH_COLOR_GREEN,
            suggested=suggested,
            extra={
                "matchedProductCode": product_code,
                "matchMethod": "product_code",
                "enginePath": engine_path,
                "stopReason": "product_code_and_embedding_match",
                "embeddingDistance": round(float(distance), 6)
                if distance is not None
                else None,
            },
        )
        logger.info(
            "[INVOICE MATCH] step=product_code_search line_no=%s result=matched "
            "code=%r item_id=%s po_line_no=%s distance=%s",
            line_no,
            product_code,
            candidate.item_id,
            candidate.po_line_no,
            distance,
        )
        return MatchingEngineLineResult(
            product_name=product_name,
            match_type=MATCH_TYPE_ALIAS,
            line_status=LINE_STATUS_PENDING_CONFIRM,
            matched=True,
            item_id=candidate.item_id,
            po_line_no=candidate.po_line_no,
            fingerprint=fingerprint if isinstance(fingerprint, dict) else None,
            item_fingerprint=item_fp,
            engine_path=engine_path,
            details={
                "matchedProductCode": product_code,
                "stopReason": "product_code_and_embedding_match",
                "embeddingDistance": distance,
            },
        )

    def run_for_line(
        self,
        *,
        product_name: str,
        line: Dict[str, Any],
        alias_hit: Optional[InvoiceLineAliasMatch] = None,
        db: Optional[Session] = None,
    ) -> MatchingEngineLineResult:
        """
        Run the full matching engine for one invoice line.

        Returns a result that already carries lineStatus / matchReview payload.
        User confirmation (CONFIRMED) is applied later via confirm API.
        """
        name = _clean(product_name) or ""
        engine_path: List[str] = ["product_name_search"]
        line_no = line.get("lineNo") or line.get("line_no")

        logger.info(
            "[INVOICE MATCH] step=line_engine_start line_no=%s product_name=%r "
            "po_candidates=%s",
            line_no,
            name,
            len(self.candidates),
        )

        code_first = self._try_product_code_first_match(
            product_name=name,
            line=line,
            engine_path=engine_path,
            line_no=line_no,
            db=db,
        )
        if code_first is not None:
            return code_first

        # --- Stage 1: product name / alias search ---
        hit = alias_hit
        if hit is None:
            hit = match_product_name_to_po_aliases(name, self.candidates)

        if hit.matched:
            logger.info(
                "[INVOICE MATCH] step=product_name_search line_no=%s result=matched "
                "method=%s alias=%r item_id=%s po_line_no=%s",
                line_no,
                hit.match_method,
                hit.matched_alias,
                hit.item_id,
                hit.po_line_no,
            )
            fingerprint = hit.product_fingerprint
            if (not fingerprint) and db is not None:
                fingerprint = _fingerprint_from_matched_candidate(
                    db, hit, vendor_id=self.vendor_id
                )
            matched_candidate = next(
                (
                    c
                    for c in self.candidates
                    if c.po_line_no == hit.po_line_no
                ),
                PoProductCandidate(
                    po_line_no=int(hit.po_line_no or 0),
                    item_id=hit.item_id,
                    item_no=(hit.details or {}).get("po_item_no"),
                    description=(hit.details or {}).get("po_description"),
                    uom=_clean(line.get("uom")),
                    product_fingerprint=fingerprint,
                ),
            )
            gate_result = self._apply_product_code_embedding_gate(
                product_name=name,
                line=line,
                candidate=matched_candidate,
                engine_path=engine_path,
                fingerprint=fingerprint if isinstance(fingerprint, dict) else None,
                line_no=line_no,
            )
            if gate_result is not None:
                return gate_result
            suggested = candidate_to_review_option(matched_candidate)
            item_fp = build_item_fingerprint_with_match_review(
                fingerprint,
                product_name=name,
                match_type=MATCH_TYPE_ALIAS,
                line_status=LINE_STATUS_PENDING_CONFIRM,
                match_color=MATCH_COLOR_GREEN,
                suggested=suggested,
                extra={
                    "matchedAlias": hit.matched_alias,
                    "matchMethod": hit.match_method,
                    "enginePath": engine_path,
                    "stopReason": "product_name_match",
                },
            )
            logger.info(
                "[INVOICE MATCH] step=line_engine_done line_no=%s match_type=%s "
                "line_status=%s stop_reason=product_name_match",
                line_no,
                MATCH_TYPE_ALIAS,
                LINE_STATUS_PENDING_CONFIRM,
            )
            return MatchingEngineLineResult(
                product_name=name,
                match_type=MATCH_TYPE_ALIAS,
                line_status=LINE_STATUS_PENDING_CONFIRM,
                matched=True,
                item_id=hit.item_id,
                po_line_no=hit.po_line_no,
                fingerprint=fingerprint,
                item_fingerprint=item_fp,
                engine_path=engine_path,
                details={
                    "matchedAlias": hit.matched_alias,
                    "stopReason": "product_name_match",
                },
            )

        logger.info(
            "[INVOICE MATCH] step=product_name_search line_no=%s result=no_match "
            "→ fingerprint_engine",
            line_no,
        )

        # --- Stage 2: product-name failed → fingerprint engine loop ---
        if not name:
            engine_path.append("fingerprint_sequence")
            logger.warning(
                "[INVOICE MATCH] step=line_engine_done line_no=%s match_type=%s "
                "reason=empty_product_name",
                line_no,
                MATCH_TYPE_MANUAL,
            )
            return MatchingEngineLineResult(
                product_name=name,
                match_type=MATCH_TYPE_MANUAL,
                line_status=LINE_STATUS_NO_MATCH,
                matched=False,
                engine_path=engine_path,
                details={"reason": "empty_product_name"},
            )

        logger.info(
            "[INVOICE MATCH] step=openai_fingerprint line_no=%s product_name=%r",
            line_no,
            name,
        )
        fingerprint = _apply_openai_fingerprint_for_unmatched_line(
            line,
            product_name=name,
        )
        engine_path.append("openai_fingerprint")
        engine_path.append("fingerprint_sequence")
        logger.info(
            "[INVOICE MATCH] step=openai_fingerprint line_no=%s result=ok keys=%s",
            line_no,
            sorted([k for k, v in fingerprint.items() if v]) if fingerprint else [],
        )

        logger.info(
            "[INVOICE MATCH] step=fingerprint_sequence_start line_no=%s fields=%s",
            line_no,
            list(PROGRESSIVE_FINGERPRINT_FIELDS),
        )
        progressive = progressive_match_fingerprint_to_po_products(
            fingerprint,
            self.candidates,
            product_name=name,
        )
        engine_path.extend(
            [f"filter:{f}" for f in progressive.matched_fields]
        )
        logger.info(
            "[INVOICE MATCH] step=fingerprint_sequence_done line_no=%s "
            "match_type=%s matched_fields=%s skipped_fields=%s remaining=%s stages=%s",
            line_no,
            progressive.match_type,
            progressive.matched_fields,
            progressive.skipped_fields,
            progressive.remaining_after_filters,
            progressive.details.get("stages"),
        )

        if progressive.match_type == MATCH_TYPE_AUTO and progressive.matched:
            winner = next(
                (c for c in self.candidates if c.po_line_no == progressive.po_line_no),
                None,
            )
            if winner is not None:
                gate_result = self._apply_product_code_embedding_gate(
                    product_name=name,
                    line=line,
                    candidate=winner,
                    engine_path=engine_path,
                    fingerprint=fingerprint,
                    line_no=line_no,
                )
                if gate_result is not None:
                    return gate_result
            winner_fp = progressive.product_fingerprint or fingerprint
            suggested = None
            for opt in progressive.all_po_lines:
                if opt.get("poLineNo") == progressive.po_line_no:
                    suggested = opt
                    break
            if suggested is None:
                suggested = {
                    "poLineNo": progressive.po_line_no,
                    "itemId": progressive.item_id,
                    "productFingerprint": winner_fp,
                }
            # Keep OCR/invoice fingerprint at top level so UI pack/brand reflect
            # the uploaded line, not the suggested PO product.
            item_fp = build_item_fingerprint_with_match_review(
                fingerprint,
                product_name=name,
                match_type=MATCH_TYPE_AUTO,
                line_status=LINE_STATUS_PENDING_CONFIRM,
                match_color=MATCH_COLOR_GREEN,
                suggested=suggested,
                extra={
                    "progressiveMatchedFields": progressive.matched_fields,
                    "progressiveSkippedFields": progressive.skipped_fields,
                    "enginePath": engine_path,
                    "stages": progressive.details.get("stages"),
                    "stopReason": "candidate_count_1",
                },
            )
            logger.info(
                "[INVOICE MATCH] step=line_engine_done line_no=%s match_type=%s "
                "line_status=%s item_id=%s po_line_no=%s stop_reason=candidate_count_1",
                line_no,
                MATCH_TYPE_AUTO,
                LINE_STATUS_PENDING_CONFIRM,
                progressive.item_id,
                progressive.po_line_no,
            )
            return MatchingEngineLineResult(
                product_name=name,
                match_type=MATCH_TYPE_AUTO,
                line_status=LINE_STATUS_PENDING_CONFIRM,
                matched=True,
                item_id=progressive.item_id,
                po_line_no=progressive.po_line_no,
                fingerprint=fingerprint,
                item_fingerprint=item_fp,
                engine_path=engine_path,
                details={
                    "stopReason": "candidate_count_1",
                    "matchedFields": progressive.matched_fields,
                },
            )

        if progressive.match_type == MATCH_TYPE_MULTIPLE:
            ranked = progressive.ranked_candidates or []
            suggested = ranked[0] if ranked else None
            item_fp = build_item_fingerprint_with_match_review(
                fingerprint,
                product_name=name,
                match_type=MATCH_TYPE_MULTIPLE,
                line_status=LINE_STATUS_PENDING_SELECT,
                match_color=MATCH_COLOR_YELLOW,
                suggested=suggested,
                ranked_candidates=ranked,
                all_po_lines=progressive.all_po_lines or None,
                extra={
                    "progressiveMatchedFields": progressive.matched_fields,
                    "progressiveSkippedFields": progressive.skipped_fields,
                    "enginePath": engine_path,
                    "stages": progressive.details.get("stages"),
                    "stopReason": "awaiting_user_confirm",
                    "reason": progressive.details.get("reason"),
                },
            )
            logger.info(
                "[INVOICE MATCH] step=line_engine_done line_no=%s match_type=%s "
                "line_status=%s ranked=%s stop_reason=awaiting_user_confirm",
                line_no,
                MATCH_TYPE_MULTIPLE,
                LINE_STATUS_PENDING_SELECT,
                len(ranked),
            )
            return MatchingEngineLineResult(
                product_name=name,
                match_type=MATCH_TYPE_MULTIPLE,
                line_status=LINE_STATUS_PENDING_SELECT,
                matched=False,
                fingerprint=fingerprint,
                item_fingerprint=item_fp,
                engine_path=engine_path,
                details={
                    "stopReason": "awaiting_user_confirm",
                    "rankedCandidates": progressive.ranked_candidates,
                },
            )

        all_po_lines = progressive.all_po_lines or [
            candidate_to_review_option(c) for c in self.candidates
        ]
        if not progressive.ranked_candidates and self.candidates:
            rank_candidates_by_product_name_embedding(name, self.candidates)
        no_match = resolve_match_state_from_count(
            len(progressive.ranked_candidates or []),
            no_match_type=MATCH_TYPE_NONE,
        )
        item_fp = build_item_fingerprint_with_match_review(
            fingerprint,
            product_name=name,
            match_type=no_match.match_type,
            line_status=no_match.line_status,
            match_color=no_match.match_color,
            all_po_lines=all_po_lines,
            extra={
                "progressiveMatchedFields": progressive.matched_fields,
                "progressiveSkippedFields": progressive.skipped_fields,
                "enginePath": engine_path,
                "stages": progressive.details.get("stages"),
                "reason": progressive.details.get("reason"),
                "stopReason": "no_confident_match",
            },
        )
        logger.info(
            "[INVOICE MATCH] step=line_engine_done line_no=%s match_type=%s "
            "line_status=%s reason=%s stop_reason=no_confident_match",
            line_no,
            no_match.match_type,
            no_match.line_status,
            progressive.details.get("reason"),
        )
        return MatchingEngineLineResult(
            product_name=name,
            match_type=no_match.match_type,
            line_status=no_match.line_status,
            matched=False,
            fingerprint=fingerprint,
            item_fingerprint=item_fp,
            engine_path=engine_path,
            details={
                "stopReason": "no_confident_match",
                "reason": progressive.details.get("reason"),
                "matchColor": no_match.match_color,
            },
        )

    def apply_to_line(
        self,
        line: Dict[str, Any],
        result: MatchingEngineLineResult,
    ) -> None:
        """Write matching-engine result onto an ERP-shaped invoice line payload."""
        if result.item_id is not None and line.get("itemId") in (None, ""):
            line["itemId"] = result.item_id
        if result.po_line_no is not None:
            line["poLineNo"] = result.po_line_no
        if result.item_fingerprint is not None:
            line["itemFingerprint"] = result.item_fingerprint
        line["lineStatus"] = result.line_status
        line["_aliasMatch"] = {
            "matched": result.matched,
            "matchType": result.match_type,
            "lineStatus": result.line_status,
            "itemId": result.item_id,
            "poLineNo": result.po_line_no,
            "enginePath": result.engine_path,
            "details": result.details,
        }


@dataclass
class InvoiceAliasMatchResult:
    po_number: Optional[str] = None
    po_id: Optional[int] = None
    po_product_count: int = 0
    matched_line_count: int = 0
    total_invoice_lines: int = 0
    line_matches: List[InvoiceLineAliasMatch] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


_clean = clean_text
_normalize_name = normalize_product_name


def extract_invoice_line_product_name(line: Dict[str, Any]) -> Optional[str]:
    """Pull the product name from an ERP-shaped or OCR-shaped invoice line."""
    for key in (
        "description",
        "product_name",
        "productName",
        "item_name",
        "itemName",
        "name",
    ):
        value = _clean(line.get(key))
        if value:
            return value
    return None


def load_po_lines_for_invoice(
    db: Session,
    *,
    tenant_id: int,
    company_id: int,
    po_number: Optional[str] = None,
    po_id: Optional[int] = None,
    document_type: Optional[str] = None,
) -> List[PurchaseOrderLine]:
    """Load PO line products for the invoice's related purchase order."""
    q = db.query(PurchaseOrderLine).filter(
        PurchaseOrderLine.tenant_id == tenant_id,
        PurchaseOrderLine.company_id == company_id,
    )
    if po_id is not None:
        q = q.filter(PurchaseOrderLine.po_id == int(po_id))
    elif _clean(po_number):
        q = q.filter(PurchaseOrderLine.po_number == str(po_number).strip())
    else:
        return []

    if _clean(document_type):
        q = q.filter(PurchaseOrderLine.document_type == str(document_type).strip())

    lines = q.order_by(PurchaseOrderLine.line_no.asc()).all()
    if lines:
        return lines

    # Fallback: resolve header then reload lines (handles document_type mismatch).
    header_q = db.query(PurchaseOrderHeader).filter(
        PurchaseOrderHeader.tenant_id == tenant_id,
        PurchaseOrderHeader.company_id == company_id,
    )
    if po_id is not None:
        header_q = header_q.filter(PurchaseOrderHeader.po_id == int(po_id))
    elif _clean(po_number):
        header_q = header_q.filter(
            PurchaseOrderHeader.po_number == str(po_number).strip()
        )
    else:
        return []

    header = (
        header_q.order_by(PurchaseOrderHeader.version_no.desc()).first()
    )
    if header is None:
        return []

    return (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.po_id == header.po_id)
        .order_by(PurchaseOrderLine.line_no.asc())
        .all()
    )


def _fingerprint_alias_values(fingerprint: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(fingerprint, dict):
        return []
    values: List[str] = []
    for key in (
        "normalized_name",
        "brand",
        "generic",
        "product",
        "product_name",
        "item_name",
    ):
        val = _clean(fingerprint.get(key))
        if val:
            values.append(val)
    keywords = fingerprint.get("keywords")
    if isinstance(keywords, list):
        for kw in keywords:
            val = _clean(kw)
            if val:
                values.append(val)
    return values


def _vendor_item_alias_values(item: VendorItem) -> List[str]:
    values: List[str] = []
    for attr in ("product_name", "item_name", "brand", "description"):
        val = _clean(getattr(item, attr, None))
        if val:
            values.append(val)
    values.extend(_fingerprint_alias_values(
        item.product_fingerprint if isinstance(item.product_fingerprint, dict) else None
    ))
    return values


def build_po_product_candidates(
    db: Session,
    po_lines: Sequence[PurchaseOrderLine],
    *,
    vendor_id: Optional[int] = None,
) -> List[PoProductCandidate]:
    """
    Build the PO-scoped candidate product set with matchable aliases.

    Alias sources (all restricted to PO products):
      - ocr_alias_dictionary rows for those item_ids
      - PO line description / item_no / product_fingerprint
      - vendor_items names when resolvable
    """
    candidates: List[PoProductCandidate] = []
    item_ids: List[int] = []

    for line in po_lines:
        item_id = int(line.item_id) if line.item_id is not None else None
        if item_id is not None:
            item_ids.append(item_id)
        aliases: List[str] = []
        for value in (
            _clean(line.description),
            _clean(line.item_no),
            *_fingerprint_alias_values(
                line.product_fingerprint
                if isinstance(line.product_fingerprint, dict)
                else None
            ),
        ):
            if value:
                aliases.append(value)
        candidates.append(
            PoProductCandidate(
                po_line_no=int(line.line_no),
                item_id=item_id,
                item_no=_clean(line.item_no),
                description=_clean(line.description),
                uom=_clean(line.uom),
                product_fingerprint=(
                    line.product_fingerprint
                    if isinstance(line.product_fingerprint, dict)
                    else None
                ),
                aliases=aliases,
            )
        )

    # Dictionary aliases for PO item_ids only.
    dict_rows = list_product_aliases_for_item_ids(item_ids)
    aliases_by_item: Dict[int, List[str]] = {}
    for row in dict_rows:
        raw_item_id = row.get("itemId", row.get("item_id"))
        try:
            item_id = int(raw_item_id)
        except (TypeError, ValueError):
            continue
        bucket = aliases_by_item.setdefault(item_id, [])
        for key in ("aliasValue", "alias_value", "canonicalValue", "canonical_value"):
            val = _clean(row.get(key))
            if val:
                bucket.append(val)

    # Optional vendor catalog names/fingerprints for the same item_ids.
    vendor_aliases_by_item: Dict[int, List[str]] = {}
    vendor_fingerprint_by_item: Dict[int, Dict[str, Any]] = {}
    unique_item_ids = sorted({i for i in item_ids if i is not None})
    if unique_item_ids and vendor_id is not None:
        vendor_items = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == int(vendor_id),
                VendorItem.item_id.in_(unique_item_ids),
            )
            .all()
        )
        for vi in vendor_items:
            if vi.item_id is None:
                continue
            iid = int(vi.item_id)
            vendor_aliases_by_item.setdefault(iid, []).extend(
                _vendor_item_alias_values(vi)
            )
            if (
                iid not in vendor_fingerprint_by_item
                and isinstance(vi.product_fingerprint, dict)
                and vi.product_fingerprint
            ):
                vendor_fingerprint_by_item[iid] = dict(vi.product_fingerprint)
            # Fill missing progressive fields from vendor_items columns.
            base_fp = vendor_fingerprint_by_item.setdefault(iid, {})
            column_fallbacks = {
                "category": getattr(vi, "category", None),
                "manufacturer": getattr(vi, "manufacturer", None),
                "brand": getattr(vi, "brand", None),
                "uom": getattr(vi, "uom", None),
                "pack_size": getattr(vi, "pack_size", None),
            }
            for key, value in column_fallbacks.items():
                if not _clean(base_fp.get(key)) and _clean(value) is not None:
                    base_fp[key] = str(value).strip()

    for candidate in candidates:
        if candidate.item_id is None:
            continue
        candidate.aliases.extend(aliases_by_item.get(candidate.item_id, []))
        candidate.aliases.extend(vendor_aliases_by_item.get(candidate.item_id, []))
        if not candidate.product_fingerprint:
            candidate.product_fingerprint = vendor_fingerprint_by_item.get(
                candidate.item_id
            )
        elif candidate.item_id in vendor_fingerprint_by_item:
            # Merge vendor progressive fields into PO fingerprint when missing.
            merged = dict(candidate.product_fingerprint)
            for key, value in vendor_fingerprint_by_item[candidate.item_id].items():
                if key in PROGRESSIVE_FINGERPRINT_FIELDS and not _clean(merged.get(key)):
                    merged[key] = value
            candidate.product_fingerprint = merged
        # Deduplicate while preserving order.
        candidate.aliases = dedupe_aliases(candidate.aliases)

    return candidates


def _score_alias_match(product_name: str, alias: str) -> Optional[tuple]:
    return score_alias_match(product_name, alias, allow_contains=True)


def match_product_name_to_po_aliases(
    product_name: str,
    candidates: Sequence[PoProductCandidate],
) -> InvoiceLineAliasMatch:
    """Match one invoice product name against PO-scoped aliases only."""
    result = InvoiceLineAliasMatch(line_no=None, product_name=product_name, matched=False)
    normalized = _normalize_name(product_name)
    if not normalized:
        result.details["reason"] = "empty_product_name"
        return result
    if not candidates:
        result.details["reason"] = "no_po_products"
        return result

    best: Optional[tuple] = None
    # best tuple: (priority, -alias_len, candidate, alias, method)
    for candidate in candidates:
        for alias in candidate.aliases:
            scored = _score_alias_match(product_name, alias)
            if scored is None:
                continue
            priority, method = scored
            rank = (priority, -len(_normalize_name(alias)), candidate.po_line_no)
            if best is None or rank < best[0]:
                best = (rank, candidate, alias, method)

    if best is None:
        result.details["reason"] = "no_alias_match"
        result.details["po_candidate_count"] = len(candidates)
        return result

    _, candidate, alias, method = best
    result.matched = True
    result.item_id = candidate.item_id
    result.po_line_no = candidate.po_line_no
    result.matched_alias = alias
    result.match_method = method
    result.product_fingerprint = (
        candidate.product_fingerprint
        if isinstance(candidate.product_fingerprint, dict)
        else None
    )
    result.details = {
        "po_item_no": candidate.item_no,
        "po_description": candidate.description,
        "alias_count": len(candidate.aliases),
    }
    return result


def match_invoice_lines_to_po_aliases(
    db: Session,
    payload: Dict[str, Any],
) -> InvoiceAliasMatchResult:
    """
    Step-1 matching engine:
      extract invoice product names → filter to PO products → match aliases.
    """
    result = InvoiceAliasMatchResult(
        po_number=_clean(payload.get("poNumber") or payload.get("po_number")),
        po_id=payload.get("poId") if payload.get("poId") is not None else payload.get("po_id"),
    )
    try:
        if result.po_id is not None:
            result.po_id = int(result.po_id)
    except (TypeError, ValueError):
        result.po_id = None

    tenant_id = payload.get("tenantId") or payload.get("tenant_id")
    company_id = payload.get("companyId") or payload.get("company_id")
    vendor_id = payload.get("vendorId") or payload.get("vendor_id")
    document_type = _clean(
        payload.get("poDocumentType") or payload.get("po_document_type")
    )
    try:
        tenant_id_int = int(tenant_id)
        company_id_int = int(company_id)
    except (TypeError, ValueError):
        result.details["reason"] = "missing_tenant_or_company"
        return result

    lines = payload.get("lines")
    if not isinstance(lines, list):
        result.details["reason"] = "no_invoice_lines"
        return result

    invoice_lines = [ln for ln in lines if isinstance(ln, dict)]
    result.total_invoice_lines = len(invoice_lines)

    po_lines = load_po_lines_for_invoice(
        db,
        tenant_id=tenant_id_int,
        company_id=company_id_int,
        po_number=result.po_number,
        po_id=result.po_id,
        document_type=document_type,
    )
    if not po_lines:
        result.details["reason"] = "po_products_not_found"
        for ln in invoice_lines:
            name = extract_invoice_line_product_name(ln) or ""
            result.line_matches.append(
                InvoiceLineAliasMatch(
                    line_no=ln.get("lineNo") or ln.get("line_no"),
                    product_name=name,
                    matched=False,
                    details={"reason": "po_products_not_found"},
                )
            )
        return result

    try:
        vendor_id_int = int(vendor_id) if vendor_id is not None else None
    except (TypeError, ValueError):
        vendor_id_int = None

    candidates = build_po_product_candidates(
        db,
        po_lines,
        vendor_id=vendor_id_int,
    )
    result.po_product_count = len(candidates)
    result.details["po_item_ids"] = [
        c.item_id for c in candidates if c.item_id is not None
    ]

    for ln in invoice_lines:
        name = extract_invoice_line_product_name(ln) or ""
        line_match = match_product_name_to_po_aliases(name, candidates)
        line_match.line_no = ln.get("lineNo") or ln.get("line_no")
        if line_match.matched:
            result.matched_line_count += 1
        result.line_matches.append(line_match)

    return result


def _fingerprint_from_matched_candidate(
    db: Session,
    hit: InvoiceLineAliasMatch,
    *,
    vendor_id: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Prefer PO-line fingerprint; fall back to vendor_items.product_fingerprint."""
    if isinstance(hit.product_fingerprint, dict) and hit.product_fingerprint:
        return hit.product_fingerprint
    if hit.item_id is None or vendor_id is None:
        return None
    vendor_item = (
        db.query(VendorItem)
        .filter(
            VendorItem.vendor_id == int(vendor_id),
            VendorItem.item_id == int(hit.item_id),
        )
        .first()
    )
    if vendor_item is None:
        vendor_item = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == int(vendor_id),
                VendorItem.id == int(hit.item_id),
            )
            .first()
        )
    if vendor_item and isinstance(vendor_item.product_fingerprint, dict):
        return vendor_item.product_fingerprint
    return None


def _apply_openai_fingerprint_for_unmatched_line(
    line: Dict[str, Any],
    *,
    product_name: str,
) -> Dict[str, Any]:
    """Create invoice-line fingerprint via OpenAI for unmatched products."""
    uom = _clean(line.get("uom"))
    item_no = _clean(line.get("itemNo") or line.get("item_no"))
    fingerprint = build_invoice_line_fingerprint_with_openai(
        product_name or _clean(line.get("description")) or "",
        uom=uom,
        item_no=item_no,
    )
    line["itemFingerprint"] = fingerprint
    # Backfill missing UOM from extracted fingerprint.
    if not uom and _clean(fingerprint.get("uom")):
        line["uom"] = fingerprint["uom"]
    return fingerprint


def enrich_invoice_payload_with_po_alias_matches(
    db: Session,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Run the invoice matching engine for every invoice line.

    Engine loop per line:
      product_name search → (on fail) OpenAI fingerprint → fingerprint sequence
      until candidate count == 1, else await user confirm.
    """
    logger.info(
        "[INVOICE MATCH] step=engine_batch_start po_number=%s po_id=%s vendor_id=%s",
        payload.get("poNumber") or payload.get("po_number"),
        payload.get("poId") or payload.get("po_id"),
        payload.get("vendorId") or payload.get("vendor_id"),
    )
    match_result = match_invoice_lines_to_po_aliases(db, payload)
    logger.info(
        "[INVOICE MATCH] step=alias_pass_done po_number=%s po_products=%s "
        "alias_matched=%s/%s details=%s",
        match_result.po_number,
        match_result.po_product_count,
        match_result.matched_line_count,
        match_result.total_invoice_lines,
        match_result.details,
    )
    lines = payload.get("lines")
    if not isinstance(lines, list):
        payload["_poAliasMatch"] = {
            "matched_line_count": 0,
            "total_invoice_lines": 0,
            "details": match_result.details,
        }
        return payload

    match_by_line_no: Dict[Any, InvoiceLineAliasMatch] = {}
    for m in match_result.line_matches:
        if m.line_no is not None:
            match_by_line_no[m.line_no] = m

    try:
        vendor_id = payload.get("vendorId") or payload.get("vendor_id")
        vendor_id_int = int(vendor_id) if vendor_id is not None else None
    except (TypeError, ValueError):
        vendor_id_int = None

    tenant_id = payload.get("tenantId") or payload.get("tenant_id")
    company_id = payload.get("companyId") or payload.get("company_id")
    document_type = _clean(
        payload.get("poDocumentType") or payload.get("po_document_type")
    )
    candidates: List[PoProductCandidate] = []
    try:
        po_lines = load_po_lines_for_invoice(
            db,
            tenant_id=int(tenant_id),
            company_id=int(company_id),
            po_number=match_result.po_number,
            po_id=match_result.po_id,
            document_type=document_type,
        )
        candidates = build_po_product_candidates(
            db,
            po_lines,
            vendor_id=vendor_id_int,
        )
    except Exception as exc:
        logger.warning("Could not build PO candidates for matching engine: %s", exc)

    engine = InvoiceMatchingEngine(
        candidates=candidates,
        vendor_id=vendor_id_int,
    )

    applied = 0
    fingerprints_created = 0
    auto_matched = 0
    awaiting_user = 0
    engine_summaries: List[Dict[str, Any]] = []

    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        line_no = line.get("lineNo") or line.get("line_no") or (idx + 1)
        alias_hit = match_by_line_no.get(line_no)
        product_name = (
            (alias_hit.product_name if alias_hit else None)
            or extract_invoice_line_product_name(line)
            or ""
        )

        result = engine.run_for_line(
            product_name=product_name,
            line=line,
            alias_hit=alias_hit,
            db=db,
        )
        engine.apply_to_line(line, result)

        if "openai_fingerprint" in result.engine_path:
            fingerprints_created += 1
        if result.matched:
            applied += 1
            if result.match_type == MATCH_TYPE_AUTO:
                auto_matched += 1
        if result.line_status in (
            LINE_STATUS_PENDING_CONFIRM,
            LINE_STATUS_PENDING_SELECT,
            LINE_STATUS_PENDING_MANUAL,
            LINE_STATUS_NO_MATCH,
        ):
            if not result.matched or result.match_type in (
                MATCH_TYPE_ALIAS,
                MATCH_TYPE_AUTO,
            ):
                # Alias/auto still need user confirm; multiple/manual always do.
                awaiting_user += 1

        engine_summaries.append(
            {
                "line_no": line_no,
                "product_name": result.product_name,
                "match_type": result.match_type,
                "line_status": result.line_status,
                "matched": result.matched,
                "item_id": result.item_id,
                "po_line_no": result.po_line_no,
                "engine_path": result.engine_path,
                "stop_reason": result.details.get("stopReason"),
            }
        )

    payload["_poAliasMatch"] = {
        "engine": "InvoiceMatchingEngine",
        "po_number": match_result.po_number,
        "po_id": match_result.po_id,
        "po_product_count": len(candidates) or match_result.po_product_count,
        "matched_line_count": match_result.matched_line_count,
        "applied_line_count": applied,
        "auto_matched": auto_matched,
        "fingerprints_created": fingerprints_created,
        "awaiting_user_confirm": awaiting_user,
        "total_invoice_lines": match_result.total_invoice_lines,
        "fingerprint_sequence": list(InvoiceMatchingEngine.FINGERPRINT_SEQUENCE),
        "details": match_result.details,
        "line_results": engine_summaries,
        "line_matches": [
            {
                "line_no": m.line_no,
                "product_name": m.product_name,
                "matched": m.matched,
                "item_id": m.item_id,
                "po_line_no": m.po_line_no,
                "matched_alias": m.matched_alias,
                "match_method": m.match_method,
                "details": m.details,
            }
            for m in match_result.line_matches
        ],
    }

    logger.info(
        "[INVOICE MATCH] step=engine_batch_summary po=%s products=%s alias=%s auto=%s "
        "fingerprints=%s awaiting_user=%s/%s",
        match_result.po_number,
        len(candidates),
        match_result.matched_line_count,
        auto_matched,
        fingerprints_created,
        awaiting_user,
        match_result.total_invoice_lines,
    )
    print(
        "[INVOICE MATCHING ENGINE]",
        {
            "po_number": match_result.po_number,
            "po_product_count": len(candidates),
            "alias_matched": match_result.matched_line_count,
            "auto_matched": auto_matched,
            "fingerprints_created": fingerprints_created,
            "awaiting_user_confirm": awaiting_user,
            "total_invoice_lines": match_result.total_invoice_lines,
            "fingerprint_sequence": list(InvoiceMatchingEngine.FINGERPRINT_SEQUENCE),
        },
    )
    return payload
