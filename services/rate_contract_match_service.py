"""Confirm rate-contract OCR line product matches against vendor catalog."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import VendorItem
from services.global_parameter import (
    ensure_uom_global_parameter,
    resolve_uom_from_global_parameter,
)
from services.ocr_alias_dictionary import create_product_name_alias
from services.ocr_item_rate_import import apply_rate_contract_line_confirm
from services.rate_contract_product_matching_engine import LINE_STATUS_CONFIRMED

logger = logging.getLogger(__name__)


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _normalize_alias_key(value: str) -> str:
    return " ".join(value.lower().split())


def _extract_ocr_product_name(ocr_line: Dict[str, Any]) -> str:
    """Best OCR label to store as a PRODUCT_NAME alias after user confirmation."""
    fingerprint = ocr_line.get("product_fingerprint")
    if isinstance(fingerprint, dict):
        review = fingerprint.get("matchReview")
        if isinstance(review, dict):
            from_review = _clean(review.get("productName"))
            if from_review:
                return from_review

    for key in ("product_name", "item_name"):
        value = _clean(ocr_line.get(key))
        if value:
            return value
    return ""


def _extract_ocr_uom(
    ocr_line: Dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """
    Return (ocr_uom_text, suggested_code) from the pending OCR line payload.
    """
    fingerprint = ocr_line.get("product_fingerprint")
    review: Dict[str, Any] = {}
    if isinstance(fingerprint, dict):
        maybe_review = fingerprint.get("matchReview")
        if isinstance(maybe_review, dict):
            review = maybe_review

    ocr_uom = (
        _clean(review.get("ocrUom"))
        or _clean(ocr_line.get("ocr_uom"))
        or _clean(ocr_line.get("ocrUom"))
        or (
            _clean(fingerprint.get("ocr_uom"))
            if isinstance(fingerprint, dict)
            else None
        )
        or (
            _clean(fingerprint.get("uom"))
            if isinstance(fingerprint, dict)
            else None
        )
        or _clean(ocr_line.get("uom"))
    )
    suggested = (
        _clean(review.get("suggestedUomCode"))
        or _clean(ocr_line.get("suggestedUomCode"))
        or (
            _clean(fingerprint.get("suggestedUomCode"))
            if isinstance(fingerprint, dict)
            else None
        )
    )
    if suggested:
        suggested = suggested[:3].upper()
    return ocr_uom, suggested


def _catalog_canonical_name(item: VendorItem) -> Optional[str]:
    return _clean(item.product_name) or _clean(item.item_name)


def _persist_rate_contract_product_alias(
    *,
    ocr_product_name: str,
    catalog_item: VendorItem,
    line_index: int,
    remarks: str,
) -> Optional[Dict[str, Any]]:
    """
    Insert OCR product label → catalog canonical name into ocr_alias_dictionary.

    Called after user confirms a rate-contract review choice (existing match or
    new product insert) so future uploads can resolve via alias search (Step 2).
    """
    alias_value = ocr_product_name.strip()
    canonical_value = _catalog_canonical_name(catalog_item)
    if not alias_value or not canonical_value:
        return None
    if _normalize_alias_key(alias_value) == _normalize_alias_key(canonical_value):
        logger.info(
            "[RATE CONTRACT MATCH] skip alias line=%s — OCR name equals canonical %r",
            line_index,
            canonical_value,
        )
        return None

    item_id = catalog_item.item_id or catalog_item.id
    if item_id is None:
        return None

    logger.info(
        "[RATE CONTRACT MATCH] create alias line=%s alias=%r canonical=%r item_id=%s",
        line_index,
        alias_value,
        canonical_value,
        item_id,
    )
    return create_product_name_alias(
        alias_value=alias_value,
        canonical_value=canonical_value,
        item_id=int(item_id),
        remarks=remarks[:500],
    )


def _stamp_item_uom(
    db_item: VendorItem,
    *,
    ocr_uom: Optional[str],
    confirmed_code: Optional[str],
) -> Optional[str]:
    """Apply resolved / confirmed UOM onto the vendor item row."""
    code = None
    if confirmed_code:
        code = confirmed_code.strip()[:3].upper() or None
    if not code and ocr_uom:
        resolved = resolve_uom_from_global_parameter(ocr_uom)
        code = resolved.canonical_code
    if not code and ocr_uom:
        code = ocr_uom.strip()[:3].upper() or None
    if not code:
        return None
    if not (db_item.uom or "").strip():
        db_item.uom = code
    db_item.uom_p = code
    return code


def confirm_rate_contract_line_match(
    db: Session,
    *,
    vendor_id: int,
    line_index: int,
    selected_vendor_item_id: Optional[int],
    ocr_line: Dict[str, Any],
    create_alias: bool = True,
    create_uom_parameter: bool = True,
    confirmed_uom_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply user-selected vendor catalog match for one rate-contract OCR line."""
    logger.info(
        "[RATE CONTRACT MATCH] confirm line_index=%s vendor_id=%s selected_vendor_item_id=%s",
        line_index,
        vendor_id,
        selected_vendor_item_id,
    )
    if not isinstance(ocr_line, dict) or not ocr_line:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="ocrLine is required",
        )

    ocr_product_name = _extract_ocr_product_name(ocr_line)
    ocr_uom, suggested_uom_code = _extract_ocr_uom(ocr_line)
    effective_uom_code = (
        (confirmed_uom_code or "").strip()[:3].upper()
        or suggested_uom_code
        or None
    )

    db_item, was_inserted, fingerprints_synced = apply_rate_contract_line_confirm(
        db,
        vendor_id,
        ocr_line,
        existing_vendor_item_id=selected_vendor_item_id,
    )

    stamped_code = _stamp_item_uom(
        db_item,
        ocr_uom=ocr_uom,
        confirmed_code=effective_uom_code,
    )

    db.commit()
    db.refresh(db_item)

    alias_created = False
    alias_payload = None
    if create_alias and ocr_product_name:
        remarks = (
            f"Rate contract line {line_index} user new product confirmation"
            if was_inserted
            else f"Rate contract line {line_index} user catalog match confirmation"
        )
        try:
            alias_result = _persist_rate_contract_product_alias(
                ocr_product_name=ocr_product_name,
                catalog_item=db_item,
                line_index=line_index,
                remarks=remarks,
            )
            alias_created = (
                isinstance(alias_result, dict) and not alias_result.get("skipped")
            )
            alias_payload = alias_result
        except Exception as exc:
            logger.warning(
                "Could not create product alias for vendor_item_id=%s line=%s: %s",
                db_item.id,
                line_index,
                exc,
            )
            alias_created = False
            alias_payload = None

    uom_created = False
    uom_payload = None
    if create_uom_parameter and ocr_uom:
        try:
            uom_result = ensure_uom_global_parameter(
                ocr_uom,
                confirmed_code=effective_uom_code or stamped_code,
            )
            uom_created = (
                isinstance(uom_result, dict) and not uom_result.get("skipped")
            )
            uom_payload = uom_result
            if isinstance(uom_result, dict) and uom_result.get("code"):
                code = str(uom_result["code"]).strip()[:3].upper()
                if code:
                    db_item.uom_p = code
                    if not (db_item.uom or "").strip():
                        db_item.uom = code
                    db.commit()
                    db.refresh(db_item)
                    stamped_code = code
        except Exception as exc:
            logger.warning(
                "Could not create UOM global_parameter for vendor_item_id=%s "
                "line=%s ocr_uom=%r: %s",
                db_item.id,
                line_index,
                ocr_uom,
                exc,
            )
            uom_created = False
            uom_payload = None

    return {
        "status": "confirmed",
        "lineStatus": LINE_STATUS_CONFIRMED,
        "vendorItemId": int(db_item.id),
        "wasInserted": was_inserted,
        "fingerprintsSynced": fingerprints_synced,
        "aliasCreated": alias_created,
        "aliasSkippedDuplicate": bool(
            isinstance(alias_payload, dict) and alias_payload.get("skipped")
        ),
        "alias": alias_payload,
        "uomParameterCreated": uom_created,
        "uomParameterSkippedDuplicate": bool(
            isinstance(uom_payload, dict) and uom_payload.get("skipped")
        ),
        "uomParameter": uom_payload,
        "ocrUom": ocr_uom,
        "confirmedUomCode": stamped_code,
        "itemStatus": db_item.status,
        "catalogProductName": _catalog_canonical_name(db_item),
        "catalogDescription": _clean(db_item.description),
        "ocrProductName": ocr_product_name or None,
        "lineIndex": line_index,
    }
