"""
Resolve shared internal product identity (erp_code + company_code) across vendors.

Many vendors may carry the same Foundation/ERP product under different
vendor_product_code values. New vendor catalog rows should reuse the existing
internal codes instead of creating duplicate master products.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import VendorItem
from services.foundation_product import list_foundation_products
from services.product_matching_common import (
    clean_text,
    names_are_close_spelling_variant,
    normalize_product_name,
)
from services.sales_foundation_catalog import (
    find_vendor_items_for_sales_line,
    index_foundation_products,
)

logger = logging.getLogger(__name__)

_FOUNDATION_CODE_KEYS = ("productCode", "product_code", "company_code", "companyCode")
_FOUNDATION_NAME_KEYS = ("name", "productName", "product_name")


def _foundation_field(row: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        value = clean_text(row.get(key))
        if value:
            return value
    return None


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


def _names_refer_to_same_product(ocr_name: str, catalog_name: str) -> bool:
    want = normalize_product_name(ocr_name)
    have = normalize_product_name(catalog_name)
    if not want or not have:
        return False
    if want == have:
        return True
    if have.startswith(want + " ") or want.startswith(have + " "):
        return True
    return names_are_close_spelling_variant(ocr_name, catalog_name)


def _item_matches_product_name(item: VendorItem, product_name: str) -> bool:
    for variant in _vendor_item_name_variants(item):
        if _names_refer_to_same_product(product_name, variant):
            return True
    return False


def _item_matches_fingerprint_name(
    item: VendorItem,
    fingerprint: Optional[Dict[str, Any]],
) -> bool:
    if not isinstance(fingerprint, dict):
        return False
    for key in ("normalized_name", "generic"):
        ocr_name = clean_text(fingerprint.get(key))
        if not ocr_name:
            continue
        for variant in _vendor_item_name_variants(item):
            if _names_refer_to_same_product(ocr_name, variant):
                return True
    return False


def _dedupe_internal_identities(
    items: Sequence[VendorItem],
) -> Dict[Tuple[str, str], VendorItem]:
    out: Dict[Tuple[str, str], VendorItem] = {}
    for item in items:
        erp = clean_text(item.erp_code)
        company = clean_text(item.company_code)
        if not erp or not company:
            continue
        key = (erp.upper(), company.upper())
        if key not in out:
            out[key] = item
    return out


def _lookup_by_internal_codes(
    db: Session,
    *,
    erp_code: Optional[str],
    company_code: Optional[str],
) -> Optional[Dict[str, str]]:
    erp = clean_text(erp_code)
    company = clean_text(company_code)
    if erp:
        hit = (
            db.query(VendorItem)
            .filter(
                func.upper(func.trim(VendorItem.erp_code)) == erp.upper(),
                VendorItem.company_code.isnot(None),
                func.trim(VendorItem.company_code) != "",
            )
            .order_by(VendorItem.id.desc())
            .first()
        )
        if hit and hit.erp_code and hit.company_code:
            return {
                "erp_code": hit.erp_code.strip(),
                "company_code": hit.company_code.strip(),
            }
    if company:
        hit = (
            db.query(VendorItem)
            .filter(
                func.upper(func.trim(VendorItem.company_code)) == company.upper(),
                VendorItem.erp_code.isnot(None),
                func.trim(VendorItem.erp_code) != "",
            )
            .order_by(VendorItem.id.desc())
            .first()
        )
        if hit and hit.erp_code and hit.company_code:
            return {
                "erp_code": hit.erp_code.strip(),
                "company_code": hit.company_code.strip(),
            }
    return None


def _lookup_by_foundation_catalog(product_name: str) -> Optional[Dict[str, str]]:
    try:
        foundation_rows = list_foundation_products()
    except Exception as exc:
        logger.warning("Foundation product list unavailable: %s", exc)
        return None
    if not foundation_rows:
        return None

    _, by_name = index_foundation_products(foundation_rows)
    norm = normalize_product_name(product_name)
    row = by_name.get(norm) if norm else None
    if row is None:
        return None

    company_code = _foundation_field(row, _FOUNDATION_CODE_KEYS)
    if not company_code:
        return None
    return {"company_code": company_code}


def resolve_shared_internal_product_codes(
    db: Session,
    *,
    product_name: str,
    product_code: Optional[str] = None,
    erp_code: Optional[str] = None,
    company_code: Optional[str] = None,
    fingerprint: Optional[Dict[str, Any]] = None,
    exclude_vendor_item_ids: Optional[Set[int]] = None,
) -> Optional[Dict[str, str]]:
    """
    Find an existing shared ``erp_code`` + ``company_code`` pair for a product.

    Searches across all vendors. Returns None when zero or ambiguous (>1)
    internal identities match.
    """
    exclude_vendor_item_ids = exclude_vendor_item_ids or set()

    by_codes = _lookup_by_internal_codes(
        db,
        erp_code=erp_code or product_code,
        company_code=company_code,
    )
    if by_codes and by_codes.get("erp_code") and by_codes.get("company_code"):
        return by_codes

    matched_items: List[VendorItem] = []

    sales_hits = find_vendor_items_for_sales_line(
        db,
        product_code=product_code or erp_code,
        product_name=product_name,
    )
    for item in sales_hits:
        if item.id in exclude_vendor_item_ids:
            continue
        if clean_text(item.erp_code) and clean_text(item.company_code):
            matched_items.append(item)

    if not matched_items:
        q = db.query(VendorItem).filter(
            VendorItem.erp_code.isnot(None),
            func.trim(VendorItem.erp_code) != "",
            VendorItem.company_code.isnot(None),
            func.trim(VendorItem.company_code) != "",
        )
        if exclude_vendor_item_ids:
            q = q.filter(VendorItem.id.notin_(exclude_vendor_item_ids))
        for item in q.order_by(VendorItem.id.desc()).all():
            if _item_matches_product_name(item, product_name):
                matched_items.append(item)
                continue
            if _item_matches_fingerprint_name(item, fingerprint):
                matched_items.append(item)

    identities = _dedupe_internal_identities(matched_items)
    if len(identities) == 1:
        item = next(iter(identities.values()))
        logger.info(
            "[INTERNAL PRODUCT] reuse erp=%s company=%s from vendor_item_id=%s "
            "for product_name=%r",
            item.erp_code,
            item.company_code,
            item.id,
            product_name,
        )
        return {
            "erp_code": item.erp_code.strip(),
            "company_code": item.company_code.strip(),
        }
    if len(identities) > 1:
        logger.info(
            "[INTERNAL PRODUCT] ambiguous internal identity for product_name=%r "
            "(%s distinct erp/company pairs)",
            product_name,
            len(identities),
        )
        return None

    foundation_hit = _lookup_by_foundation_catalog(product_name)
    if foundation_hit and foundation_hit.get("company_code"):
        by_company = _lookup_by_internal_codes(
            db,
            erp_code=None,
            company_code=foundation_hit["company_code"],
        )
        if by_company:
            return by_company

    return None


def _find_vendor_items_matching_code(db: Session, code: str) -> List[VendorItem]:
    """Match vendor_items by erp_code, company_code, or vendor_product_code."""
    text = clean_text(code)
    if not text:
        return []
    upper = text.upper()
    return (
        db.query(VendorItem)
        .filter(
            VendorItem.vendor_id.isnot(None),
            or_(
                func.upper(func.trim(VendorItem.erp_code)) == upper,
                func.upper(func.trim(VendorItem.company_code)) == upper,
                func.upper(func.trim(VendorItem.vendor_product_code)) == upper,
            ),
        )
        .order_by(VendorItem.id.desc())
        .all()
    )


def _collect_internal_identity_tokens(
    items: Sequence[VendorItem],
) -> Tuple[Set[str], Set[str]]:
    erp_codes: Set[str] = set()
    company_codes: Set[str] = set()
    for item in items:
        erp = clean_text(item.erp_code)
        company = clean_text(item.company_code)
        if erp:
            erp_codes.add(erp.upper())
        if company:
            company_codes.add(company.upper())
    return erp_codes, company_codes


def _collect_internal_identity_pairs(
    items: Sequence[VendorItem],
) -> Set[Tuple[str, str]]:
    """(erp_code, company_code) pairs present on items that have both codes."""
    pairs: Set[Tuple[str, str]] = set()
    for item in items:
        erp = clean_text(item.erp_code)
        company = clean_text(item.company_code)
        if erp and company:
            pairs.add((erp.upper(), company.upper()))
    return pairs


def _find_vendor_items_by_internal_identity(
    db: Session,
    *,
    erp_codes: Set[str],
    company_codes: Set[str],
    identity_pairs: Optional[Set[Tuple[str, str]]] = None,
) -> List[VendorItem]:
    """
    Vendor items that share the same internal product identity.

    Prefer exact (erp_code, company_code) pairs when available so a shared ERP
    code alone does not pull unrelated products from other companies. Fall back
    to matching either code only when no pairs are known.
    """
    pairs = identity_pairs or set()
    if pairs:
        filters = [
            (
                (func.upper(func.trim(VendorItem.erp_code)) == erp)
                & (func.upper(func.trim(VendorItem.company_code)) == company)
            )
            for erp, company in pairs
        ]
        return (
            db.query(VendorItem)
            .filter(VendorItem.vendor_id.isnot(None), or_(*filters))
            .order_by(VendorItem.id.desc())
            .all()
        )

    if not erp_codes and not company_codes:
        return []
    filters = []
    if erp_codes:
        filters.append(func.upper(func.trim(VendorItem.erp_code)).in_(erp_codes))
    if company_codes:
        filters.append(func.upper(func.trim(VendorItem.company_code)).in_(company_codes))
    return (
        db.query(VendorItem)
        .filter(VendorItem.vendor_id.isnot(None), or_(*filters))
        .order_by(VendorItem.id.desc())
        .all()
    )


def _find_vendor_items_by_item_ids(
    db: Session,
    item_ids: Set[int],
) -> List[VendorItem]:
    if not item_ids:
        return []
    return (
        db.query(VendorItem)
        .filter(
            VendorItem.vendor_id.isnot(None),
            VendorItem.item_id.in_(item_ids),
        )
        .order_by(VendorItem.id.desc())
        .all()
    )


def _find_vendor_items_by_exact_product_names(
    db: Session,
    names: Sequence[str],
) -> List[VendorItem]:
    """
    All vendor_items whose product_name / item_name equals any of the names
    (case-insensitive). No row cap — used so stock vendor pickers list every
    vendor carrying the same labeled product, even when ERP codes differ.
    """
    normalized: Set[str] = set()
    for raw in names:
        text = clean_text(raw)
        if not text or text == "—":
            continue
        normalized.add(text.lower())
    if not normalized:
        return []

    return (
        db.query(VendorItem)
        .filter(
            VendorItem.vendor_id.isnot(None),
            or_(
                func.lower(func.trim(VendorItem.product_name)).in_(normalized),
                func.lower(func.trim(VendorItem.item_name)).in_(normalized),
            ),
        )
        .order_by(VendorItem.id.desc())
        .all()
    )


def _expand_vendor_items_by_product_names(
    db: Session,
    names: Sequence[str],
) -> List[VendorItem]:
    """Cross-vendor vendor_items lookup by product display name (fuzzy + prefix)."""
    hits: List[VendorItem] = []
    seen_ids: Set[int] = set()
    for raw_name in names:
        name = clean_text(raw_name)
        if not name or name == "—":
            continue
        for item in find_vendor_items_for_sales_line(
            db,
            product_code=None,
            product_name=name,
        ):
            if item.id is None or item.id in seen_ids:
                continue
            seen_ids.add(int(item.id))
            hits.append(item)

        # Prefix / contains SQL fallback for names embedding search may miss.
        # No hard cap: stock vendor pickers must see every sibling vendor.
        norm = normalize_product_name(name)
        if not norm:
            continue
        token = norm.split()[0] if norm.split() else norm
        if len(token) < 3:
            continue
        like = f"{token}%"
        for item in (
            db.query(VendorItem)
            .filter(
                VendorItem.vendor_id.isnot(None),
                or_(
                    func.lower(VendorItem.product_name).like(like),
                    func.lower(VendorItem.item_name).like(like),
                ),
            )
            .order_by(VendorItem.id.desc())
            .all()
        ):
            if item.id is None or item.id in seen_ids:
                continue
            if not _item_matches_product_name(item, name):
                continue
            seen_ids.add(int(item.id))
            hits.append(item)
    return hits


def _merge_vendor_items(*groups: Sequence[VendorItem]) -> List[VendorItem]:
    merged: List[VendorItem] = []
    seen_ids: Set[int] = set()
    for group in groups:
        for item in group:
            if item.id is None or item.id in seen_ids:
                continue
            seen_ids.add(int(item.id))
            merged.append(item)
    return merged


def _collect_names_for_vendor_list(
    *,
    code: str,
    code_to_product_name: Dict[str, str],
    seed_items: Sequence[VendorItem],
) -> List[str]:
    names: List[str] = []
    stock_name = clean_text(code_to_product_name.get(code))
    if stock_name:
        names.append(stock_name)
    for item in seed_items:
        for attr in ("product_name", "item_name"):
            val = clean_text(getattr(item, attr, None))
            if val:
                names.append(val)
    return names


def list_vendors_for_stock_product_codes(
    db: Session,
    product_codes: Sequence[str],
    *,
    code_to_product_name: Optional[Dict[str, str]] = None,
) -> Dict[str, List[int]]:
    """
    For each stock.product_code, return vendor_ids for every vendor that carries
    the same product — not only the vendor whose vendor_product_code equals the
    stock code.

    Expansion order:
      1. Seed by erp_code / company_code / vendor_product_code
      2. Exact product_name / item_name matches (stock name + seed names)
      3. Shared erp_code / company_code from all of the above
      4. Shared buyer item_id
      5. Fuzzy / prefix name matches
    """
    normalized: List[str] = []
    seen_upper: Set[str] = set()
    for raw in product_codes:
        code = clean_text(raw)
        if not code or code == "—":
            continue
        key = code.upper()
        if key in seen_upper:
            continue
        seen_upper.add(key)
        normalized.append(code)

    result: Dict[str, List[int]] = {code: [] for code in normalized}
    if not normalized:
        return result

    code_to_product_name = code_to_product_name or {}

    for code in normalized:
        seed_items = _find_vendor_items_matching_code(db, code)
        names = _collect_names_for_vendor_list(
            code=code,
            code_to_product_name=code_to_product_name,
            seed_items=seed_items,
        )

        # Exact name hits first so siblings without shared ERP codes still appear.
        exact_name_items = _find_vendor_items_by_exact_product_names(db, names)
        seeded_pool = _merge_vendor_items(seed_items, exact_name_items)

        erp_codes, company_codes = _collect_internal_identity_tokens(seeded_pool)
        identity_pairs = _collect_internal_identity_pairs(seeded_pool)
        identity_items: List[VendorItem] = []
        if identity_pairs or erp_codes or company_codes:
            identity_items = _find_vendor_items_by_internal_identity(
                db,
                erp_codes=erp_codes,
                company_codes=company_codes,
                identity_pairs=identity_pairs,
            )

        item_ids: Set[int] = {
            int(item.item_id)
            for item in _merge_vendor_items(seeded_pool, identity_items)
            if item.item_id is not None
        }
        item_id_items = _find_vendor_items_by_item_ids(db, item_ids)

        # Include names discovered via identity / item_id for fuzzy expand.
        for item in _merge_vendor_items(identity_items, item_id_items):
            for attr in ("product_name", "item_name"):
                val = clean_text(getattr(item, attr, None))
                if val:
                    names.append(val)

        name_items = _expand_vendor_items_by_product_names(db, names)
        pool_items = _merge_vendor_items(
            seeded_pool,
            identity_items,
            item_id_items,
            name_items,
        )

        # Second identity pass: pairs from fuzzy matches may unlock more siblings.
        more_pairs = _collect_internal_identity_pairs(pool_items)
        if more_pairs - identity_pairs:
            more_erp, more_company = _collect_internal_identity_tokens(pool_items)
            extra_identity = _find_vendor_items_by_internal_identity(
                db,
                erp_codes=more_erp,
                company_codes=more_company,
                identity_pairs=more_pairs,
            )
            pool_items = _merge_vendor_items(pool_items, extra_identity)

        vendor_ids: List[int] = []
        seen_vendors: Set[int] = set()
        for item in pool_items:
            if item.vendor_id is None or item.vendor_id in seen_vendors:
                continue
            seen_vendors.add(int(item.vendor_id))
            vendor_ids.append(int(item.vendor_id))
        result[code] = vendor_ids
        stock_name = clean_text(code_to_product_name.get(code))
        logger.info(
            "[STOCK VENDOR LIST] code=%r stock_name=%r vendors=%s "
            "(seed=%s exact_name=%s identity=%s item_id=%s name=%s)",
            code,
            stock_name,
            vendor_ids,
            len(seed_items),
            len(exact_name_items),
            len(identity_items),
            len(item_id_items),
            len(name_items),
        )

    return result

