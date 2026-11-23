"""
Shared helpers for invoice, rate-contract, and sales product matching engines.

Domain engines keep their own candidate pools and orchestration; this module
holds normalization, alias scoring, embedding ranking, and match-review shape.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, TypeVar

from services.embedding_service import embed_text

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")

# Embedding distance thresholds (shared across engines).
EMBEDDING_STRONG_MAX_DISTANCE = 0.08
EMBEDDING_DUPLICATE_MAX_DISTANCE = 0.12
EMBEDDING_PICK_MAX_DISTANCE = 0.15

# Match types.
MATCH_TYPE_EXACT = "exact"
MATCH_TYPE_ALIAS = "alias"
MATCH_TYPE_EMBEDDING = "embedding"
MATCH_TYPE_AUTO = "auto"
MATCH_TYPE_MULTIPLE = "multiple"
MATCH_TYPE_NONE = "none"
MATCH_TYPE_MANUAL = "manual"

# Line / review statuses.
LINE_STATUS_CONFIRMED = "CONFIRMED"
LINE_STATUS_PENDING_SELECT = "PENDING_SELECT"
LINE_STATUS_NO_MATCH = "NO_MATCH"
LINE_STATUS_PENDING_CONFIRM = "PENDING_CONFIRM"
LINE_STATUS_PENDING_MANUAL = "PENDING_MANUAL"

# UI colours (shared: green = auto, yellow/amber = multiple, red = no match).
MATCH_COLOR_GREEN = "green"
MATCH_COLOR_YELLOW = "yellow"
MATCH_COLOR_AMBER = "amber"
MATCH_COLOR_RED = "red"

T = TypeVar("T")


@dataclass(frozen=True)
class MatchState:
    """Traffic-light match outcome from candidate COUNT (0 / 1 / many)."""

    match_color: str
    line_status: str
    match_type: str
    candidate_count: int


def resolve_match_state_from_count(
    candidate_count: int,
    *,
    single_match_type: str = MATCH_TYPE_EXACT,
    multiple_match_type: str = MATCH_TYPE_MULTIPLE,
    no_match_type: str = MATCH_TYPE_NONE,
) -> MatchState:
    """
    Map engine candidate COUNT to shared green / amber / red semantics.

    COUNT = 1  → green  (auto match)
    COUNT > 1  → amber  (multiple matches → user picks)
    COUNT = 0  → red    (no match → show full catalog / PO pool)
    """
    count = max(0, int(candidate_count))
    if count == 0:
        return MatchState(
            match_color=MATCH_COLOR_RED,
            line_status=LINE_STATUS_NO_MATCH,
            match_type=no_match_type,
            candidate_count=0,
        )
    if count == 1:
        return MatchState(
            match_color=MATCH_COLOR_GREEN,
            line_status=LINE_STATUS_PENDING_CONFIRM,
            match_type=single_match_type,
            candidate_count=1,
        )
    return MatchState(
        match_color=MATCH_COLOR_YELLOW,
        line_status=LINE_STATUS_PENDING_SELECT,
        match_type=multiple_match_type,
        candidate_count=count,
    )


def resolve_match_color_from_count(candidate_count: int) -> str:
    return resolve_match_state_from_count(candidate_count).match_color


def clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def normalize_product_name(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = text.lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def names_are_close_spelling_variant(
    product_name: str,
    candidate_name: str,
    *,
    min_ratio: float = 0.78,
) -> bool:
    """
    True when two product names differ only by minor spelling (e.g. Brownys/Brownies).
    """
    left = normalize_product_name(product_name)
    right = normalize_product_name(candidate_name)
    if not left or not right:
        return False
    if left == right:
        return True
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if left_tokens and left_tokens == right_tokens:
        return True
    return SequenceMatcher(None, left, right).ratio() >= min_ratio


def score_alias_match(
    product_name: str,
    alias: str,
    *,
    allow_contains: bool = False,
) -> Optional[Tuple[int, str]]:
    """
    Return ``(priority, method)`` when names match; lower priority wins.

    Exact matches always score 0. When ``allow_contains`` is True (invoice),
    substring matches score 1 when overlap length is at least 3.
    """
    left = normalize_product_name(product_name)
    right = normalize_product_name(alias)
    if not left or not right:
        return None
    if left == right:
        return (0, "exact")
    if allow_contains and (left in right or right in left):
        overlap = min(len(left), len(right))
        if overlap >= 3:
            return (1, "contains")
    return None


def dedupe_aliases(aliases: Sequence[str]) -> List[str]:
    """Deduplicate alias strings by normalized form, preserving order."""
    seen: Set[str] = set()
    deduped: List[str] = []
    for alias in aliases:
        key = normalize_product_name(alias)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(alias)
    return deduped


def parse_aliases_by_item_id(rows: Sequence[Dict[str, Any]]) -> Dict[int, List[str]]:
    """Group ocr_alias_dictionary rows by itemId / item_id."""
    aliases_by_id: Dict[int, List[str]] = {}
    for row in rows:
        raw_id = row.get("itemId", row.get("item_id"))
        try:
            item_id = int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            item_id = None
        if item_id is None:
            continue
        for key in ("aliasValue", "alias_value", "canonicalValue", "canonical_value"):
            val = clean_text(row.get(key))
            if val:
                aliases_by_id.setdefault(item_id, []).append(val)
    return aliases_by_id


def cosine_distance(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 1.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_l = sum(a * a for a in left) ** 0.5
    norm_r = sum(b * b for b in right) ** 0.5
    if norm_l == 0 or norm_r == 0:
        return 1.0
    similarity = dot / (norm_l * norm_r)
    return max(0.0, 1.0 - similarity)


def rank_candidates_by_embedding(
    product_name: str,
    candidates: Sequence[T],
    label_fn: Callable[[T], str],
    *,
    tie_breaker: Optional[Callable[[T], Any]] = None,
    log_context: str = "product",
) -> List[Tuple[T, float]]:
    """Rank candidates by embedding cosine distance to ``product_name``."""
    query = clean_text(product_name)
    if not query or not candidates:
        return [(candidate, 1.0) for candidate in candidates]

    try:
        query_vector = embed_text(query)
    except Exception as exc:
        logger.warning(
            "Could not embed %s name %r: %s",
            log_context,
            query,
            exc,
        )
        return [(candidate, 1.0) for candidate in candidates]

    ranked: List[Tuple[T, float]] = []
    for candidate in candidates:
        label = clean_text(label_fn(candidate)) or ""
        if not label:
            ranked.append((candidate, 1.0))
            continue
        try:
            vector = embed_text(label)
            dist = cosine_distance(query_vector, vector)
        except Exception as exc:
            logger.warning("Could not embed %s label %r: %s", log_context, label, exc)
            dist = 1.0
        ranked.append((candidate, dist))

    if tie_breaker is not None:
        ranked.sort(key=lambda row: (row[1], tie_breaker(row[0])))
    else:
        ranked.sort(key=lambda row: row[1])

    for candidate, dist in ranked:
        label = clean_text(label_fn(candidate)) or ""
        log_product_embedding_distance(
            log_context=log_context,
            query=query,
            candidate_label=label or "(empty label)",
            distance=dist,
            product_name=query,
            status="ranked",
        )
    return ranked


def describe_embedding_acceptance(
    product_name: str,
    candidate_name: str,
    distance: float,
) -> Tuple[bool, str]:
    """Return whether distance qualifies and a short reason string for logs."""
    if distance <= EMBEDDING_STRONG_MAX_DISTANCE:
        return True, f"strong<={EMBEDDING_STRONG_MAX_DISTANCE}"
    if distance <= EMBEDDING_DUPLICATE_MAX_DISTANCE:
        want = normalize_product_name(product_name)
        have = normalize_product_name(candidate_name)
        if want and have and want == have:
            return True, f"exact_name<={EMBEDDING_DUPLICATE_MAX_DISTANCE}"
        return (
            False,
            f"name_mismatch distance={distance:.4f} max={EMBEDDING_DUPLICATE_MAX_DISTANCE}",
        )
    return False, f"distance>{EMBEDDING_DUPLICATE_MAX_DISTANCE}"


def log_product_embedding_distance(
    *,
    log_context: str,
    query: str,
    candidate_label: str,
    distance: float,
    product_name: Optional[str] = None,
    status: str = "evaluated",
    extra: Optional[str] = None,
) -> None:
    """Log one OCR query vs catalog candidate embedding distance."""
    name = product_name or query
    acceptable, reason = describe_embedding_acceptance(name, candidate_label, distance)
    suffix = f" {extra}" if extra else ""
    logger.info(
        "[MATCH EMBEDDING] context=%s status=%s query=%r candidate=%r "
        "distance=%.4f acceptable=%s reason=%s%s",
        log_context,
        status,
        query,
        candidate_label,
        distance,
        acceptable,
        reason,
        suffix,
    )


def build_match_review(
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
    """Standard match-review payload attached to OCR lines or import results."""
    review: Dict[str, Any] = {
        "matchType": match_type,
        "lineStatus": line_status,
        "productName": product_name,
    }
    if match_color:
        review["matchColor"] = match_color
    if suggested:
        review["suggested"] = suggested
    if ranked_candidates:
        review["rankedCandidates"] = ranked_candidates
    if all_po_lines:
        review["allPoLines"] = all_po_lines
    if extra:
        review.update(extra)
    return review


def embedding_acceptable_for_match(
    product_name: str,
    candidate_name: str,
    distance: float,
) -> bool:
    """True when embedding distance is strong enough to treat as a duplicate match."""
    acceptable, _ = describe_embedding_acceptance(product_name, candidate_name, distance)
    return acceptable


def normalize_product_code(code: Optional[str]) -> str:
    return normalize_product_name(code) if code else ""


def extract_line_product_code(line: Dict[str, Any]) -> Optional[str]:
    """Read SKU / ERP / vendor product code from an OCR or ERP-shaped line dict."""
    for key in (
        "item_no",
        "itemNo",
        "erp_code",
        "erpCode",
        "vendor_product_code",
        "vendorProductCode",
        "product_code",
        "productCode",
        "item_code",
        "itemCode",
        "sku",
        "SKU",
    ):
        value = clean_text(line.get(key))
        if value:
            return value
    return None


def collect_product_codes(*values: Optional[str]) -> List[str]:
    """Deduplicate non-empty product-code strings."""
    codes: List[str] = []
    for value in values:
        cleaned = clean_text(value)
        if cleaned:
            codes.append(cleaned)
    return dedupe_aliases(codes)


def product_code_in_candidate_codes(
    product_code: Optional[str],
    candidate_codes: Sequence[str],
) -> bool:
    want = normalize_product_code(product_code)
    if not want:
        return False
    return any(normalize_product_code(code) == want for code in candidate_codes if code)


def embedding_distance_between(product_name: str, candidate_name: str) -> float:
    """Cosine distance between two product-name embedding vectors."""
    left = clean_text(product_name)
    right = clean_text(candidate_name)
    if not left or not right:
        return 1.0
    try:
        left_vector = embed_text(left)
        right_vector = embed_text(right)
    except Exception as exc:
        logger.warning(
            "Could not embed product names %r vs %r: %s",
            left,
            right,
            exc,
        )
        return 1.0
    return cosine_distance(left_vector, right_vector)


def evaluate_product_code_embedding_gate(
    *,
    product_name: str,
    product_code: Optional[str],
    candidate_name: str,
    candidate_codes: Sequence[str],
    log_context: str = "product match",
) -> Tuple[bool, bool, Optional[float], Optional[str]]:
    """
    When a PDF line includes product_code, require code match AND embedding match.

    Returns (code_present, code_and_embedding_ok, distance, stop_reason).
    ``code_present`` is False when the line had no code (callers skip the gate).
    ``code_and_embedding_ok`` is True only when the code matched a candidate code
    and the OCR product name embedding matches the catalog name.
    """
    code = clean_text(product_code)
    if not code:
        return False, True, None, None

    if not product_code_in_candidate_codes(code, candidate_codes):
        return True, False, None, "product_code_not_on_candidate"

    distance = embedding_distance_between(product_name, candidate_name)
    acceptable = embedding_acceptable_for_match(
        product_name,
        candidate_name,
        distance,
    )
    log_product_embedding_distance(
        log_context=log_context,
        query=product_name,
        candidate_label=candidate_name,
        distance=distance,
        product_name=product_name,
        status="code_gate",
        extra=f"product_code={code!r} acceptable={acceptable}",
    )
    if acceptable:
        return True, True, distance, None
    return True, False, distance, "product_code_embedding_mismatch"
