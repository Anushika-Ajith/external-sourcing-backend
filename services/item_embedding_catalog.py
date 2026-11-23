"""
Catalog item_embeddings (VECTOR_DB):

- Written when rate contracts sync vendor items
- Queried during stock upload when product_code / product_id is missing, or when
  product_code is not found on vendor_items
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from database import VectorSessionLocal
from models import ItemEmbedding, Vendor, VendorItem
from services.embedding_service import embed_text
from services.item_fingerprint import (
    build_fingerprint_from_line_data,
    enrich_fingerprint_from_vendor_item,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_DISTANCE = 0.45
DEFAULT_MATCH_LIMIT = 5


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _clip(value: Optional[str], max_len: int) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    return text if len(text) <= max_len else text[:max_len]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_vendor_code(vendor: Optional[Vendor], vendor_id: Optional[int] = None) -> str:
    if vendor is not None:
        from services.vendor_codes import ensure_vendor_code_from_registration

        synced = ensure_vendor_code_from_registration(vendor)
        if synced:
            return _clip(synced, 100)
        for candidate in (
            getattr(vendor, "company_code", None),
            getattr(vendor, "company_name", None),
        ):
            code = _clean(candidate)
            if code:
                return _clip(code, 100)
        if vendor.vendor_id is not None:
            return f"V{vendor.vendor_id}"
    if vendor_id is not None:
        return f"V{vendor_id}"
    return "UNKNOWN"


def resolve_item_code(vendor_item: VendorItem) -> str:
    for candidate in (
        vendor_item.erp_code,
        vendor_item.vendor_product_code,
        vendor_item.barcode,
        str(vendor_item.item_id) if vendor_item.item_id is not None else None,
    ):
        code = _clean(candidate)
        if code:
            return _clip(code, 100)
    # Last resort when item_id is unset (new rows set item_id = vendor_items.id).
    if vendor_item.id is not None:
        return f"VI-{vendor_item.id}"
    name = _clean(vendor_item.item_name) or "product"
    return _clip(name, 100)


def resolve_vendor_item_product_name(vendor_item: VendorItem) -> Optional[str]:
    """Prefer product_name; fall back to item_name / codes for embedding input."""
    return (
        _clean(vendor_item.product_name)
        or _clean(vendor_item.item_name)
        or resolve_item_code(vendor_item)
    )


def build_vendor_item_embedding_text(
    vendor_item: VendorItem,
    fingerprint: Optional[Dict[str, Any]] = None,
) -> str:
    """Rate-contract catalog embeddings use product_name only.

    ``fingerprint`` is accepted for call-site compatibility; embeddings intentionally
    ignore fingerprint fields so catalog vectors stay name-based.
    """
    _ = fingerprint
    return resolve_vendor_item_product_name(vendor_item) or ""


def sync_vendor_item_to_item_embeddings(
    vendor_item: VendorItem,
    *,
    vendor: Optional[Vendor] = None,
    product_fingerprint: Optional[Dict[str, Any]] = None,
) -> Optional[ItemEmbedding]:
    """
    Upsert public.item_embeddings for a rate-contract vendor item.
    Does not raise — logs and returns None on failure.
    """
    if vendor_item.id is None:
        return None

    fp_source = product_fingerprint
    if not isinstance(fp_source, dict) or not fp_source:
        fp_source = (
            vendor_item.product_fingerprint
            if isinstance(vendor_item.product_fingerprint, dict)
            else None
        )

    description = (
        _clean(vendor_item.description)
        or _clean(vendor_item.product_name)
        or _clean(vendor_item.item_name)
    )
    if not fp_source:
        fp_source = build_fingerprint_from_line_data(
            description=description,
            uom=_clean(vendor_item.uom),
            item_no=_clean(vendor_item.erp_code) or _clean(vendor_item.vendor_product_code),
            vendor_item=vendor_item,
            use_gemini=True,
        )

    # Create/normalize fingerprint (unchanged); embedding uses product_name only.
    fingerprint = enrich_fingerprint_from_vendor_item(fp_source, vendor_item)
    if not fingerprint:
        logger.info(
            "Skipping item_embeddings for vendor_item_id=%s — empty fingerprint",
            vendor_item.id,
        )
        return None

    embedding_text = build_vendor_item_embedding_text(vendor_item, fingerprint)
    if not embedding_text.strip():
        return None

    vector = None
    try:
        vector = embed_text(embedding_text)
    except Exception as exc:
        logger.warning(
            "Embedding failed for vendor_item_id=%s: %s",
            vendor_item.id,
            exc,
        )

    item_code = resolve_item_code(vendor_item)
    vendor_code = resolve_vendor_code(vendor, vendor_item.vendor_id)
    item_name = _clip(
        _clean(vendor_item.product_name)
        or _clean(vendor_item.item_name)
        or item_code,
        255,
    )
    item_description = _clean(vendor_item.description) or embedding_text

    vector_db = VectorSessionLocal()
    try:
        existing = (
            vector_db.query(ItemEmbedding)
            .filter(
                ItemEmbedding.item_code == item_code,
                ItemEmbedding.vendor_code == vendor_code,
            )
            .order_by(ItemEmbedding.id.desc())
            .first()
        )
        if existing is not None:
            existing.item_name = item_name
            existing.item_description = item_description
            existing.embedding_text = embedding_text
            if vector is not None:
                existing.embedding = vector
            existing.updated_at = _utcnow()
            vector_db.commit()
            vector_db.refresh(existing)
            return existing

        row = ItemEmbedding(
            item_code=item_code,
            item_name=item_name,
            vendor_code=vendor_code,
            item_description=item_description,
            embedding_text=embedding_text,
            embedding=vector,
        )
        vector_db.add(row)
        vector_db.commit()
        vector_db.refresh(row)
        logger.info(
            "Inserted item_embeddings id=%s item_code=%s vendor_code=%s",
            row.id,
            item_code,
            vendor_code,
        )
        return row
    except Exception as exc:
        vector_db.rollback()
        logger.warning(
            "item_embeddings sync failed for vendor_item_id=%s: %s",
            vendor_item.id,
            exc,
        )
        return None
    finally:
        vector_db.close()


def delete_vendor_item_from_item_embeddings(
    vendor_item: VendorItem,
    *,
    vendor: Optional[Vendor] = None,
) -> int:
    """
    Remove catalog embedding rows for a deleted vendor item.
    Matches the same item_code + vendor_code keys used on upsert.
    Returns the number of rows deleted (0 on failure or no match).
    """
    if vendor_item.id is None:
        return 0

    item_code = resolve_item_code(vendor_item)
    vendor_code = resolve_vendor_code(vendor, vendor_item.vendor_id)
    fallback_code = f"VI-{vendor_item.id}"

    vector_db = VectorSessionLocal()
    try:
        q = vector_db.query(ItemEmbedding).filter(
            ItemEmbedding.vendor_code == vendor_code,
            ItemEmbedding.item_code.in_(
                [code for code in (item_code, fallback_code) if code]
            ),
        )
        deleted = q.delete(synchronize_session=False)
        if deleted:
            vector_db.commit()
            logger.info(
                "Deleted %s item_embeddings row(s) vendor_item_id=%s item_code=%s vendor_code=%s",
                deleted,
                vendor_item.id,
                item_code,
                vendor_code,
            )
        return deleted
    except Exception as exc:
        vector_db.rollback()
        logger.warning(
            "item_embeddings delete failed for vendor_item_id=%s: %s",
            vendor_item.id,
            exc,
        )
        return 0
    finally:
        vector_db.close()


@dataclass
class ItemEmbeddingMatch:
    item_code: str
    item_name: str
    vendor_code: str
    item_description: str
    embedding_text: Optional[str]
    distance: float
    item_embedding_id: int


def search_item_embeddings(
    query_vector: List[float],
    *,
    vendor_code: Optional[str] = None,
    limit: int = DEFAULT_MATCH_LIMIT,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> List[ItemEmbeddingMatch]:
    """Nearest-neighbor search on VECTOR_DB.item_embeddings."""
    distance = ItemEmbedding.embedding.cosine_distance(query_vector).label("distance")
    vector_db = VectorSessionLocal()
    try:
        q = vector_db.query(ItemEmbedding, distance).filter(
            ItemEmbedding.embedding.isnot(None)
        )
        if vendor_code and vendor_code.strip():
            q = q.filter(ItemEmbedding.vendor_code == vendor_code.strip())

        rows = q.order_by(distance).limit(max(1, limit)).all()
        matches: List[ItemEmbeddingMatch] = []
        for row, dist in rows:
            d = float(dist)
            if d > max_distance:
                continue
            matches.append(
                ItemEmbeddingMatch(
                    item_code=row.item_code,
                    item_name=row.item_name,
                    vendor_code=row.vendor_code,
                    item_description=row.item_description or "",
                    embedding_text=row.embedding_text,
                    distance=d,
                    item_embedding_id=int(row.id),
                )
            )
        return matches
    finally:
        vector_db.close()


def embed_stock_line_for_match(
    *,
    product_name: Optional[str],
    brand: Optional[str] = None,
    category: Optional[str] = None,
    unit: Optional[str] = None,
    barcode: Optional[str] = None,
    use_gemini: bool = True,
) -> Tuple[List[float], str]:
    """Embed a stock line for catalog match using product_name only.

    Catalog vectors from rate contracts are name-based, so the query embedding
    must match that space. Extra kwargs are accepted for call-site compatibility.
    """
    _ = (brand, category, unit, barcode, use_gemini)
    text = _clean(product_name) or ""
    if not text:
        raise ValueError("Cannot embed stock line without product name")
    return embed_text(text), text


def match_stock_line_to_catalog(
    line: Dict[str, Any],
    *,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    vendor_code: Optional[str] = None,
) -> Optional[ItemEmbeddingMatch]:
    """
    Resolve a stock line against item_embeddings by product_name similarity.
    Returns the best match within max_distance, or None.
    """
    try:
        vector, _ = embed_stock_line_for_match(
            product_name=line.get("product_name"),
            brand=line.get("brand"),
            category=line.get("category"),
            unit=line.get("unit"),
            barcode=line.get("barcode"),
            use_gemini=True,
        )
    except Exception as exc:
        logger.warning("Stock line embed for catalog match failed: %s", exc)
        return None

    preferred = vendor_code or _clean(line.get("preferred_supplier"))
    matches = search_item_embeddings(
        vector,
        vendor_code=preferred,
        limit=DEFAULT_MATCH_LIMIT,
        max_distance=max_distance,
    )
    # If supplier filter yielded nothing, search the full catalog.
    if not matches and preferred:
        matches = search_item_embeddings(
            vector,
            vendor_code=None,
            limit=DEFAULT_MATCH_LIMIT,
            max_distance=max_distance,
        )
    return matches[0] if matches else None


def match_product_name_embedding(
    product_name: str,
    *,
    vendor_code: Optional[str] = None,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    limit: int = DEFAULT_MATCH_LIMIT,
) -> List[ItemEmbeddingMatch]:
    """Nearest catalog matches for a product_name embedding (rate-contract upsert)."""
    text = _clean(product_name)
    if not text:
        return []
    try:
        vector = embed_text(text)
    except Exception as exc:
        logger.warning("product_name embed for match failed: %s", exc)
        return []
    return search_item_embeddings(
        vector,
        vendor_code=vendor_code,
        limit=limit,
        max_distance=max_distance,
    )
