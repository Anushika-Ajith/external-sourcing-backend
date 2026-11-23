"""Sales matching helpers: vendor_items → Foundation product → local products mirror."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import Product, VendorItem
from services.foundation_product import list_foundation_products
from services.product_matching_common import clean_text, names_are_close_spelling_variant, normalize_product_name

logger = logging.getLogger(__name__)

_FOUNDATION_CODE_KEYS = ("productCode", "product_code", "company_code", "companyCode")
_FOUNDATION_ID_KEYS = ("productId", "product_id")
_FOUNDATION_NAME_KEYS = ("name", "productName", "product_name")


def _foundation_field(row: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = clean_text(row.get(key))
        if value:
            return value
    return None


def index_foundation_products(
    rows: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Index Foundation products by productCode and normalized display name."""
    by_code: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = _foundation_field(row, _FOUNDATION_CODE_KEYS)
        if code:
            by_code[code.upper()] = row
        name = _foundation_field(row, _FOUNDATION_NAME_KEYS)
        if name:
            norm = normalize_product_name(name)
            if norm:
                by_name.setdefault(norm, row)
    return by_code, by_name


def ensure_local_product_for_sales(db: Session, foundation_row: Dict[str, Any]) -> Optional[Product]:
    """
    Ensure a local ``products`` row exists for a Foundation product id.

    This mirrors an existing Foundation master row into the external-sourcing DB
    so ``sales_transactions.product_id`` FK can be satisfied. It does not create
    Foundation product master entries.
    """
    raw_id = foundation_row.get("productId")
    if raw_id is None:
        raw_id = foundation_row.get("product_id")
    if raw_id is None:
        return None
    product_id = int(raw_id)
    name = _foundation_field(foundation_row, _FOUNDATION_NAME_KEYS) or f"Product {product_id}"
    category = clean_text(foundation_row.get("category"))

    existing = db.query(Product).filter(Product.product_id == product_id).first()
    if existing is not None:
        return existing

    product = Product(
        product_id=product_id,
        name=name[:100],
        category=category[:50] if category else None,
        is_active=bool(foundation_row.get("isActive", foundation_row.get("is_active", True))),
    )
    db.add(product)
    db.flush()
    logger.info(
        "[SALES MATCH] mirrored Foundation product_id=%s name=%r into local products",
        product_id,
        name,
    )
    return product


def _candidate_from_product(product: Product):
    from services.sales_product_matching_engine import ProductCatalogCandidate

    name = clean_text(product.name) or f"Product {product.product_id}"
    return ProductCatalogCandidate(
        product_id=int(product.product_id),
        name=name,
        category=clean_text(product.category),
        aliases=[name],
        product=product,
    )


def build_sales_match_candidates(db: Session):
    """
    Build the sales match pool from local products plus Foundation master products.

    Foundation rows are mirrored locally (by ``product_id``) when missing.
    """
    from services.sales_product_matching_engine import build_product_catalog_candidates

    candidates = build_product_catalog_candidates(db)
    seen_ids = {c.product_id for c in candidates}

    foundation_rows = list_foundation_products()
    by_code, by_name = index_foundation_products(foundation_rows)
    for row in foundation_rows:
        product = ensure_local_product_for_sales(db, row)
        if product is None:
            continue
        pid = int(product.product_id)
        if pid in seen_ids:
            continue
        candidates.append(_candidate_from_product(product))
        seen_ids.add(pid)

    return candidates, by_code, by_name


def _vendor_item_name_variants(item: VendorItem) -> List[str]:
    names: List[str] = []
    for value in (item.product_name, item.item_name, item.description):
        text = clean_text(value)
        if text:
            names.append(text)
    fp = item.product_fingerprint if isinstance(item.product_fingerprint, dict) else {}
    for value in (fp.get("generic"), fp.get("normalized_name")):
        text = clean_text(value)
        if text:
            names.append(text)
    return names


def _vendor_item_rank(item: VendorItem) -> Tuple[int, int]:
    status = (item.status or "").strip().upper()
    status_rank = 0 if status == "CONFIRMED" else 1 if status in {"ACTIVE", "PENDING"} else 2
    return (status_rank, -int(item.id or 0))


def find_vendor_items_for_sales_line(
    db: Session,
    *,
    product_code: Optional[str],
    product_name: str,
) -> List[VendorItem]:
    """Match vendor catalog rows by ERP/product code, then by product name."""
    hits: List[VendorItem] = []
    seen_ids: set[int] = set()

    code = clean_text(product_code)
    if code:
        for item in (
            db.query(VendorItem)
            .filter(func.upper(VendorItem.erp_code) == code.upper())
            .order_by(VendorItem.id.desc())
            .all()
        ):
            if item.id not in seen_ids:
                hits.append(item)
                seen_ids.add(int(item.id))

    want = normalize_product_name(product_name)
    if want:
        if not hits:
            name_rows = (
                db.query(VendorItem)
                .filter(
                    or_(
                        func.lower(VendorItem.product_name) == product_name.strip().lower(),
                        func.lower(VendorItem.item_name) == product_name.strip().lower(),
                    )
                )
                .order_by(VendorItem.id.desc())
                .all()
            )
            for item in name_rows:
                if item.id not in seen_ids:
                    hits.append(item)
                    seen_ids.add(int(item.id))

        if not hits:
            for item in db.query(VendorItem).order_by(VendorItem.id.desc()).all():
                if item.id in seen_ids:
                    continue
                if any(
                    names_are_close_spelling_variant(product_name, variant)
                    for variant in _vendor_item_name_variants(item)
                ):
                    hits.append(item)
                    seen_ids.add(int(item.id))

    hits.sort(key=_vendor_item_rank)
    return hits


def resolve_foundation_product_for_vendor_item(
    vendor_item: VendorItem,
    *,
    foundation_by_code: Dict[str, Dict[str, Any]],
    foundation_by_name: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for code in (vendor_item.company_code, vendor_item.erp_code, vendor_item.vendor_product_code):
        text = clean_text(code)
        if text and text.upper() in foundation_by_code:
            return foundation_by_code[text.upper()]

    for variant in _vendor_item_name_variants(vendor_item):
        norm = normalize_product_name(variant)
        if norm and norm in foundation_by_name:
            return foundation_by_name[norm]
    return None


def resolve_sales_line_via_vendor_catalog(
    db: Session,
    *,
    product_code: Optional[str],
    product_name: str,
    pool: Sequence[Any],
    foundation_by_code: Dict[str, Dict[str, Any]],
    foundation_by_name: Dict[str, Dict[str, Any]],
) -> Optional[Tuple[Any, VendorItem, Dict[str, Any]]]:
    """
    Resolve a sales line through vendor_items → Foundation product → local pool.
    """
    vendor_hits = find_vendor_items_for_sales_line(
        db,
        product_code=product_code,
        product_name=product_name,
    )
    if not vendor_hits:
        return None

    resolved: List[Tuple[Any, VendorItem, Dict[str, Any]]] = []
    for vendor_item in vendor_hits:
        foundation_row = resolve_foundation_product_for_vendor_item(
            vendor_item,
            foundation_by_code=foundation_by_code,
            foundation_by_name=foundation_by_name,
        )
        if foundation_row is None:
            continue
        product = ensure_local_product_for_sales(db, foundation_row)
        if product is None:
            continue
        candidate = next((c for c in pool if c.product_id == int(product.product_id)), None)
        if candidate is None:
            candidate = _candidate_from_product(product)
        resolved.append((candidate, vendor_item, foundation_row))

    if not resolved:
        return None
    if len(resolved) > 1:
        # Prefer a single unambiguous vendor/Foundation hit; caller handles multiples.
        unique_ids = {c.product_id for c, _, _ in resolved}
        if len(unique_ids) > 1:
            return None
    return resolved[0]


def collect_vendor_foundation_matches(
    db: Session,
    *,
    product_code: Optional[str],
    product_name: str,
    pool: Sequence[Any],
    foundation_by_code: Dict[str, Dict[str, Any]],
    foundation_by_name: Dict[str, Dict[str, Any]],
) -> List[Any]:
    """Return all vendor→Foundation→local candidates for a sales line."""
    from services.sales_product_matching_engine import ProductCatalogCandidate

    vendor_hits = find_vendor_items_for_sales_line(
        db,
        product_code=product_code,
        product_name=product_name,
    )
    candidates: List[ProductCatalogCandidate] = []
    seen_ids: set[int] = set()
    for vendor_item in vendor_hits:
        foundation_row = resolve_foundation_product_for_vendor_item(
            vendor_item,
            foundation_by_code=foundation_by_code,
            foundation_by_name=foundation_by_name,
        )
        if foundation_row is None:
            continue
        product = ensure_local_product_for_sales(db, foundation_row)
        if product is None:
            continue
        pid = int(product.product_id)
        if pid in seen_ids:
            continue
        candidate = next((c for c in pool if c.product_id == pid), None)
        if candidate is None:
            candidate = _candidate_from_product(product)
        candidates.append(candidate)
        seen_ids.add(pid)
    return candidates
