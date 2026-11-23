"""
Sales product matching engine.

Candidate pool is the ``products`` catalog (active products).
Matching strategy:
  1. Exact normalized product name
  2. Close spelling variant (e.g. Brownys → Brownies)
  3. Product-name / alias search
  4. Embedding search against the products catalog pool (not vendor item index)

Match colours for the UI:
  - green  → single confident match (auto-insert)
  - yellow → multiple candidates (user selects on click)
  - red    → no match
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import Product, Stock
from services.item_embedding_catalog import DEFAULT_MAX_DISTANCE
from services.ocr_alias_dictionary import list_product_aliases_for_item_ids
from services.product_matching_common import (
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
    dedupe_aliases,
    embedding_acceptable_for_match,
    log_product_embedding_distance,
    names_are_close_spelling_variant,
    normalize_product_name,
    parse_aliases_by_item_id,
    rank_candidates_by_embedding,
    score_alias_match,
)

logger = logging.getLogger(__name__)


@dataclass
class ProductCatalogCandidate:
    product_id: int
    name: str
    category: Optional[str] = None
    aliases: List[str] = field(default_factory=list)
    product: Optional[Product] = None


@dataclass
class SalesProductMatchResult:
    matched: bool
    product: Optional[Product] = None
    match_type: str = MATCH_TYPE_NONE
    match_color: str = MATCH_COLOR_RED
    line_status: str = LINE_STATUS_NO_MATCH
    needs_review: bool = False
    match_review: Optional[Dict[str, Any]] = None
    ranked_candidates: List[Dict[str, Any]] = field(default_factory=list)
    suggested: Optional[Dict[str, Any]] = None
    engine_path: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def product_candidate_to_review_option(
    candidate: ProductCatalogCandidate,
    *,
    rank: Optional[int] = None,
    distance: Optional[float] = None,
) -> Dict[str, Any]:
    option: Dict[str, Any] = {
        "productId": candidate.product_id,
        "productName": candidate.name,
        "category": candidate.category,
    }
    if rank is not None:
        option["rank"] = rank
    if distance is not None:
        option["distance"] = distance
    return option


def build_sales_match_review(
    *,
    product_name: str,
    match_type: str,
    line_status: str,
    match_color: str,
    suggested: Optional[Dict[str, Any]] = None,
    ranked_candidates: Optional[List[Dict[str, Any]]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return build_match_review(
        product_name=product_name,
        match_type=match_type,
        line_status=line_status,
        match_color=match_color,
        suggested=suggested,
        ranked_candidates=ranked_candidates,
        extra=extra,
    )


def _build_confirmed_result(
    *,
    product: Product,
    product_name: str,
    match_type: str,
    engine_path: List[str],
    details: Optional[Dict[str, Any]] = None,
) -> SalesProductMatchResult:
    candidate = ProductCatalogCandidate(
        product_id=int(product.product_id),
        name=clean_text(product.name) or "",
        category=clean_text(product.category),
        product=product,
    )
    suggested = product_candidate_to_review_option(candidate)
    review = build_sales_match_review(
        product_name=product_name,
        match_type=match_type,
        line_status=LINE_STATUS_CONFIRMED,
        match_color=MATCH_COLOR_GREEN,
        suggested=suggested,
    )
    return SalesProductMatchResult(
        matched=True,
        product=product,
        match_type=match_type,
        match_color=MATCH_COLOR_GREEN,
        line_status=LINE_STATUS_CONFIRMED,
        match_review=review,
        suggested=suggested,
        engine_path=engine_path,
        details=details or {},
    )


def _build_multiple_result(
    *,
    product_name: str,
    candidates: Sequence[ProductCatalogCandidate],
    engine_path: List[str],
    details: Dict[str, Any],
    ranked_pairs: Optional[Sequence[tuple[ProductCatalogCandidate, float]]] = None,
) -> SalesProductMatchResult:
    pairs = list(ranked_pairs) if ranked_pairs is not None else rank_product_candidates_by_embedding(
        product_name, candidates
    )
    ranked_opts = [
        product_candidate_to_review_option(
            candidate,
            rank=idx + 1,
            distance=round(float(dist), 6),
        )
        for idx, (candidate, dist) in enumerate(pairs)
    ]
    suggested = ranked_opts[0] if ranked_opts else None
    review = build_sales_match_review(
        product_name=product_name,
        match_type=MATCH_TYPE_MULTIPLE,
        line_status=LINE_STATUS_PENDING_SELECT,
        match_color=MATCH_COLOR_YELLOW,
        suggested=suggested,
        ranked_candidates=ranked_opts,
    )
    return SalesProductMatchResult(
        matched=False,
        needs_review=True,
        match_type=MATCH_TYPE_MULTIPLE,
        match_color=MATCH_COLOR_YELLOW,
        line_status=LINE_STATUS_PENDING_SELECT,
        match_review=review,
        ranked_candidates=ranked_opts,
        suggested=suggested,
        engine_path=engine_path,
        details=details,
    )


def _build_no_match_result(
    *,
    product_name: str,
    engine_path: List[str],
    details: Optional[Dict[str, Any]] = None,
) -> SalesProductMatchResult:
    review = build_sales_match_review(
        product_name=product_name,
        match_type=MATCH_TYPE_NONE,
        line_status=LINE_STATUS_NO_MATCH,
        match_color=MATCH_COLOR_RED,
    )
    return SalesProductMatchResult(
        matched=False,
        match_type=MATCH_TYPE_NONE,
        match_color=MATCH_COLOR_RED,
        line_status=LINE_STATUS_NO_MATCH,
        match_review=review,
        engine_path=engine_path,
        details=details or {"reason": "no_confident_match"},
    )


def build_product_catalog_candidates(
    db: Session,
    *,
    active_only: bool = True,
) -> List[ProductCatalogCandidate]:
    """Build product catalog candidates with matchable aliases."""
    q = db.query(Product)
    if active_only:
        q = q.filter(or_(Product.is_active.is_(True), Product.is_active.is_(None)))
    products = q.all()

    product_ids = [int(p.product_id) for p in products if p.product_id is not None]
    aliases_by_id: Dict[int, List[str]] = {}
    if product_ids:
        aliases_by_id = parse_aliases_by_item_id(
            list_product_aliases_for_item_ids(product_ids)
        )

    candidates: List[ProductCatalogCandidate] = []
    for product in products:
        if product.product_id is None:
            continue
        aliases: List[str] = []
        name = clean_text(product.name)
        if name:
            aliases.append(name)
        aliases.extend(aliases_by_id.get(int(product.product_id), []))
        candidates.append(
            ProductCatalogCandidate(
                product_id=int(product.product_id),
                name=name or f"Product {product.product_id}",
                category=clean_text(product.category),
                aliases=dedupe_aliases(aliases),
                product=product,
            )
        )
    return candidates


def match_product_name_to_aliases(
    product_name: str,
    candidates: Sequence[ProductCatalogCandidate],
) -> List[ProductCatalogCandidate]:
    """Return all catalog products whose alias exactly matches the OCR name."""
    if not normalize_product_name(product_name) or not candidates:
        return []

    hits: List[ProductCatalogCandidate] = []
    seen_ids: Set[int] = set()
    for candidate in candidates:
        for alias in candidate.aliases:
            if score_alias_match(product_name, alias) is None:
                continue
            if candidate.product_id in seen_ids:
                break
            hits.append(candidate)
            seen_ids.add(candidate.product_id)
            break
    return hits


def _candidate_for_product(
    pool: Sequence[ProductCatalogCandidate],
    product: Product,
) -> Optional[ProductCatalogCandidate]:
    for candidate in pool:
        if candidate.product_id == int(product.product_id):
            return candidate
    return None


def _resolve_product_from_stock_name(
    db: Session,
    *,
    stock_name: str,
    pool: Sequence[ProductCatalogCandidate],
) -> Optional[ProductCatalogCandidate]:
    """Map a stock master row name to an existing products-catalog candidate."""
    want = normalize_product_name(stock_name)
    if not want:
        return None

    exact = [
        c
        for c in pool
        if normalize_product_name(c.name) == want and c.product is not None
    ]
    if len(exact) == 1:
        return exact[0]

    spelling = match_spelling_variants(stock_name, pool)
    if len(spelling) == 1:
        return spelling[0]

    hit = (
        db.query(Product)
        .filter(func.lower(Product.name) == stock_name.strip().lower())
        .order_by(Product.product_id.desc())
        .first()
    )
    if hit is not None:
        return _candidate_for_product(pool, hit)
    return None


def match_via_stock_master(
    db: Session,
    *,
    product_code: Optional[str],
    product_name: str,
    pool: Sequence[ProductCatalogCandidate],
) -> Optional[ProductCatalogCandidate]:
    """
    Resolve a sales line through the stock master (``stock`` table).

    Stock rows are matched by ``product_code`` or name; the linked
    ``products`` catalog row must already exist — nothing is created here.
    """
    code = clean_text(product_code)
    stock: Optional[Stock] = None
    if code:
        stock = (
            db.query(Stock)
            .filter(Stock.product_code == code)
            .order_by(Stock.stock_id.asc())
            .first()
        )
    if stock is None and product_name:
        name_norm = product_name.strip().lower()
        stock = (
            db.query(Stock)
            .filter(func.lower(Stock.product_name) == name_norm)
            .order_by(Stock.stock_id.asc())
            .first()
        )
    if stock is None:
        return None
    return _resolve_product_from_stock_name(
        db,
        stock_name=stock.product_name or product_name,
        pool=pool,
    )


def match_spelling_variants(
    product_name: str,
    candidates: Sequence[ProductCatalogCandidate],
) -> List[ProductCatalogCandidate]:
    """Return catalog products whose name is a close spelling variant of the OCR text."""
    if not normalize_product_name(product_name) or not candidates:
        return []

    hits: List[ProductCatalogCandidate] = []
    seen_ids: Set[int] = set()
    for candidate in candidates:
        if candidate.product_id in seen_ids:
            continue
        if names_are_close_spelling_variant(product_name, candidate.name):
            hits.append(candidate)
            seen_ids.add(candidate.product_id)
    return hits


def rank_product_candidates_by_embedding(
    product_name: str,
    candidates: Sequence[ProductCatalogCandidate],
) -> List[tuple[ProductCatalogCandidate, float]]:
    return rank_candidates_by_embedding(
        product_name,
        candidates,
        lambda candidate: candidate.name,
        log_context="sales product",
    )


def partition_pool_embedding_matches(
    product_name: str,
    pool: Sequence[ProductCatalogCandidate],
) -> Tuple[List[tuple[ProductCatalogCandidate, float]], List[tuple[ProductCatalogCandidate, float]]]:
    """
    Rank sales lines against the products catalog pool once.

    Returns (auto_accept_hits, suggest_hits) where suggest_hits are within
    DEFAULT_MAX_DISTANCE but may need user confirmation.
    """
    ranked = rank_product_candidates_by_embedding(product_name, pool)
    auto_hits: List[tuple[ProductCatalogCandidate, float]] = []
    suggest_hits: List[tuple[ProductCatalogCandidate, float]] = []
    seen_suggest: Set[int] = set()

    for candidate, dist in ranked:
        distance = float(dist)
        if distance > DEFAULT_MAX_DISTANCE:
            continue
        log_product_embedding_distance(
            log_context="sales product pool",
            query=product_name,
            candidate_label=candidate.name,
            distance=distance,
            product_name=product_name,
            status="ranked",
            extra=f"product_id={candidate.product_id}",
        )
        if candidate.product_id not in seen_suggest:
            suggest_hits.append((candidate, distance))
            seen_suggest.add(candidate.product_id)
        if embedding_acceptable_for_match(product_name, candidate.name, distance):
            auto_hits.append((candidate, distance))

    auto_hits.sort(key=lambda row: row[1])
    suggest_hits.sort(key=lambda row: row[1])
    return auto_hits, suggest_hits


class SalesProductMatchingEngine:
    """Match sales OCR/CSV lines against the products catalog."""

    def __init__(
        self,
        *,
        candidates: Sequence[ProductCatalogCandidate],
        foundation_by_code: Optional[Dict[str, Dict[str, Any]]] = None,
        foundation_by_name: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.candidates = list(candidates)
        self.foundation_by_code = foundation_by_code or {}
        self.foundation_by_name = foundation_by_name or {}
        self._line_cache: Dict[Tuple[str, str, str], SalesProductMatchResult] = {}

    def run_for_line(
        self,
        *,
        db: Session,
        product_name: str,
        category: Optional[str] = None,
        product_code: Optional[str] = None,
    ) -> SalesProductMatchResult:
        cache_key = (
            normalize_product_name(product_name),
            normalize_product_name(category or ""),
            normalize_product_name(product_code or ""),
        )
        cached = self._line_cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._run_for_line_uncached(
            db=db,
            product_name=product_name,
            category=category,
            product_code=product_code,
        )
        self._line_cache[cache_key] = result
        return result

    def _run_for_line_uncached(
        self,
        *,
        db: Session,
        product_name: str,
        category: Optional[str] = None,
        product_code: Optional[str] = None,
    ) -> SalesProductMatchResult:
        engine_path: List[str] = ["product_catalog_pool"]
        pool = list(self.candidates)

        logger.info(
            "[SALES MATCH] line_start product_name=%r product_code=%r category=%r pool=%s",
            product_name,
            product_code,
            category,
            len(pool),
        )

        if not pool:
            return _build_no_match_result(
                product_name=product_name,
                engine_path=engine_path,
                details={"reason": "empty_product_pool"},
            )

        if self.foundation_by_code or self.foundation_by_name:
            from services.sales_foundation_catalog import collect_vendor_foundation_matches

            engine_path.append("vendor_catalog")
            vendor_candidates = collect_vendor_foundation_matches(
                db,
                product_code=product_code,
                product_name=product_name,
                pool=pool,
                foundation_by_code=self.foundation_by_code,
                foundation_by_name=self.foundation_by_name,
            )
            if len(vendor_candidates) == 1 and vendor_candidates[0].product is not None:
                candidate = vendor_candidates[0]
                logger.info(
                    "[SALES MATCH] vendor_catalog -> product_id=%s name=%r erp=%r",
                    candidate.product_id,
                    candidate.name,
                    product_code,
                )
                return _build_confirmed_result(
                    product=candidate.product,
                    product_name=product_name,
                    match_type=MATCH_TYPE_EXACT,
                    engine_path=engine_path,
                    details={
                        "matchedName": candidate.name,
                        "source": "vendor_catalog",
                        "productCode": product_code,
                        "foundationProductId": candidate.product_id,
                    },
                )
            if len(vendor_candidates) > 1:
                engine_path.append(f"vendor_multiple:{len(vendor_candidates)}")
                return _build_multiple_result(
                    product_name=product_name,
                    candidates=vendor_candidates,
                    engine_path=engine_path,
                    details={"reason": "multiple_vendor_catalog_matches"},
                )

        engine_path.append("stock_master")
        stock_candidate = match_via_stock_master(
            db,
            product_code=product_code,
            product_name=product_name,
            pool=pool,
        )
        if stock_candidate is not None and stock_candidate.product is not None:
            logger.info(
                "[SALES MATCH] stock_master -> product_id=%s stock_name=%r",
                stock_candidate.product.product_id,
                stock_candidate.name,
            )
            return _build_confirmed_result(
                product=stock_candidate.product,
                product_name=product_name,
                match_type=MATCH_TYPE_EXACT,
                engine_path=engine_path,
                details={
                    "matchedName": stock_candidate.name,
                    "source": "stock_master",
                    "productCode": product_code,
                },
            )

        result = self._match_against_pool(
            product_name=product_name,
            pool=pool,
            engine_path=engine_path,
        )
        if result.matched or result.needs_review:
            return result

        logger.info("[SALES MATCH] no_match product_name=%r", product_name)
        return _build_no_match_result(
            product_name=product_name,
            engine_path=engine_path,
        )

    def _match_against_pool(
        self,
        *,
        product_name: str,
        pool: Sequence[ProductCatalogCandidate],
        engine_path: List[str],
    ) -> SalesProductMatchResult:
        if not pool:
            return _build_no_match_result(
                product_name=product_name,
                engine_path=engine_path,
                details={"reason": "empty_product_pool"},
            )

        want = normalize_product_name(product_name)
        if want:
            engine_path.append("exact_name")
            exact_hits = [c for c in pool if normalize_product_name(c.name) == want]
            if len(exact_hits) == 1:
                product = exact_hits[0].product
                if product is not None:
                    logger.info(
                        "[SALES MATCH] exact_name -> product_id=%s",
                        product.product_id,
                    )
                    return _build_confirmed_result(
                        product=product,
                        product_name=product_name,
                        match_type=MATCH_TYPE_EXACT,
                        engine_path=engine_path,
                        details={"matchedName": exact_hits[0].name},
                    )
            if len(exact_hits) > 1:
                engine_path.append(f"exact_multiple:{len(exact_hits)}")
                return _build_multiple_result(
                    product_name=product_name,
                    candidates=exact_hits,
                    engine_path=engine_path,
                    details={"reason": "multiple_exact_name_matches"},
                )

        engine_path.append("spelling_variant")
        spelling_hits = match_spelling_variants(product_name, pool)
        if len(spelling_hits) == 1:
            product = spelling_hits[0].product
            if product is not None:
                logger.info(
                    "[SALES MATCH] spelling_variant -> product_id=%s matchedName=%r",
                    product.product_id,
                    spelling_hits[0].name,
                )
                return _build_confirmed_result(
                    product=product,
                    product_name=product_name,
                    match_type=MATCH_TYPE_AUTO,
                    engine_path=engine_path,
                    details={
                        "matchedName": spelling_hits[0].name,
                        "reason": "close_spelling_variant",
                    },
                )
        if len(spelling_hits) > 1:
            engine_path.append(f"spelling_multiple:{len(spelling_hits)}")
            return _build_multiple_result(
                product_name=product_name,
                candidates=spelling_hits,
                engine_path=engine_path,
                details={"reason": "multiple_spelling_variants"},
            )

        engine_path.append("alias_search")
        alias_hits = match_product_name_to_aliases(product_name, pool)
        if len(alias_hits) == 1:
            product = alias_hits[0].product
            if product is not None:
                logger.info(
                    "[SALES MATCH] alias -> product_id=%s",
                    product.product_id,
                )
                return _build_confirmed_result(
                    product=product,
                    product_name=product_name,
                    match_type=MATCH_TYPE_ALIAS,
                    engine_path=engine_path,
                    details={"matchedProductId": product.product_id},
                )
        if len(alias_hits) > 1:
            engine_path.append(f"alias_multiple:{len(alias_hits)}")
            return _build_multiple_result(
                product_name=product_name,
                candidates=alias_hits,
                engine_path=engine_path,
                details={"reason": "multiple_alias_matches"},
            )

        engine_path.append("embedding_search")
        auto_hits, suggest_hits = partition_pool_embedding_matches(product_name, pool)
        if len(auto_hits) > 1:
            engine_path.append(f"embedding_multiple:{len(auto_hits)}")
            return _build_multiple_result(
                product_name=product_name,
                candidates=[c for c, _ in auto_hits],
                engine_path=engine_path,
                ranked_pairs=auto_hits,
                details={
                    "reason": "multiple_embedding_matches",
                    "distances": [round(d, 6) for _, d in auto_hits],
                },
            )
        if len(auto_hits) == 1:
            candidate, dist = auto_hits[0]
            product = candidate.product
            if product is not None:
                logger.info(
                    "[SALES MATCH] embedding -> product_id=%s distance=%.4f",
                    product.product_id,
                    dist,
                )
                return _build_confirmed_result(
                    product=product,
                    product_name=product_name,
                    match_type=MATCH_TYPE_EMBEDDING,
                    engine_path=engine_path,
                    details={
                        "productId": product.product_id,
                        "embeddingDistance": round(dist, 6),
                    },
                )

        if len(suggest_hits) == 1:
            candidate, dist = suggest_hits[0]
            engine_path.append("embedding_suggest_single")
            return _build_multiple_result(
                product_name=product_name,
                candidates=[candidate],
                engine_path=engine_path,
                ranked_pairs=[(candidate, dist)],
                details={
                    "reason": "single_embedding_suggestion",
                    "embeddingDistance": round(dist, 6),
                },
            )
        if len(suggest_hits) > 1:
            engine_path.append(f"embedding_suggest_multiple:{len(suggest_hits)}")
            return _build_multiple_result(
                product_name=product_name,
                candidates=[c for c, _ in suggest_hits],
                engine_path=engine_path,
                ranked_pairs=suggest_hits,
                details={
                    "reason": "multiple_embedding_suggestions",
                    "distances": [round(d, 6) for _, d in suggest_hits],
                },
            )

        logger.info("[SALES MATCH] no_match product_name=%r", product_name)
        return _build_no_match_result(
            product_name=product_name,
            engine_path=engine_path,
        )


def match_sales_product_name(
    db: Session,
    *,
    product_name: str,
    category: Optional[str] = None,
    candidates: Optional[Sequence[ProductCatalogCandidate]] = None,
) -> SalesProductMatchResult:
    """Run product matching for one sales line."""
    pool = (
        list(candidates)
        if candidates is not None
        else build_product_catalog_candidates(db)
    )
    engine = SalesProductMatchingEngine(candidates=pool)
    return engine.run_for_line(
        db=db,
        product_name=product_name,
        category=category,
    )
