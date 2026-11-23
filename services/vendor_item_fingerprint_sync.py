"""Sync vendor_item_fingerprint rows from OCR product fingerprints (rate contracts)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from config import settings
from database import VectorSessionLocal
from models import VendorItem, VendorItemFingerprint
from services.embedding_service import embed_text
from services.item_fingerprint import (
    build_fingerprint_from_line_data,
    enrich_fingerprint_from_vendor_item,
)

logger = logging.getLogger(__name__)


def sync_vendor_item_fingerprint(
    db: Session,
    vendor_item: VendorItem,
    product_fingerprint: Optional[Dict[str, Any]] = None,
) -> Optional[VendorItemFingerprint]:
    """
    Upsert vendor_item_fingerprint for a vendor item using product_fingerprint data.
    Writes to VECTOR_DB (not the main app DB). Replaces existing rows for the
    same vendor_item_id.
    """
    fp_source = product_fingerprint
    if not isinstance(fp_source, dict) or not fp_source:
        fp_source = (
            vendor_item.product_fingerprint
            if isinstance(vendor_item.product_fingerprint, dict)
            else None
        )

    if not fp_source:
        fp_source = build_fingerprint_from_line_data(
            description=vendor_item.description or vendor_item.item_name,
            uom=vendor_item.uom,
            item_no=vendor_item.erp_code or vendor_item.vendor_product_code,
            vendor_item=vendor_item,
        )

    fingerprint = enrich_fingerprint_from_vendor_item(fp_source, vendor_item)
    if not fingerprint:
        logger.info(
            "Skipping vendor_item_fingerprint for vendor_item_id=%s — no fingerprint data",
            vendor_item.id,
        )
        return None

    # Fingerprint is stored unchanged; embedding uses product_name only.
    embedding_text = (
        (vendor_item.product_name or "").strip()
        or (vendor_item.item_name or "").strip()
        or ""
    )
    if not embedding_text:
        logger.info(
            "Skipping vendor_item_fingerprint for vendor_item_id=%s — empty product_name",
            vendor_item.id,
        )
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

    vector_db = VectorSessionLocal()
    try:
        vector_db.query(VendorItemFingerprint).filter(
            VendorItemFingerprint.vendor_item_id == vendor_item.id
        ).delete(synchronize_session=False)

        row = VendorItemFingerprint(
            vendor_item_id=vendor_item.id,
            fingerprint_json=fingerprint,
            embedding=vector,
            embedding_model=settings.embedding_model,
        )
        vector_db.add(row)
        vector_db.commit()
        vector_db.refresh(row)
        return row
    except Exception:
        vector_db.rollback()
        raise
    finally:
        vector_db.close()
