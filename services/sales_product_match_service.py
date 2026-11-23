"""Confirm sales upload product matches against the products catalog."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from models import Product, SalesTransaction
from services.ocr_alias_dictionary import create_product_name_alias
from services.sales_product_matching_engine import (
    LINE_STATUS_CONFIRMED,
    MATCH_COLOR_GREEN,
)

logger = logging.getLogger(__name__)


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _clip(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    return s if len(s) <= max_len else s[:max_len]


def _normalize_alias_key(value: str) -> str:
    return " ".join(value.lower().split())


def _parse_date(val: Any) -> Optional[date]:
    if val is None or val == "":
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        try:
            return date.fromisoformat(val.strip()[:10])
        except ValueError:
            return None
    return None


def _parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).strip().replace(",", ""))
    except Exception:
        return None


def _extract_sales_product_name(sales_line: Dict[str, Any]) -> str:
    review = sales_line.get("matchReview")
    if isinstance(review, dict):
        from_review = _clean(review.get("productName"))
        if from_review:
            return from_review
    return _clean(sales_line.get("product_name")) or ""


def _persist_sales_product_alias(
    *,
    ocr_product_name: str,
    catalog_product: Product,
    line_index: int,
    remarks: str,
) -> Optional[Dict[str, Any]]:
    alias_value = ocr_product_name.strip()
    canonical_value = _clean(catalog_product.name)
    if not alias_value or not canonical_value:
        return None
    if _normalize_alias_key(alias_value) == _normalize_alias_key(canonical_value):
        logger.info(
            "[SALES MATCH] skip alias line=%s — OCR name equals canonical %r",
            line_index,
            canonical_value,
        )
        return None

    logger.info(
        "[SALES MATCH] create alias line=%s alias=%r canonical=%r product_id=%s",
        line_index,
        alias_value,
        canonical_value,
        catalog_product.product_id,
    )
    return create_product_name_alias(
        alias_value=alias_value,
        canonical_value=canonical_value,
        item_id=int(catalog_product.product_id),
        remarks=remarks[:500],
    )


def confirm_sales_line_product_match(
    db: Session,
    *,
    line_index: int,
    selected_product_id: int,
    sales_line: Dict[str, Any],
    tenant_id: int = 1,
    company_id: int = 3,
    created_by: Optional[int] = None,
    created_ip: Optional[str] = None,
    create_alias: bool = True,
) -> Dict[str, Any]:
    """Insert one sales transaction after the user picks a catalog product."""
    logger.info(
        "[SALES MATCH] confirm line_index=%s selected_product_id=%s",
        line_index,
        selected_product_id,
    )
    if not isinstance(sales_line, dict) or not sales_line:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="salesLine is required",
        )

    product = (
        db.query(Product)
        .filter(Product.product_id == int(selected_product_id))
        .first()
    )
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Product with id {selected_product_id} not found",
        )

    transaction_date = _parse_date(sales_line.get("transaction_date"))
    product_name = _clip(_clean(sales_line.get("product_name")), 255)
    category = _clip(_clean(sales_line.get("category")), 100)
    qty_sold = _parse_decimal(sales_line.get("qty_sold"))

    if transaction_date is None or not product_name or qty_sold is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="salesLine must include transaction_date, product_name, and qty_sold",
        )

    row = SalesTransaction(
        transaction_date=transaction_date,
        product_id=int(selected_product_id),
        product_name=product_name,
        category=category,
        qty_sold=qty_sold,
        tenant_id=tenant_id,
        company_id=company_id,
        created_by=created_by,
        created_ip=_clip(created_ip, 45) if created_ip else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    ocr_product_name = _extract_sales_product_name(sales_line)
    alias_created = False
    alias_payload = None
    if create_alias and ocr_product_name:
        remarks = f"Sales line {line_index} user catalog match confirmation"
        try:
            alias_result = _persist_sales_product_alias(
                ocr_product_name=ocr_product_name,
                catalog_product=product,
                line_index=line_index,
                remarks=remarks,
            )
            alias_created = (
                isinstance(alias_result, dict) and not alias_result.get("skipped")
            )
            alias_payload = alias_result
        except Exception as exc:
            logger.warning(
                "Could not create product alias for product_id=%s line=%s: %s",
                product.product_id,
                line_index,
                exc,
            )

    return {
        "status": "confirmed",
        "lineStatus": LINE_STATUS_CONFIRMED,
        "matchColor": MATCH_COLOR_GREEN,
        "salesTransactionId": int(row.sales_transaction_id),
        "productId": int(product.product_id),
        "aliasCreated": alias_created,
        "aliasSkippedDuplicate": bool(
            isinstance(alias_payload, dict) and alias_payload.get("skipped")
        ),
        "alias": alias_payload,
        "catalogProductName": _clean(product.name),
        "ocrProductName": ocr_product_name or None,
        "lineIndex": line_index,
    }
