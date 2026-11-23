"""
Rate-contract product matching engine.

Candidate pool is scoped to the vendor's ``vendor_items`` catalog (not PO lines).
Purpose: on re-upload, find an existing product and update it instead of inserting
a duplicate.

Engine loop for each rate-contract OCR line:
  1. Exact normalized product_name / item_name (within pack-filtered pool)
  2. Product-name / alias search against the vendor catalog pool
  3. Product-name embedding search (``item_embeddings``)
  4. Progressive fingerprint filters:
       category → manufacturer → brand → pack_size → uom
  5. Unique verified candidate → existing ``vendor_items`` row (update)
     otherwise → None (insert new row)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import VendorItem
from services.item_embedding_catalog import (
    DEFAULT_MATCH_LIMIT,
    DEFAULT_MAX_DISTANCE,
    match_product_name_embedding,
)
from services.ocr_alias_dictionary import list_product_aliases_for_item_ids
from services.product_matching_common import (
    EMBEDDING_DUPLICATE_MAX_DISTANCE,
    EMBEDDING_PICK_MAX_DISTANCE,
    EMBEDDING_STRONG_MAX_DISTANCE,
    LINE_STATUS_CONFIRMED,
    LINE_STATUS_NO_MATCH,
    LINE_STATUS_PENDING_SELECT,
    MATCH_COLOR_GREEN,
    MATCH_COLOR_RED,
    MATCH_COLOR_YELLOW,
    MATCH_TYPE_ALIAS,
    MATCH_TYPE_AUTO,
    MATCH_TYPE_EMBEDDING,
    MATCH_TYPE_EXACT,
    MATCH_TYPE_MULTIPLE,
    MATCH_TYPE_NONE,
    build_match_review,
    clean_text,
    collect_product_codes,
    dedupe_aliases,
    embedding_acceptable_for_match,
    evaluate_product_code_embedding_gate,
    log_product_embedding_distance,
    normalize_product_name,
    parse_aliases_by_item_id,
    product_code_in_candidate_codes,
    rank_candidates_by_embedding,
    resolve_match_state_from_count,
    score_alias_match,
)

logger = logging.getLogger(__name__)

PROGRESSIVE_FINGERPRINT_FIELDS = (
    "category",
    "manufacturer",
    "brand",
    "pack_size",
    "uom",
)

AUTO_IDENTITY_FIELDS = ("brand", "generic", "normalized_name")

# vendor_items.status values for rate-contract OCR catalog rows
VENDOR_ITEM_STATUS_PENDING = "PENDING"
VENDOR_ITEM_STATUS_CONFIRMED = "CONFIRMED"


@dataclass
class VendorProductCandidate:
    """One vendor catalog product that rate-contract lines may match against."""

    vendor_item_id: int
    item_id: Optional[int]
    vendor_code: Optional[str]
    product_name: Optional[str]
    item_name: Optional[str]
    uom: Optional[str]
    product_fingerprint: Optional[Dict[str, Any]] = None
    aliases: List[str] = field(default_factory=list)
    vendor_item: Optional[VendorItem] = None


@dataclass
class VendorLineAliasMatch:
    product_name: str
    matched: bool = False
    vendor_item_id: Optional[int] = None
    item_id: Optional[int] = None
    matched_alias: Optional[str] = None
    match_method: Optional[str] = None
    product_fingerprint: Optional[Dict[str, Any]] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProgressiveFingerprintMatch:
    matched: bool = False
    match_type: Optional[str] = None
    vendor_item_id: Optional[int] = None
    item_id: Optional[int] = None
    product_fingerprint: Optional[Dict[str, Any]] = None
    matched_fields: List[str] = field(default_factory=list)
    skipped_fields: List[str] = field(default_factory=list)
    remaining_after_filters: int = 0
    ranked_candidates: List[VendorProductCandidate] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RateContractMatchResult:
    matched: bool
    vendor_item: Optional[VendorItem] = None
    match_type: str = MATCH_TYPE_NONE
    needs_review: bool = False
    match_review: Optional[Dict[str, Any]] = None
    ranked_candidates: List[Dict[str, Any]] = field(default_factory=list)
    suggested: Optional[Dict[str, Any]] = None
    engine_path: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def vendor_candidate_product_name(candidate: VendorProductCandidate) -> str:
    return (
        clean_text(candidate.product_name)
        or clean_text(candidate.item_name)
        or ""
    )


def vendor_candidate_product_codes(candidate: VendorProductCandidate) -> List[str]:
    codes: List[str] = []
    item = candidate.vendor_item
    if item is not None:
        codes.extend(
            collect_product_codes(
                getattr(item, "erp_code", None),
                getattr(item, "vendor_product_code", None),
                getattr(item, "barcode", None),
                getattr(item, "company_code", None),
            )
        )
        if item.id is not None:
            codes.append(str(item.id))
        if item.item_id is not None:
            codes.append(str(item.item_id))
    return dedupe_aliases(codes)


def _vendor_candidates_matching_product_code(
    product_code: Optional[str],
    candidates: Sequence[VendorProductCandidate],
) -> List[VendorProductCandidate]:
    if not _clean(product_code):
        return []
    return [
        candidate
        for candidate in candidates
        if product_code_in_candidate_codes(
            product_code,
            vendor_candidate_product_codes(candidate),
        )
    ]


def vendor_candidate_to_review_option(
    candidate: VendorProductCandidate,
    *,
    rank: Optional[int] = None,
    distance: Optional[float] = None,
) -> Dict[str, Any]:
    option: Dict[str, Any] = {
        "vendorItemId": candidate.vendor_item_id,
        "itemId": candidate.item_id,
        "productName": vendor_candidate_product_name(candidate),
        "itemName": candidate.item_name,
        "uom": candidate.uom,
        "productFingerprint": candidate.product_fingerprint,
    }
    if rank is not None:
        option["rank"] = rank
    if distance is not None:
        option["distance"] = distance
    return option


def build_rate_contract_match_review(
    *,
    product_name: str,
    match_type: str,
    line_status: str,
    suggested: Optional[Dict[str, Any]] = None,
    ranked_candidates: Optional[List[Dict[str, Any]]] = None,
    match_color: Optional[str] = None,
    all_po_lines: Optional[List[Dict[str, Any]]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return build_match_review(
        product_name=product_name,
        match_type=match_type,
        line_status=line_status,
        suggested=suggested,
        ranked_candidates=ranked_candidates,
        match_color=match_color,
        all_po_lines=all_po_lines,
        extra=extra,
    )


def _build_multiple_review_result(
    *,
    product_name: str,
    candidates: Sequence[VendorProductCandidate],
    engine_path: List[str],
    details: Dict[str, Any],
    matched_fields: Optional[List[str]] = None,
) -> RateContractMatchResult:
    ranked_pairs = rank_vendor_candidates_by_embedding(product_name, candidates)
    ranked_opts = [
        vendor_candidate_to_review_option(
            candidate,
            rank=idx + 1,
            distance=round(float(dist), 6),
        )
        for idx, (candidate, dist) in enumerate(ranked_pairs)
    ]
    suggested = ranked_opts[0] if ranked_opts else None
    multiple = resolve_match_state_from_count(
        len(ranked_opts),
        multiple_match_type=MATCH_TYPE_MULTIPLE,
    )
    review = build_rate_contract_match_review(
        product_name=product_name,
        match_type=multiple.match_type,
        line_status=multiple.line_status,
        match_color=multiple.match_color,
        suggested=suggested,
        ranked_candidates=ranked_opts,
        extra={"matchedFields": matched_fields or []},
    )
    return RateContractMatchResult(
        matched=False,
        needs_review=True,
        match_type=multiple.match_type,
        match_review=review,
        ranked_candidates=ranked_opts,
        suggested=suggested,
        engine_path=engine_path,
        details={**details, "matchColor": multiple.match_color},
    )


def _build_no_match_review_result(
    *,
    product_name: str,
    candidates: Sequence[VendorProductCandidate],
    engine_path: List[str],
    details: Dict[str, Any],
) -> RateContractMatchResult:
    all_catalog = [vendor_candidate_to_review_option(c) for c in candidates]
    if candidates:
        rank_vendor_candidates_by_embedding(product_name, candidates)
    no_match = resolve_match_state_from_count(0, no_match_type=MATCH_TYPE_NONE)
    review = build_rate_contract_match_review(
        product_name=product_name,
        match_type=no_match.match_type,
        line_status=no_match.line_status,
        match_color=no_match.match_color,
        all_po_lines=all_catalog,
        extra=details,
    )
    return RateContractMatchResult(
        matched=False,
        needs_review=True,
        match_type=no_match.match_type,
        match_review=review,
        engine_path=engine_path,
        details={**details, "matchColor": no_match.match_color},
    )


_clean = clean_text
_normalize_name = normalize_product_name


def _parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).strip().replace(",", ""))
    except Exception:
        return None


def _format_pack_size_key(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, Decimal):
        text = format(val.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text.upper() if text else None
    text = str(val).strip()
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    numeric = _parse_decimal(text)
    if numeric is not None and re.fullmatch(r"[\d.,]+", text.replace(" ", "")):
        return _format_pack_size_key(numeric)
    return text.upper()


def _item_pack_size_key(item: VendorItem) -> Optional[str]:
    for candidate in (item.consumer_unit, item.consumer_unit_size):
        key = _format_pack_size_key(candidate)
        if key:
            return key
    fp = (
        item.product_fingerprint
        if isinstance(getattr(item, "product_fingerprint", None), dict)
        else None
    )
    if fp:
        key = _format_pack_size_key(fp.get("pack_size"))
        if key:
            return key
    if item.pack_size is not None:
        try:
            if Decimal(item.pack_size) == Decimal("1"):
                pass
            else:
                return _format_pack_size_key(item.pack_size)
        except (InvalidOperation, TypeError):
            return _format_pack_size_key(item.pack_size)
    for candidate in (item.uom, item.purchase_unit):
        key = _format_pack_size_key(candidate)
        if key:
            return key
    return None


def _pack_sizes_match(item: VendorItem, canonical: Optional[str]) -> bool:
    item_key = _item_pack_size_key(item)
    want = _format_pack_size_key(canonical)
    if not want and not item_key:
        return True
    if item_key and want and item_key == want:
        return True
    want_num = _parse_decimal(canonical)
    if (
        want_num is not None
        and item.pack_size is not None
        and re.fullmatch(r"[\d.,\s]+", str(canonical).replace(",", ""))
    ):
        try:
            if Decimal(item.pack_size) == want_num:
                return True
        except (InvalidOperation, TypeError):
            pass
    for candidate in (
        item.consumer_unit,
        item.consumer_unit_size,
        item.uom,
        item.purchase_unit,
    ):
        if want and _format_pack_size_key(candidate) == want:
            return True
    fp = (
        item.product_fingerprint
        if isinstance(getattr(item, "product_fingerprint", None), dict)
        else None
    )
    if fp and want and _format_pack_size_key(fp.get("pack_size")) == want:
        return True
    return False


def _normalize_fp_value(value: Any) -> str:
    return _normalize_name(value)


def _pack_size_tokens(value: Any) -> Set[str]:
    text = _normalize_fp_value(value)
    if not text:
        return set()
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
        ln = _normalize_fp_value(left)
        rn = _normalize_fp_value(right)
        return bool(ln and rn and (ln in rn or rn in ln))
    ln = _normalize_fp_value(left)
    rn = _normalize_fp_value(right)
    if not ln or not rn:
        return False
    return ln == rn


def candidate_fingerprint_view(candidate: VendorProductCandidate) -> Dict[str, Optional[str]]:
    fp = (
        candidate.product_fingerprint
        if isinstance(candidate.product_fingerprint, dict)
        else {}
    )
    view: Dict[str, Optional[str]] = {}
    for key in PROGRESSIVE_FINGERPRINT_FIELDS:
        value = _clean(fp.get(key)) if fp else None
        if not value and key == "uom":
            value = _clean(candidate.uom)
        view[key] = value
    return view


def _has_auto_identity_match(matched_fields: Sequence[str]) -> bool:
    matched = {str(f).strip().lower() for f in matched_fields if f}
    return bool(matched & {f.lower() for f in AUTO_IDENTITY_FIELDS})


def _same_product_name(product_name: str, candidate: VendorProductCandidate) -> bool:
    """True when OCR name equals the catalog product/item name (normalized)."""
    want = _normalize_name(product_name)
    if not want:
        return False
    for existing in (
        _normalize_name(candidate.product_name),
        _normalize_name(candidate.item_name),
    ):
        if existing and existing == want:
            return True
    return False


def _embedding_acceptable_for_duplicate(
    product_name: str,
    candidate: VendorProductCandidate,
    distance: float,
) -> bool:
    return embedding_acceptable_for_match(
        product_name,
        vendor_candidate_product_name(candidate),
        distance,
    )


def _fingerprint_alias_values(fingerprint: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(fingerprint, dict):
        return []
    values: List[str] = []
    for key in ("normalized_name", "brand", "generic", "product_name", "item_name"):
        val = _clean(fingerprint.get(key))
        if val:
            values.append(val)
    return values


def _vendor_item_alias_values(item: VendorItem) -> List[str]:
    values: List[str] = []
    for attr in ("product_name", "item_name", "brand", "description"):
        val = _clean(getattr(item, attr, None))
        if val:
            values.append(val)
    values.extend(
        _fingerprint_alias_values(
            item.product_fingerprint if isinstance(item.product_fingerprint, dict) else None
        )
    )
    return values


def build_vendor_product_candidates(
    db: Session,
    *,
    vendor_id: int,
    vendor_code: Optional[str] = None,
    exclude_vendor_item_ids: Optional[Set[int]] = None,
) -> List[VendorProductCandidate]:
    """Build vendor-scoped catalog candidates with matchable aliases."""
    q = db.query(VendorItem).filter(VendorItem.vendor_id == vendor_id)
    if vendor_code:
        q = q.filter(VendorItem.vendor_code == vendor_code)
    if exclude_vendor_item_ids:
        q = q.filter(VendorItem.id.notin_(exclude_vendor_item_ids))
    vendor_items = q.all()

    item_ids: List[int] = []
    for item in vendor_items:
        if item.item_id is not None:
            item_ids.append(int(item.item_id))

    aliases_by_item: Dict[int, List[str]] = {}
    if item_ids:
        aliases_by_item = parse_aliases_by_item_id(
            list_product_aliases_for_item_ids(item_ids)
        )

    candidates: List[VendorProductCandidate] = []
    for item in vendor_items:
        aliases = _vendor_item_alias_values(item)
        if item.item_id is not None:
            aliases.extend(aliases_by_item.get(int(item.item_id), []))
        candidates.append(
            VendorProductCandidate(
                vendor_item_id=int(item.id),
                item_id=int(item.item_id) if item.item_id is not None else None,
                vendor_code=_clean(item.vendor_code),
                product_name=_clean(item.product_name),
                item_name=_clean(item.item_name),
                uom=_clean(item.uom),
                product_fingerprint=(
                    dict(item.product_fingerprint)
                    if isinstance(item.product_fingerprint, dict)
                    else None
                ),
                aliases=dedupe_aliases(aliases),
                vendor_item=item,
            )
        )
    return candidates


def _filter_candidates_by_pack(
    candidates: Sequence[VendorProductCandidate],
    canonical_pack_size: Optional[str],
) -> List[VendorProductCandidate]:
    if not candidates:
        return []
    pack_matched: List[VendorProductCandidate] = []
    for candidate in candidates:
        item = candidate.vendor_item
        if item is not None and _pack_sizes_match(item, canonical_pack_size):
            pack_matched.append(candidate)
    if not pack_matched and not _clean(canonical_pack_size):
        return list(candidates)
    return pack_matched


_score_alias_match = score_alias_match


def match_product_name_to_vendor_aliases(
    product_name: str,
    candidates: Sequence[VendorProductCandidate],
) -> VendorLineAliasMatch:
    """Match one OCR product name against vendor-catalog aliases only."""
    result = VendorLineAliasMatch(product_name=product_name, matched=False)
    if not _normalize_name(product_name):
        result.details["reason"] = "empty_product_name"
        return result
    if not candidates:
        result.details["reason"] = "no_vendor_products"
        return result

    best: Optional[tuple] = None
    for candidate in candidates:
        for alias in candidate.aliases:
            scored = _score_alias_match(product_name, alias)
            if scored is None:
                continue
            priority, method = scored
            rank = (priority, -len(_normalize_name(alias)), candidate.vendor_item_id)
            if best is None or rank < best[0]:
                best = (rank, candidate, alias, method)

    if best is None:
        result.details["reason"] = "no_alias_match"
        return result

    _, candidate, alias, method = best
    result.matched = True
    result.vendor_item_id = candidate.vendor_item_id
    result.item_id = candidate.item_id
    result.matched_alias = alias
    result.match_method = method
    result.product_fingerprint = candidate.product_fingerprint
    result.details = {"alias_count": len(candidate.aliases)}
    return result


def _vendor_item_from_embedding_match(
    db: Session,
    vendor_id: int,
    item_code: str,
) -> Optional[VendorItem]:
    code = (item_code or "").strip()
    if not code:
        return None

    if code.upper().startswith("VI-"):
        try:
            vid = int(code.split("-", 1)[1])
        except (TypeError, ValueError):
            vid = None
        if vid is not None:
            hit = (
                db.query(VendorItem)
                .filter(VendorItem.id == vid, VendorItem.vendor_id == vendor_id)
                .first()
            )
            if hit:
                return hit

    if code.isdigit():
        vid = int(code)
        hit = (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id == vendor_id,
                or_(VendorItem.item_id == vid, VendorItem.id == vid),
            )
            .order_by(VendorItem.id.desc())
            .first()
        )
        if hit:
            return hit

    return (
        db.query(VendorItem)
        .filter(
            VendorItem.vendor_id == vendor_id,
            or_(
                VendorItem.erp_code == code,
                VendorItem.vendor_product_code == code,
                VendorItem.barcode == code,
                func.lower(VendorItem.item_name) == code.lower(),
                func.lower(VendorItem.product_name) == code.lower(),
            ),
        )
        .order_by(VendorItem.id.desc())
        .first()
    )


def _candidate_from_vendor_item(
    pool: Sequence[VendorProductCandidate],
    vendor_item: VendorItem,
) -> Optional[VendorProductCandidate]:
    for candidate in pool:
        if candidate.vendor_item_id == vendor_item.id:
            return candidate
    return None


def _resolve_via_embedding_item_name(
    pool: Sequence[VendorProductCandidate],
    item_name: str,
) -> Optional[VendorProductCandidate]:
    want = (item_name or "").strip().lower()
    if not want:
        return None
    for candidate in pool:
        existing = (
            (candidate.product_name or "").strip().lower()
            or (candidate.item_name or "").strip().lower()
        )
        if existing and existing == want:
            return candidate
    return None


def collect_embedding_matches(
    db: Session,
    *,
    vendor_id: int,
    vendor_code: Optional[str],
    product_name: str,
    pool: Sequence[VendorProductCandidate],
    alternate_names: Optional[List[str]] = None,
) -> List[tuple[VendorProductCandidate, float]]:
    embedding_queries: List[str] = []
    for value in [product_name, *(alternate_names or [])]:
        cleaned = _clean(value)
        if not cleaned or cleaned in embedding_queries:
            continue
        if len(cleaned.split()) < 2 and _clean(product_name) != cleaned:
            continue
        embedding_queries.append(cleaned)

    match_limit = max(DEFAULT_MATCH_LIMIT, 25)
    matches: List[Any] = []
    for embedding_query in embedding_queries:
        matches = match_product_name_embedding(
            embedding_query,
            vendor_code=vendor_code,
            max_distance=DEFAULT_MAX_DISTANCE,
            limit=match_limit,
        )
        if matches:
            break
    if not matches:
        return []

    pool_ids = {c.vendor_item_id for c in pool}
    hits: List[tuple[VendorProductCandidate, float]] = []
    seen_ids: Set[int] = set()
    for match in matches:
        dist = float(match.distance)
        catalog_label = _clean(match.item_name) or _clean(match.item_code) or ""
        item = _vendor_item_from_embedding_match(db, vendor_id, match.item_code)
        candidate: Optional[VendorProductCandidate] = None
        if item is not None:
            candidate = _candidate_from_vendor_item(pool, item)
        if candidate is None:
            candidate = _resolve_via_embedding_item_name(pool, match.item_name)
        if candidate is None or candidate.vendor_item_id not in pool_ids:
            log_product_embedding_distance(
                log_context="rate-contract index",
                query=product_name,
                candidate_label=catalog_label or "(unknown)",
                distance=dist,
                product_name=product_name,
                status="skipped",
                extra=f"reason=not_in_pool item_code={match.item_code!r}",
            )
            continue
        if candidate.vendor_item_id in seen_ids:
            continue
        candidate_label = vendor_candidate_product_name(candidate)
        acceptable = _embedding_acceptable_for_duplicate(product_name, candidate, dist)
        log_product_embedding_distance(
            log_context="rate-contract index",
            query=product_name,
            candidate_label=candidate_label,
            distance=dist,
            product_name=product_name,
            status="accepted" if acceptable else "rejected",
            extra=f"vendor_item_id={candidate.vendor_item_id}",
        )
        if acceptable:
            hits.append((candidate, dist))
            seen_ids.add(candidate.vendor_item_id)
    hits.sort(key=lambda row: row[1])
    return hits


def match_product_name_via_embeddings(
    db: Session,
    *,
    vendor_id: int,
    vendor_code: Optional[str],
    product_name: str,
    pool: Sequence[VendorProductCandidate],
    alternate_names: Optional[List[str]] = None,
) -> Optional[VendorProductCandidate]:
    embedding_queries: List[str] = []
    for value in [product_name, *(alternate_names or [])]:
        cleaned = _clean(value)
        if not cleaned or cleaned in embedding_queries:
            continue
        if len(cleaned.split()) < 2 and _clean(product_name) != cleaned:
            continue
        embedding_queries.append(cleaned)

    match_limit = max(DEFAULT_MATCH_LIMIT, 25)
    matches: List[Any] = []
    for embedding_query in embedding_queries:
        matches = match_product_name_embedding(
            embedding_query,
            vendor_code=vendor_code,
            max_distance=DEFAULT_MAX_DISTANCE,
            limit=match_limit,
        )
        if matches:
            logger.info(
                "[RATE CONTRACT MATCH] embedding query=%r hits=%s",
                embedding_query,
                [
                    (m.item_code, m.item_name, round(m.distance, 4))
                    for m in matches[:5]
                ],
            )
            break
    if not matches:
        return None

    pool_ids = {c.vendor_item_id for c in pool}
    for match in matches:
        dist = float(match.distance)
        item = _vendor_item_from_embedding_match(db, vendor_id, match.item_code)
        candidate: Optional[VendorProductCandidate] = None
        if item is not None:
            candidate = _candidate_from_vendor_item(pool, item)
        if candidate is None:
            candidate = _resolve_via_embedding_item_name(pool, match.item_name)
            if candidate is None:
                logger.warning(
                    "[RATE CONTRACT MATCH] embedding item_code=%r item_name=%r "
                    "did not resolve to vendor_items vendor_id=%s",
                    match.item_code,
                    match.item_name,
                    vendor_id,
                )
                continue
            logger.info(
                "[RATE CONTRACT MATCH] resolved stale embedding item_code=%r "
                "via item_name=%r -> vendor_item_id=%s",
                match.item_code,
                match.item_name,
                candidate.vendor_item_id,
            )
        if candidate.vendor_item_id not in pool_ids:
            continue
        if _embedding_acceptable_for_duplicate(product_name, candidate, dist):
            return candidate
        logger.info(
            "[RATE CONTRACT MATCH] reject embedding vendor_item_id=%s "
            "distance=%.4f product_name=%r catalog_name=%r",
            candidate.vendor_item_id,
            dist,
            product_name,
            candidate.product_name,
        )
    return None


def rank_vendor_candidates_by_embedding(
    product_name: str,
    candidates: Sequence[VendorProductCandidate],
) -> List[tuple[VendorProductCandidate, float]]:
    return rank_candidates_by_embedding(
        product_name,
        candidates,
        vendor_candidate_product_name,
        log_context="rate-contract product",
    )


def progressive_match_fingerprint_to_vendor_products(
    fingerprint: Dict[str, Any],
    candidates: Sequence[VendorProductCandidate],
) -> ProgressiveFingerprintMatch:
    result = ProgressiveFingerprintMatch()
    if not isinstance(fingerprint, dict) or not fingerprint:
        result.details["reason"] = "empty_fingerprint"
        return result
    if not candidates:
        result.details["reason"] = "no_vendor_products"
        return result

    pool: List[VendorProductCandidate] = list(candidates)
    result.details["initial_pool"] = len(pool)

    for field in PROGRESSIVE_FINGERPRINT_FIELDS:
        invoice_value = _clean(fingerprint.get(field))
        if not invoice_value:
            result.skipped_fields.append(field)
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
            result.skipped_fields.append(field)
            continue

        pool = narrowed
        result.matched_fields.append(field)
        if len(pool) == 1 and _has_auto_identity_match(result.matched_fields):
            break

    result.remaining_after_filters = len(pool)
    result.ranked_candidates = list(pool)

    if len(pool) == 1 and _has_auto_identity_match(result.matched_fields):
        winner = pool[0]
        result.matched = True
        result.match_type = MATCH_TYPE_AUTO
        result.vendor_item_id = winner.vendor_item_id
        result.item_id = winner.item_id
        result.product_fingerprint = winner.product_fingerprint or fingerprint
        result.details["stop_reason"] = "candidate_count_1"
        result.details["winner_product_name"] = winner.product_name
        return result

    if len(pool) > 1 and result.matched_fields:
        result.details["reason"] = "multiple_candidates"
        return result

    result.details["reason"] = (
        "no_candidates_after_filters" if not pool else "no_identity_match"
    )
    return result


def _gate_vendor_candidate_product_code(
    *,
    product_name: str,
    product_code: Optional[str],
    candidate: VendorProductCandidate,
    pool: Sequence[VendorProductCandidate],
    engine_path: List[str],
) -> Optional[RateContractMatchResult]:
    if not _clean(product_code):
        return None

    code_hits = _vendor_candidates_matching_product_code(product_code, pool)
    if not code_hits:
        return None

    candidate_codes = vendor_candidate_product_codes(candidate)
    _, ok, distance, stop_reason = evaluate_product_code_embedding_gate(
        product_name=product_name,
        product_code=product_code,
        candidate_name=vendor_candidate_product_name(candidate),
        candidate_codes=candidate_codes,
        log_context="rate contract product",
    )

    if product_code_in_candidate_codes(product_code, candidate_codes):
        if ok:
            return None
        engine_path.append("product_code_embedding_mismatch")
        return _build_multiple_review_result(
            product_name=product_name,
            candidates=pool,
            engine_path=engine_path,
            details={
                "reason": stop_reason or "product_code_embedding_mismatch",
                "matchedProductCode": product_code,
                "embeddingDistance": round(float(distance or 0.0), 6),
            },
        )

    engine_path.append("product_code_name_conflict")
    return _build_multiple_review_result(
        product_name=product_name,
        candidates=code_hits,
        engine_path=engine_path,
        details={
            "reason": "product_code_name_conflict",
            "matchedProductCode": product_code,
        },
    )


class RateContractMatchingEngine:
    """Duplicate-detection engine for rate-contract OCR imports."""

    def __init__(
        self,
        *,
        candidates: Sequence[VendorProductCandidate],
        vendor_id: int,
        vendor_code: Optional[str] = None,
    ):
        self.candidates = list(candidates)
        self.vendor_id = vendor_id
        self.vendor_code = vendor_code

    def run_for_line(
        self,
        *,
        db: Session,
        product_name: str,
        fingerprint: Optional[Dict[str, Any]],
        canonical_pack_size: Optional[str],
        alternate_names: Optional[List[str]] = None,
        product_code: Optional[str] = None,
    ) -> RateContractMatchResult:
        engine_path: List[str] = ["vendor_catalog_pool"]
        pool = _filter_candidates_by_pack(self.candidates, canonical_pack_size)
        engine_path.append(f"pack_filter:{len(pool)}")

        logger.info(
            "[RATE CONTRACT MATCH] line_start product_name=%r pack=%r pool=%s product_code=%r",
            product_name,
            canonical_pack_size,
            len(pool),
            product_code,
        )

        if not pool:
            return _build_no_match_review_result(
                product_name=product_name,
                candidates=self.candidates,
                engine_path=engine_path,
                details={"reason": "no_pack_matched_candidates"},
            )

        if _clean(product_code):
            engine_path.append("product_code_search")
            code_hits = _vendor_candidates_matching_product_code(product_code, pool)
            if len(code_hits) == 1:
                candidate = code_hits[0]
                _, ok, distance, stop_reason = evaluate_product_code_embedding_gate(
                    product_name=product_name,
                    product_code=product_code,
                    candidate_name=vendor_candidate_product_name(candidate),
                    candidate_codes=vendor_candidate_product_codes(candidate),
                    log_context="rate contract product",
                )
                if ok:
                    logger.info(
                        "[RATE CONTRACT MATCH] product_code+embedding -> vendor_item_id=%s code=%r distance=%.4f",
                        candidate.vendor_item_id,
                        product_code,
                        float(distance or 0.0),
                    )
                    return RateContractMatchResult(
                        matched=True,
                        vendor_item=candidate.vendor_item,
                        match_type=MATCH_TYPE_EXACT,
                        engine_path=engine_path,
                        details={
                            "matchedProductCode": product_code,
                            "embeddingDistance": round(float(distance or 0.0), 6),
                            "stopReason": "product_code_and_embedding_match",
                        },
                    )
                engine_path.append("product_code_embedding_mismatch")
                return _build_multiple_review_result(
                    product_name=product_name,
                    candidates=pool,
                    engine_path=engine_path,
                    details={
                        "reason": stop_reason or "product_code_embedding_mismatch",
                        "matchedProductCode": product_code,
                        "embeddingDistance": round(float(distance or 0.0), 6),
                    },
                )
            if len(code_hits) > 1:
                engine_path.append(f"product_code_multiple:{len(code_hits)}")
                return _build_multiple_review_result(
                    product_name=product_name,
                    candidates=code_hits,
                    engine_path=engine_path,
                    details={
                        "reason": "multiple_product_code_matches",
                        "matchedProductCode": product_code,
                    },
                )

        want = _normalize_name(product_name)
        if want:
            engine_path.append("exact_name")
            for candidate in pool:
                for existing in (
                    _normalize_name(candidate.product_name),
                    _normalize_name(candidate.item_name),
                ):
                    if existing and existing == want:
                        gate = _gate_vendor_candidate_product_code(
                            product_name=product_name,
                            product_code=product_code,
                            candidate=candidate,
                            pool=pool,
                            engine_path=engine_path,
                        )
                        if gate is not None:
                            return gate
                        logger.info(
                            "[RATE CONTRACT MATCH] exact_name -> vendor_item_id=%s",
                            candidate.vendor_item_id,
                        )
                        return RateContractMatchResult(
                            matched=True,
                            vendor_item=candidate.vendor_item,
                            match_type=MATCH_TYPE_EXACT,
                            engine_path=engine_path,
                            details={"matchedName": existing},
                        )

        engine_path.append("alias_search")
        alias_hit = match_product_name_to_vendor_aliases(product_name, pool)
        if alias_hit.matched and alias_hit.vendor_item_id is not None:
            candidate = next(
                (c for c in pool if c.vendor_item_id == alias_hit.vendor_item_id),
                None,
            )
            if candidate is not None:
                gate = _gate_vendor_candidate_product_code(
                    product_name=product_name,
                    product_code=product_code,
                    candidate=candidate,
                    pool=pool,
                    engine_path=engine_path,
                )
                if gate is not None:
                    return gate
                logger.info(
                    "[RATE CONTRACT MATCH] alias %r -> vendor_item_id=%s",
                    alias_hit.matched_alias,
                    candidate.vendor_item_id,
                )
                return RateContractMatchResult(
                    matched=True,
                    vendor_item=candidate.vendor_item,
                    match_type=MATCH_TYPE_ALIAS,
                    engine_path=engine_path,
                    details={
                        "matchedAlias": alias_hit.matched_alias,
                        "matchMethod": alias_hit.match_method,
                    },
                )

        engine_path.append("embedding_search")
        embedding_hits = collect_embedding_matches(
            db,
            vendor_id=self.vendor_id,
            vendor_code=self.vendor_code,
            product_name=product_name,
            pool=pool,
            alternate_names=alternate_names,
        )
        if len(embedding_hits) > 1:
            engine_path.append(f"embedding_multiple:{len(embedding_hits)}")
            return _build_multiple_review_result(
                product_name=product_name,
                candidates=[c for c, _ in embedding_hits],
                engine_path=engine_path,
                details={
                    "reason": "multiple_embedding_matches",
                    "distances": [round(d, 6) for _, d in embedding_hits],
                },
            )
        if len(embedding_hits) == 1:
            embedding_hit, dist = embedding_hits[0]
            logger.info(
                "[RATE CONTRACT MATCH] embedding -> vendor_item_id=%s distance=%.4f",
                embedding_hit.vendor_item_id,
                dist,
            )
            return RateContractMatchResult(
                matched=True,
                vendor_item=embedding_hit.vendor_item,
                match_type=MATCH_TYPE_EMBEDDING,
                engine_path=engine_path,
                details={
                    "vendorItemId": embedding_hit.vendor_item_id,
                    "embeddingDistance": round(dist, 6),
                },
            )

        if isinstance(fingerprint, dict) and fingerprint:
            engine_path.append("fingerprint_sequence")
            progressive = progressive_match_fingerprint_to_vendor_products(
                fingerprint,
                pool,
            )
            engine_path.extend([f"filter:{f}" for f in progressive.matched_fields])

            if progressive.matched and progressive.vendor_item_id is not None:
                candidate = next(
                    (
                        c
                        for c in pool
                        if c.vendor_item_id == progressive.vendor_item_id
                    ),
                    None,
                )
                if candidate is not None and _same_product_name(product_name, candidate):
                    logger.info(
                        "[RATE CONTRACT MATCH] fingerprint_auto -> vendor_item_id=%s "
                        "fields=%s",
                        candidate.vendor_item_id,
                        progressive.matched_fields,
                    )
                    return RateContractMatchResult(
                        matched=True,
                        vendor_item=candidate.vendor_item,
                        match_type=MATCH_TYPE_AUTO,
                        engine_path=engine_path,
                        details={
                            "matchedFields": progressive.matched_fields,
                            "stopReason": progressive.details.get("stop_reason"),
                        },
                    )
                if candidate is not None:
                    logger.info(
                        "[RATE CONTRACT MATCH] reject fingerprint_auto "
                        "vendor_item_id=%s fields=%s product_name=%r catalog_name=%r",
                        candidate.vendor_item_id,
                        progressive.matched_fields,
                        product_name,
                        candidate.product_name,
                    )

            if progressive.ranked_candidates and len(progressive.ranked_candidates) > 1:
                if progressive.matched_fields:
                    engine_path.append(
                        f"fingerprint_multiple:{len(progressive.ranked_candidates)}"
                    )
                    return _build_multiple_review_result(
                        product_name=product_name,
                        candidates=progressive.ranked_candidates,
                        engine_path=engine_path,
                        details={
                            "reason": "multiple_fingerprint_candidates",
                            "matchedFields": progressive.matched_fields,
                            "stopReason": progressive.details.get("stop_reason"),
                        },
                        matched_fields=progressive.matched_fields,
                    )

            if progressive.ranked_candidates:
                ranked = rank_vendor_candidates_by_embedding(
                    product_name,
                    progressive.ranked_candidates,
                )
                if ranked:
                    best, dist = ranked[0]
                    if (
                        dist <= EMBEDDING_PICK_MAX_DISTANCE
                        and _same_product_name(product_name, best)
                    ):
                        logger.info(
                            "[RATE CONTRACT MATCH] fingerprint_ranked_pick "
                            "vendor_item_id=%s distance=%.4f",
                            best.vendor_item_id,
                            dist,
                        )
                        return RateContractMatchResult(
                            matched=True,
                            vendor_item=best.vendor_item,
                            match_type=MATCH_TYPE_AUTO,
                            engine_path=engine_path,
                            details={
                                "matchedFields": progressive.matched_fields,
                                "embeddingDistance": dist,
                                "reason": "ranked_pick_after_fingerprint",
                            },
                        )

        logger.info(
            "[RATE CONTRACT MATCH] no_match product_name=%r pack=%r",
            product_name,
            canonical_pack_size,
        )
        return _build_no_match_review_result(
            product_name=product_name,
            candidates=pool or self.candidates,
            engine_path=engine_path,
            details={"reason": "no_confident_match"},
        )


def match_rate_contract_line(
    db: Session,
    *,
    vendor_id: int,
    vendor_code: Optional[str],
    product_name: str,
    canonical_pack_size: Optional[str],
    alternate_names: Optional[List[str]] = None,
    fingerprint: Optional[Dict[str, Any]] = None,
    product_code: Optional[str] = None,
    exclude_vendor_item_ids: Optional[Set[int]] = None,
) -> RateContractMatchResult:
    """Run duplicate detection for one rate-contract OCR line."""
    candidates = build_vendor_product_candidates(
        db,
        vendor_id=vendor_id,
        vendor_code=vendor_code,
        exclude_vendor_item_ids=exclude_vendor_item_ids,
    )
    engine = RateContractMatchingEngine(
        candidates=candidates,
        vendor_id=vendor_id,
        vendor_code=vendor_code,
    )
    return engine.run_for_line(
        db=db,
        product_name=product_name,
        fingerprint=fingerprint,
        canonical_pack_size=canonical_pack_size,
        alternate_names=alternate_names,
        product_code=product_code,
    )


def find_existing_vendor_item_by_identity(
    db: Session,
    *,
    vendor_id: int,
    vendor_code: Optional[str],
    product_name: str,
    canonical_pack_size: Optional[str],
    alternate_names: Optional[List[str]] = None,
    fingerprint: Optional[Dict[str, Any]] = None,
) -> Optional[VendorItem]:
    """
    Rate-contract duplicate check: return existing ``vendor_items`` row or None.

    Uses ``RateContractMatchingEngine`` against the vendor catalog (not PO lines).
    """
    candidates = build_vendor_product_candidates(
        db,
        vendor_id=vendor_id,
        vendor_code=vendor_code,
    )
    engine = RateContractMatchingEngine(
        candidates=candidates,
        vendor_id=vendor_id,
        vendor_code=vendor_code,
    )
    result = engine.run_for_line(
        db=db,
        product_name=product_name,
        fingerprint=fingerprint,
        canonical_pack_size=canonical_pack_size,
        alternate_names=alternate_names,
    )
    if result.matched and result.vendor_item is not None:
        return result.vendor_item
    return None
