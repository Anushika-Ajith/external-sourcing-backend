"""
Upsert vendor_items and append vendor_rates from structured OCR payload.

For rate contracts (``sync_item_fingerprints=True``), products are matched via
``RateContractMatchingEngine`` (vendor catalog pool — not PO lines) and updated
when found, otherwise inserted.

For other document types, matching falls back to erp_code / vendor_product_code /
barcode / case-insensitive item_name.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Vendor, VendorItem, VendorRate
from services.item_embedding_catalog import (
    resolve_vendor_code,
    sync_vendor_item_to_item_embeddings,
)
from services.foundation_product import create_item_in_erp, create_product_in_foundation
from services.item_fingerprint import (
    FINGERPRINT_FIELDS,
    enrich_fingerprint_from_vendor_item,
    missing_rate_contract_fingerprint_fields,
    normalize_fingerprint,
    parse_rate_contract_fingerprint_with_openai,
)
from services.ocr_alias_dictionary import resolve_pack_size_canonical
from services.global_parameter import (
    UomResolveResult,
    resolve_uom_from_global_parameter,
)
from services.next_numbering import get_next_vendor_product_code_from_nest
from services.product_matching_common import (
    LINE_STATUS_PENDING_CONFIRM,
    MATCH_COLOR_GREEN,
    MATCH_TYPE_EXACT,
    build_match_review,
    evaluate_product_code_embedding_gate,
)
from services.internal_product_identity import resolve_shared_internal_product_codes
from services.rate_contract_product_matching_engine import (
    LINE_STATUS_CONFIRMED,
    LINE_STATUS_NO_MATCH,
    LINE_STATUS_PENDING_SELECT,
    VENDOR_ITEM_STATUS_CONFIRMED,
    VENDOR_ITEM_STATUS_PENDING,
    find_existing_vendor_item_by_identity,
    match_rate_contract_line,
)
from services.vendor_item_fingerprint_sync import sync_vendor_item_fingerprint

logger = logging.getLogger(__name__)

_PACK_SIZE_TOKEN_RE = re.compile(
    r"(?i)\b(\d+(?:[xX*]\d+)+|\d+(?:\.\d+)?\s*(?:gm|g|kg|ml|l|pcs|pc|nos|ea)|"
    r"dozen|gm|g|kg|ml|l|pcs|pc|nos)\b"
)


def _clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).strip().replace(",", ""))
    except Exception:
        return None


def _format_pack_size_key(val: Any) -> Optional[str]:
    """Normalize pack size for equality checks (string key, uppercased)."""
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
    # Collapse whitespace for keys like "500 GM"
    text = re.sub(r"\s+", " ", text)
    numeric = _parse_decimal(text)
    if numeric is not None and re.fullmatch(r"[\d.,]+", text.replace(" ", "")):
        return _format_pack_size_key(numeric)
    return text.upper()


def _extract_raw_pack_size(row: Dict[str, Any]) -> Optional[str]:
    """Pull pack size from OCR line fields / fingerprint / description."""
    for key in ("pack_size", "consumer_unit_size"):
        raw = _clean_str(row.get(key))
        if raw:
            return raw

    fingerprint = row.get("product_fingerprint")
    if isinstance(fingerprint, dict):
        raw = _clean_str(fingerprint.get("pack_size"))
        if raw:
            return raw

    # Last resort: token from product/item name or description.
    # Do NOT fall back to uom — values like "Carton"/"Pack" are units, not pack size.
    for key in ("product_name", "item_name", "description"):
        text = _clean_str(row.get(key))
        if not text:
            continue
        match = _PACK_SIZE_TOKEN_RE.search(text)
        if match:
            return match.group(1).strip()
    return None


def _canonical_pack_size(row: Dict[str, Any]) -> Optional[str]:
    raw = _extract_raw_pack_size(row)
    if not raw:
        return None
    return resolve_pack_size_canonical(raw)


def _apply_canonical_pack_size(db_item: VendorItem, canonical: Optional[str]) -> None:
    """Persist canonical pack size on the vendor item."""
    if not canonical:
        return
    numeric = _parse_decimal(canonical)
    # Pure numeric (e.g. "12") → pack_size column; unit-like aliases (GM→G)
    # go on consumer_unit so size fields like consumer_unit_size stay intact.
    if numeric is not None and re.fullmatch(
        r"[\d.,\s]+", canonical.replace(",", "")
    ):
        db_item.pack_size = numeric
    else:
        db_item.consumer_unit = _clip(canonical, 20) or canonical


def _item_pack_size_key(item: VendorItem) -> Optional[str]:
    """Best pack-size identity currently stored on a vendor item.

    Prefer consumer_unit / fingerprint pack strings over numeric ``pack_size``.
    Rate-contract packs like ``60 x 100 g`` are stored on consumer_unit while
    ``pack_size`` is often the NOT NULL default ``1``.
    """
    for candidate in (
        item.consumer_unit,
        item.consumer_unit_size,
    ):
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
        # Ignore the placeholder default of 1 when a richer pack string exists above.
        try:
            if Decimal(item.pack_size) == Decimal("1"):
                pass
            else:
                return _format_pack_size_key(item.pack_size)
        except (InvalidOperation, TypeError):
            return _format_pack_size_key(item.pack_size)

    for candidate in (
        item.uom,
        item.purchase_unit,
    ):
        key = _format_pack_size_key(candidate)
        if key:
            return key
    return None


def _pack_sizes_match(item: VendorItem, canonical: Optional[str]) -> bool:
    item_key = _item_pack_size_key(item)
    want = _format_pack_size_key(canonical)
    if not want and not item_key:
        return True
    if not want or not item_key:
        # Still allow consumer_unit / fingerprint fallbacks below when one side
        # only has the numeric pack_size default.
        pass
    if item_key and want and item_key == want:
        return True

    # Do not early-return False on numeric pack_size==1 vs rich pack strings.
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


def _apply_product_attrs(
    db_item: VendorItem,
    row: Dict[str, Any],
    *,
    overwrite: bool = False,
    canonical_pack_size: Optional[str] = None,
) -> None:
    """Map extended vendor_items product columns from an OCR/catalog row."""
    mapping = {
        "brand": (100, None),
        "manufacturer": (255, None),
        "product_name": (255, None),
        "category": (100, None),
        "consumer_unit": (20, None),
        "consumer_unit_size": (50, None),
        "purchase_unit": (20, None),
        "status": (20, None),
    }
    for field, (maxlen, _) in mapping.items():
        raw = _clip(_clean_str(row.get(field)), maxlen)
        if not raw:
            continue
        current = getattr(db_item, field, None)
        if overwrite or not (current or "").strip():
            setattr(db_item, field, raw)

    if canonical_pack_size:
        _apply_canonical_pack_size(db_item, canonical_pack_size)
    else:
        pack_size = _parse_decimal(row.get("pack_size"))
        if pack_size is not None and (overwrite or db_item.pack_size is None):
            db_item.pack_size = pack_size

    conversion_factor = _parse_decimal(row.get("conversion_factor"))
    if conversion_factor is not None and (overwrite or db_item.conversion_factor is None):
        db_item.conversion_factor = conversion_factor

    uom_p = _clip(_clean_str(row.get("uom_p")), 3)
    if uom_p and (overwrite or not _clean_str(getattr(db_item, "uom_p", None))):
        db_item.uom_p = uom_p
    uom_s = _clip(_clean_str(row.get("uom_s")), 3)
    if uom_s and (overwrite or not _clean_str(getattr(db_item, "uom_s", None))):
        db_item.uom_s = uom_s
    conv_f = _parse_decimal(row.get("conv_f"))
    if conv_f is not None and (overwrite or getattr(db_item, "conv_f", None) is None):
        db_item.conv_f = conv_f
    conv_t = _parse_decimal(row.get("conv_t"))
    if conv_t is not None and (overwrite or getattr(db_item, "conv_t", None) is None):
        db_item.conv_t = conv_t

    # Prefer explicit product_name; fall back to item_name for display.
    if not (db_item.product_name or "").strip() and (db_item.item_name or "").strip():
        db_item.product_name = db_item.item_name


def _fill_vendor_item_required_fields(
    db_item: VendorItem,
    *,
    vendor_code: Optional[str] = None,
) -> None:
    """Populate required vendor_items columns before insert/update."""
    product_name = (
        _clean_str(getattr(db_item, "product_name", None))
        or _clean_str(getattr(db_item, "item_name", None))
        or _clean_str(getattr(db_item, "description", None))
        or "UNKNOWN PRODUCT"
    )
    db_item.product_name = _clip(product_name, 255) or "UNKNOWN PRODUCT"

    brand = (
        _clean_str(getattr(db_item, "brand", None))
        or _clean_str(getattr(db_item, "manufacturer", None))
        or db_item.product_name
    )
    db_item.brand = _clip(brand, 100) or "UNKNOWN BRAND"

    manufacturer = (
        _clean_str(getattr(db_item, "manufacturer", None))
        or _clean_str(getattr(db_item, "brand", None))
        or db_item.brand
    )
    db_item.manufacturer = _clip(manufacturer, 255) or "UNKNOWN MANUFACTURER"

    category = _clean_str(getattr(db_item, "category", None)) or "UNCATEGORIZED"
    db_item.category = _clip(category, 100) or "UNCATEGORIZED"

    if not _clean_str(getattr(db_item, "vendor_code", None)) and vendor_code:
        db_item.vendor_code = _clip(vendor_code, 20) or vendor_code

    if db_item.pack_size is None:
        db_item.pack_size = Decimal("1")


def _row_or_fingerprint_value(
    row: Dict[str, Any],
    key: str,
    fingerprint: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Prefer top-level OCR row value, then product_fingerprint."""
    value = _clean_str(row.get(key))
    if value:
        return value
    fp = fingerprint if isinstance(fingerprint, dict) else row.get("product_fingerprint")
    if isinstance(fp, dict):
        return _clean_str(fp.get(key))
    return None


def _resolve_logged_in_vendor_code(
    vendor: Optional[Vendor],
    vendor_id: int,
) -> str:
    """
    Rate-contract fingerprints take vendor_code from the vendor account when set.

    Draft vendors (pre-approval) may have no code yet — return empty string so
    item rows can be saved with null vendor_code until approval backfills.
    """
    from services.vendor_codes import ensure_vendor_code_from_registration

    if vendor is not None:
        code = ensure_vendor_code_from_registration(vendor)
        if code:
            return code

    fallback = _clean_str(resolve_vendor_code(vendor, vendor_id))
    # resolve_vendor_code may invent V{id}; only use a real assigned code here.
    if vendor is not None and ensure_vendor_code_from_registration(vendor):
        return _clip(fallback, 20) or fallback

    return ""


_VENDOR_ITEM_PLACEHOLDERS = {
    "UNKNOWN BRAND",
    "UNKNOWN MANUFACTURER",
    "UNCATEGORIZED",
    "UNKNOWN PRODUCT",
}


def _is_empty_or_placeholder(value: Any) -> bool:
    text = _clean_str(value)
    if not text:
        return True
    return text.upper() in _VENDOR_ITEM_PLACEHOLDERS


def _rate_contract_openai_source_text(
    row: Dict[str, Any],
    *,
    product_name: Optional[str],
    uom: Optional[str],
    description: Optional[str],
) -> str:
    """Build the text OpenAI uses to extract a rate-contract product fingerprint."""
    chunks: List[str] = []
    for label, value in (
        (
            "Product Name",
            product_name
            or _clean_str(row.get("product_name"))
            or _clean_str(row.get("item_name")),
        ),
        ("Category", row.get("category")),
        ("Brand", row.get("brand")),
        ("Manufacturer", row.get("manufacturer")),
        ("Pack Size", row.get("pack_size")),
        ("UOM", uom or row.get("uom")),
        ("Description", description or row.get("description")),
        ("ERP Code", row.get("erp_code")),
        ("Vendor Product Code", row.get("vendor_product_code")),
    ):
        text = _clean_str(value)
        if text:
            chunks.append(f"{label}: {text}")
    return "\n".join(chunks)


def _apply_fingerprint_fields_to_row(
    row: Dict[str, Any],
    fingerprint: Dict[str, Any],
) -> Dict[str, Any]:
    """Copy OpenAI fingerprint identity fields onto the OCR row when missing."""
    updated = dict(row)
    for key in ("category", "brand", "manufacturer", "uom", "pack_size"):
        if _is_empty_or_placeholder(updated.get(key)):
            value = _clean_str(fingerprint.get(key))
            if value:
                updated[key] = value
    if _is_empty_or_placeholder(updated.get("product_name")):
        name = (
            _clean_str(fingerprint.get("normalized_name"))
            or _clean_str(fingerprint.get("brand"))
        )
        if name:
            updated["product_name"] = name
    return updated


def _http_422() -> int:
    """Prefer Starlette's newer alias when present; avoid deprecation warnings."""
    return int(
        getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", None)
        or getattr(status, "HTTP_422_UNPROCESSABLE_ENTITY", 422)
    )


def _require_fingerprint_mandatory_fields(
    fingerprint: Dict[str, Any],
    *,
    line_index: int,
) -> None:
    """
    category / manufacturer / brand / uom / pack_size / vendor_code are mandatory
    on product_fingerprint (sourced from OpenAI + logged-in vendor_code), not OCR.
    """
    missing = missing_rate_contract_fingerprint_fields(fingerprint)
    if missing:
        logger.warning(
            "Rate-contract fingerprint missing mandatory fields at line %s: %s | fingerprint=%s",
            line_index,
            missing,
            fingerprint,
        )
        raise HTTPException(
            status_code=_http_422(),
            detail=(
                f"Invalid rate contract item at line {line_index}: "
                f"product_fingerprint missing required fields {', '.join(missing)}"
            ),
        )


def _complete_product_fingerprint_json(
    fingerprint: Dict[str, Any],
    *,
    vendor_code: str,
) -> Dict[str, Any]:
    """
    Persist the full product_fingerprint JSON with every standard key present.

    OpenAI / merged values are normalized; missing keys are stored as null so the
    JSON always contains the complete fingerprint schema.
    """
    normalized = normalize_fingerprint(
        {**fingerprint, "vendor_code": vendor_code},
        uom_fallback=_clean_str(fingerprint.get("uom")),
    )
    complete: Dict[str, Any] = {}
    for key in FINGERPRINT_FIELDS:
        complete[key] = normalized.get(key)
    complete["vendor_code"] = _clean_str(vendor_code) or complete.get("vendor_code")
    return complete


def _fill_vendor_item_from_fingerprint(
    db_item: VendorItem,
    fingerprint: Dict[str, Any],
    *,
    canonical_pack_size: Optional[str] = None,
) -> None:
    """
    Populate vendor_items columns from product_fingerprint.

    For rate contracts, category / manufacturer / brand / uom / pack_size come
    from the fingerprint (mandatory there), not from OCR text.
    """
    field_map = (
        ("brand", "brand", 100),
        ("manufacturer", "manufacturer", 255),
        ("category", "category", 100),
        ("uom", "uom", 50),
    )
    for fp_key, attr, maxlen in field_map:
        value = _clip(_clean_str(fingerprint.get(fp_key)), maxlen)
        if value:
            setattr(db_item, attr, value)

    name = _clip(_clean_str(fingerprint.get("normalized_name")), 255)
    if name and _is_empty_or_placeholder(getattr(db_item, "product_name", None)):
        db_item.product_name = name
        if _is_empty_or_placeholder(getattr(db_item, "item_name", None)):
            db_item.item_name = name

    code = _clip(_clean_str(fingerprint.get("vendor_code")), 20)
    if code:
        db_item.vendor_code = code

    pack = canonical_pack_size or _clean_str(fingerprint.get("pack_size"))
    if pack:
        _apply_canonical_pack_size(db_item, pack)


def _extract_rate_contract_fingerprint_with_openai(
    *,
    row: Dict[str, Any],
    vendor_code: str,
    canonical_pack_size: Optional[str],
    uom: Optional[str],
    product_name: Optional[str],
    description: Optional[str],
    line_index: int,
) -> Dict[str, Any]:
    """Call OpenAI once per rate-contract line and seed vendor_code."""
    source = _rate_contract_openai_source_text(
        row,
        product_name=product_name,
        uom=uom,
        description=description,
    )
    if not source:
        source = product_name or "UNKNOWN PRODUCT"

    try:
        openai_fp = parse_rate_contract_fingerprint_with_openai(
            source,
            uom=uom or _clean_str(row.get("uom")),
            item_no=_clean_str(row.get("erp_code"))
            or _clean_str(row.get("vendor_product_code")),
            pack_size_hint=canonical_pack_size or _clean_str(row.get("pack_size")),
        )
    except Exception as exc:
        logger.warning(
            "OpenAI rate-contract fingerprint failed at line %s: %s",
            line_index,
            exc,
        )
        openai_fp = {}

    raw: Dict[str, Any] = dict(openai_fp) if isinstance(openai_fp, dict) else {}
    for key in (
        "category",
        "manufacturer",
        "brand",
        "uom",
        "pack_size",
        "normalized_name",
        "generic",
        "strength",
        "dosage_form",
    ):
        if not _clean_str(raw.get(key)):
            value = _clean_str(row.get(key))
            if value:
                raw[key] = value

    raw["vendor_code"] = vendor_code
    if canonical_pack_size and not _clean_str(raw.get("pack_size")):
        raw["pack_size"] = canonical_pack_size
    if uom and not _clean_str(raw.get("uom")):
        raw["uom"] = uom
    if not _clean_str(raw.get("normalized_name")) and _clean_str(product_name):
        raw["normalized_name"] = product_name

    # Deterministic fallbacks so mandatory fingerprint fields are not blank
    # when OpenAI returns a partial result for sparse rate-contract lines.
    brand = _clean_str(raw.get("brand"))
    if not brand and _clean_str(product_name):
        brand = _clean_str(str(product_name).split()[0])
        if brand:
            raw["brand"] = brand
    if not _clean_str(raw.get("manufacturer")) and brand:
        raw["manufacturer"] = brand
    if not _clean_str(raw.get("category")):
        raw["category"] = "GENERAL"
    if not _clean_str(raw.get("uom")):
        raw["uom"] = uom or "NOS"
    if not _clean_str(raw.get("pack_size")):
        raw["pack_size"] = canonical_pack_size or _clean_str(row.get("pack_size")) or "1"

    # Return the complete fingerprint schema for storage on vendor_items.
    return _complete_product_fingerprint_json(raw, vendor_code=vendor_code)


def _finalize_rate_contract_product_fingerprint(
    db_item: VendorItem,
    *,
    fingerprint: Dict[str, Any],
    vendor_code: str,
    canonical_pack_size: Optional[str],
) -> Dict[str, Any]:
    """Merge OpenAI fingerprint with vendor-item fields; store full JSON schema."""
    enriched = enrich_fingerprint_from_vendor_item(
        fingerprint,
        db_item,
        pack_size_override=canonical_pack_size,
    )
    enriched["vendor_code"] = vendor_code
    return _complete_product_fingerprint_json(enriched, vendor_code=vendor_code)

def _build_rate_contract_product_fingerprint(
    db_item: VendorItem,
    *,
    row: Dict[str, Any],
    fingerprint: Optional[Dict[str, Any]],
    vendor_code: Optional[str],
    canonical_pack_size: Optional[str],
    uom: Optional[str],
    product_name: Optional[str] = None,
    description: Optional[str] = None,
    line_index: int = 0,
) -> Dict[str, Any]:
    """
    Build product_fingerprint for rate-contract upload via OpenAI.

    Uses ``parse_rate_contract_fingerprint_with_openai`` for each product line,
    then forces ``vendor_code`` from the logged-in / upload-target vendor account.
    """
    if not _clean_str(vendor_code):
        raise HTTPException(
            status_code=_http_422(),
            detail=(
                "product_fingerprint missing required field vendor_code "
                "(must come from the logged-in vendor account)"
            ),
        )

    openai_fp = _extract_rate_contract_fingerprint_with_openai(
        row=row,
        vendor_code=vendor_code,
        canonical_pack_size=canonical_pack_size,
        uom=uom,
        product_name=product_name,
        description=description,
        line_index=line_index,
    )
    if isinstance(fingerprint, dict):
        for key, value in fingerprint.items():
            if key == "vendor_code":
                continue
            if not _clean_str(openai_fp.get(key)) and _clean_str(value):
                openai_fp[key] = value

    return _finalize_rate_contract_product_fingerprint(
        db_item,
        fingerprint=openai_fp,
        vendor_code=vendor_code,
        canonical_pack_size=canonical_pack_size,
    )
def _parse_date(val: Any) -> Optional[date]:
    if val is None or val == "":
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str):
        s = val.strip()[:10]
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    return None


def _parse_rate(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).replace(",", ""))
    except Exception:
        return None


def _clip(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    return s if len(s) <= max_len else s[:max_len]


def _ensure_vendor_product_code(
    current_value: Optional[str],
    *,
    tenant_id: int = 1,
    company_id: int = 1,
    department_id: int = 1,
) -> str:
    code = _clip(_clean_str(current_value), 20)
    if code:
        return code
    try:
        generated = get_next_vendor_product_code_from_nest(
            tenant_id=tenant_id,
            company_id=company_id,
            department_id=department_id,
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to allocate vendor product code from next-numbering: {exc}",
        ) from exc
    code = _clip(_clean_str(generated), 20)
    if not code:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Next-numbering returned an empty vendor product code",
        )
    return code


def _create_foundation_product_for_rate_contract(
    *,
    tenant_id: int,
    company_id: int,
    row: Dict[str, Any],
    fingerprint: Dict[str, Any],
    name: str,
    product_name: str,
    desc_str: Optional[str],
    uom: Optional[str],
    canonical_pack_size: Optional[str],
    created_by: Optional[int] = None,
    department_id: int = 1,
) -> Dict[str, Optional[str]]:
    """
    Create ERP item + Foundation product for a new rate-contract line.

    Next-numbering runs inside each downstream API:
    - ERP ``itemCode`` (ERP_CODE) -> vendor_items.erp_code
    - Foundation ``productCode`` (ITEM_MASTER) -> vendor_items.company_code
    """
    pack_size_numeric = _parse_decimal(canonical_pack_size or row.get("pack_size"))
    category = _clean_str(fingerprint.get("category")) or _clean_str(row.get("category"))
    brand = _clean_str(fingerprint.get("brand")) or _clean_str(row.get("brand"))
    hsn_code = (
        _clean_str(row.get("hsn_code"))
        or _clean_str(row.get("hsnCode"))
        or "3004"
    )
    hsn_description = (
        _clean_str(row.get("hsn_description")) or _clean_str(row.get("hsnDescription"))
    )
    consumer_unit = (
        uom or _clean_str(row.get("consumer_unit")) or _clean_str(row.get("consumerUnit"))
    )
    consumer_unit_size = (
        canonical_pack_size
        or _clean_str(row.get("consumer_unit_size"))
        or _clean_str(row.get("consumerUnitSize"))
    )
    purchase_unit = (
        _clean_str(row.get("purchase_unit"))
        or _clean_str(row.get("purchaseUnit"))
        or uom
    )
    conversion_factor = (
        _parse_decimal(row.get("conversion_factor") or row.get("conversionFactor"))
        or Decimal("1")
    )

    erp_item = create_item_in_erp(
        tenant_id=tenant_id,
        company_id=company_id,
        name=name,
        description=desc_str,
        category=category,
        hsn_code=hsn_code,
        hsn_description=hsn_description,
        brand=brand,
        product_name=product_name,
        consumer_unit=consumer_unit,
        consumer_unit_size=consumer_unit_size,
        purchase_unit=purchase_unit,
        pack_size=pack_size_numeric,
        conversion_factor=conversion_factor,
        status_value="Active",
        is_active=True,
        created_by=created_by,
    )
    foundation_product = create_product_in_foundation(
        tenant_id=tenant_id,
        company_id=company_id,
        department_id=department_id,
        name=name,
        description=desc_str,
        category=category,
        hsn_code=hsn_code,
        hsn_description=hsn_description,
        brand=brand,
        product_name=product_name,
        consumer_unit=consumer_unit,
        consumer_unit_size=consumer_unit_size,
        purchase_unit=purchase_unit,
        pack_size=pack_size_numeric,
        conversion_factor=conversion_factor,
        status_value="ACTIVE",
        is_active=True,
        created_by=created_by,
    )

    erp_code = _clip(
        _clean_str(erp_item.get("itemCode")) or _clean_str(erp_item.get("item_code")),
        20,
    )
    company_code = _clip(
        _clean_str(foundation_product.get("productCode"))
        or _clean_str(foundation_product.get("product_code")),
        50,
    )
    if not erp_code:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="ERP item creation did not return itemCode",
        )
    if not company_code:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Foundation product creation did not return productCode",
        )
    return {"erp_code": erp_code, "company_code": company_code}


def assign_vendor_item_master_codes(
    db: Session,
    db_item: VendorItem,
    *,
    created_by: Optional[int] = None,
) -> None:
    """
    Fill a confirmed vendor item's missing ``vendor_product_code``, ``erp_code``
    and ``company_code``. Called only when the user confirms the item.

    Reuses a shared ERP / Foundation identity when one already exists for the
    product; otherwise creates the ERP item + Foundation product.
    """
    vendor = db.query(Vendor).filter(Vendor.vendor_id == db_item.vendor_id).first()
    tenant_id = int(getattr(vendor, "tenant_id", None) or 1)
    company_id = int(getattr(vendor, "company_id", None) or 1)

    if not _clean_str(db_item.vendor_product_code):
        db_item.vendor_product_code = _ensure_vendor_product_code(
            None,
            tenant_id=tenant_id,
            company_id=company_id,
            department_id=1,
        )

    if _clean_str(db_item.erp_code) and _clean_str(db_item.company_code):
        return

    fingerprint = (
        dict(db_item.product_fingerprint)
        if isinstance(db_item.product_fingerprint, dict)
        else {}
    )
    fingerprint.pop("matchReview", None)
    product_name = (
        _clean_str(db_item.product_name)
        or _clean_str(db_item.item_name)
        or "UNKNOWN PRODUCT"
    )
    shared_codes = resolve_shared_internal_product_codes(
        db,
        product_name=product_name,
        product_code=_clean_str(db_item.erp_code) or _clean_str(db_item.vendor_product_code),
        erp_code=_clean_str(db_item.erp_code),
        company_code=_clean_str(db_item.company_code),
        fingerprint=fingerprint or None,
        exclude_vendor_item_ids={int(db_item.id)} if db_item.id is not None else None,
    )
    if shared_codes:
        codes = shared_codes
    else:
        item_row: Dict[str, Any] = {
            key: value
            for key, value in {
                "category": db_item.category,
                "brand": db_item.brand,
                "pack_size": db_item.pack_size,
                "consumer_unit": db_item.consumer_unit,
                "consumer_unit_size": db_item.consumer_unit_size,
                "purchase_unit": db_item.purchase_unit,
                "conversion_factor": db_item.conversion_factor,
            }.items()
            if value is not None and value != ""
        }
        codes = _create_foundation_product_for_rate_contract(
            tenant_id=tenant_id,
            company_id=company_id,
            row={**fingerprint, **item_row},
            fingerprint=fingerprint,
            name=_clean_str(db_item.item_name) or product_name,
            product_name=product_name,
            desc_str=_clean_str(db_item.description),
            uom=_clean_str(db_item.uom),
            canonical_pack_size=_item_pack_size_key(db_item),
            created_by=created_by,
            department_id=1,
        )

    if not _clean_str(db_item.erp_code) and codes.get("erp_code"):
        db_item.erp_code = _clip(codes["erp_code"], 20)
    if not _clean_str(db_item.company_code) and codes.get("company_code"):
        db_item.company_code = _clip(codes["company_code"], 50)


def _resolve_item_name(row: Dict[str, Any]) -> Optional[str]:
    """Prefer item_name; else product_name; else first line of description."""
    name = _clean_str(row.get("item_name")) or _clean_str(row.get("product_name"))
    if name:
        return _clip(name, 255) or name
    desc = row.get("description")
    if desc is None:
        return None
    text = str(desc).strip()
    if not text:
        return None
    line = text.split("\n", 1)[0].strip()
    if not line:
        return None
    return _clip(line, 255) or line


def _resolve_product_name(row: Dict[str, Any], item_name: str) -> str:
    return (
        _clip(_clean_str(row.get("product_name")), 255)
        or _clip(item_name, 255)
        or item_name
    )


def _find_existing_vendor_item(
    db: Session,
    vendor_id: int,
    item_name: str,
    erp: Optional[str],
    vpc: Optional[str],
    barcode: Optional[str] = None,
) -> Optional[VendorItem]:
    q = db.query(VendorItem).filter(VendorItem.vendor_id == vendor_id)
    if barcode:
        hit = db.query(VendorItem).filter(VendorItem.barcode == barcode).first()
        if hit:
            return hit
    if erp:
        hit = q.filter(VendorItem.erp_code == erp).first()
        if hit:
            return hit
    if vpc:
        hit = q.filter(VendorItem.vendor_product_code == vpc).first()
        if hit:
            return hit
    return q.filter(func.lower(VendorItem.item_name) == item_name.lower()).first()


def _resolve_line_uom(
    raw_uom: Optional[str],
    *,
    rate_contract: bool,
) -> UomResolveResult:
    """
    Match OCR UOM to Foundation ``global_parameter`` (type=UOM).

    Known codes/descriptions are canonicalized; unknown UOMs keep the OCR
    value and set ``needs_confirm`` so the user can confirm (like product aliases).
    """
    text = _clean_str(raw_uom) or ""
    if not rate_contract or not text:
        code = (_clip(text, 3) or "").upper() if text else ""
        return UomResolveResult(
            ocr_value=text,
            canonical_code=code or None,
            description=text or None,
            matched=False,
        )
    return resolve_uom_from_global_parameter(text)


def _vendor_item_review_option(item: VendorItem) -> Dict[str, Any]:
    return {
        "vendorItemId": int(item.id) if item.id is not None else None,
        "itemId": int(item.item_id)
        if item.item_id is not None
        else (int(item.id) if item.id is not None else None),
        "productName": _clean_str(item.product_name) or _clean_str(item.item_name),
        "itemName": _clean_str(item.item_name),
        "uom": _clean_str(item.uom),
        "productFingerprint": item.product_fingerprint
        if isinstance(item.product_fingerprint, dict)
        else None,
    }


def _uom_review_extra(uom_result: UomResolveResult) -> Dict[str, Any]:
    return {
        "uomNeedsConfirm": bool(uom_result.needs_confirm),
        "uomMatched": bool(uom_result.matched),
        "ocrUom": uom_result.ocr_value or None,
        "suggestedUomCode": uom_result.canonical_code,
        "uomDescription": uom_result.description,
    }


def _enrich_row_with_uom(
    row: Dict[str, Any],
    fingerprint: Optional[Dict[str, Any]],
    uom_result: UomResolveResult,
    *,
    resolved_uom: str,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Stamp OCR line + fingerprint with resolved UOM + confirm metadata."""
    code = (
        (uom_result.canonical_code or _clip(resolved_uom, 3) or "").upper() or None
    )
    enriched = {
        **row,
        "uom": resolved_uom or None,
        "ocr_uom": uom_result.ocr_value or None,
        "uom_p": code,
        "uomNeedsConfirm": bool(uom_result.needs_confirm),
        "suggestedUomCode": code,
    }
    fp = dict(fingerprint) if isinstance(fingerprint, dict) else None
    if fp is not None:
        fp["uom"] = resolved_uom or fp.get("uom")
        fp["ocr_uom"] = uom_result.ocr_value or None
        fp["uomNeedsConfirm"] = bool(uom_result.needs_confirm)
        fp["suggestedUomCode"] = code
    return enriched, fp


def resolve_canonical_pack_size_for_item(item: VendorItem) -> Optional[str]:
    """Resolve canonical pack size from a vendor item's stored pack/UOM fields."""
    for candidate in (
        item.pack_size,
        item.consumer_unit,
        item.consumer_unit_size,
        item.uom,
        item.purchase_unit,
    ):
        raw = _clean_str(candidate)
        if raw:
            return resolve_pack_size_canonical(raw)
    return None


def apply_canonical_pack_size_to_item(item: VendorItem) -> Optional[str]:
    """Canonicalize and persist pack size on a vendor item; returns canonical value."""
    canonical = resolve_canonical_pack_size_for_item(item)
    if canonical:
        _apply_canonical_pack_size(item, canonical)
    return canonical


def assert_unique_vendor_item_identity(
    db: Session,
    *,
    vendor_id: int,
    item_id: Optional[int],
    vendor_code: Optional[str],
    product_name: str,
    canonical_pack_size: Optional[str],
) -> Optional[VendorItem]:
    """
    Look up an existing item by product identity.

    - If another item matches (and differs from ``item_id``), raise 409.
    - Returns the matched item when it is the same ``item_id``, or None when no match.
    """
    matched = find_existing_vendor_item_by_identity(
        db,
        vendor_id=vendor_id,
        vendor_code=vendor_code,
        product_name=product_name,
        canonical_pack_size=canonical_pack_size,
    )
    if matched is not None and (item_id is None or matched.id != item_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A product with the same vendor_code, pack_size, and product_name "
                f"already exists (vendor_item_id={matched.id})"
            ),
        )
    return matched


def _update_existing_vendor_item(
    db_item: VendorItem,
    *,
    name: str,
    product_name: str,
    itype: str,
    uom: str,
    desc_str: Optional[str],
    erp: Optional[str],
    vpc: Optional[str],
    barcode: Optional[str],
    fingerprint: Optional[Dict[str, Any]],
    buyer_item_id: Optional[int],
    vendor_code: Optional[str],
    row: Dict[str, Any],
    canonical_pack_size: Optional[str],
    overwrite_attrs: bool,
) -> None:
    if overwrite_attrs:
        if name:
            db_item.item_name = name
        if product_name:
            db_item.product_name = product_name
        if itype:
            db_item.item_type = itype
        if uom:
            db_item.uom = uom
        if desc_str:
            db_item.description = desc_str
        if erp:
            db_item.erp_code = erp
        if vpc:
            db_item.vendor_product_code = vpc
        if barcode:
            db_item.barcode = barcode
        if fingerprint:
            db_item.product_fingerprint = fingerprint
    else:
        if not db_item.description and desc_str:
            db_item.description = desc_str
        if not (db_item.uom or "").strip() and uom:
            db_item.uom = uom
        if erp and not (db_item.erp_code or "").strip():
            db_item.erp_code = erp
        if vpc and not (db_item.vendor_product_code or "").strip():
            db_item.vendor_product_code = vpc
        if barcode and not (db_item.barcode or "").strip():
            db_item.barcode = barcode
        if fingerprint and not db_item.product_fingerprint:
            db_item.product_fingerprint = fingerprint
        if not (db_item.product_name or "").strip() and product_name:
            db_item.product_name = product_name

    if buyer_item_id is not None and (overwrite_attrs or db_item.item_id is None):
        db_item.item_id = buyer_item_id
    if db_item.item_id is None and db_item.id is not None:
        db_item.item_id = int(db_item.id)
    if vendor_code and (overwrite_attrs or not db_item.vendor_code):
        db_item.vendor_code = vendor_code

    _apply_product_attrs(
        db_item,
        row,
        overwrite=overwrite_attrs,
        canonical_pack_size=canonical_pack_size,
    )


def _ocr_line_for_pending_review(
    row: Dict[str, Any],
    fingerprint: Optional[Dict[str, Any]],
    *,
    match_review: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = dict(row)
    fp = dict(fingerprint) if isinstance(fingerprint, dict) else {}
    if isinstance(match_review, dict):
        fp["matchReview"] = match_review
    if fp:
        payload["product_fingerprint"] = fp
    return payload


def insert_ocr_items_and_rates(
    db: Session,
    vendor_id: int,
    ocr_payload: Dict[str, Any],
    *,
    sync_item_fingerprints: bool = False,
    tenant_id: int = 1,
    company_id: int = 1,
    department_id: int = 1,
    created_by: Optional[int] = None,
) -> Tuple[int, int, int, List[Dict[str, Any]]]:
    """
    For each OCR item line: reuse existing vendor_items when matched; otherwise
    insert a new PENDING row without generated codes (see
    ``assign_vendor_item_master_codes``, run on confirm). Adds a vendor_rates
    row when a numeric rate is present.

    When ``sync_item_fingerprints`` is True (rate contract uploads):
      - match by vendor_code + canonical pack_size + product_name embedding
      - update existing product when matched, else insert
      - canonicalize pack_size via Foundation OCR alias dictionary
      - vendor_code is always taken from the logged-in / upload-target vendor
        account (never from OCR line text)
      - product_fingerprint is extracted per product via OpenAI
        (``parse_fingerprint_with_openai``) and stored as a complete JSON object
        on vendor_items.product_fingerprint (all standard fingerprint keys)
      - category / manufacturer / brand / uom / pack_size are mandatory on
        product_fingerprint (from OpenAI); vendor_code from logged-in vendor
      - those fingerprint fields populate vendor_items columns
      - upsert vendor_item_fingerprint and item_embeddings

    Returns (new_vendor_items_count, new_vendor_rates_count, fingerprints_synced_count, pending_match_reviews).
    """
    items: List[Any] = ocr_payload.get("items") or []
    if not isinstance(items, list):
        return (0, 0, 0, [])

    items_inserted = 0
    rates_inserted = 0
    fingerprints_synced = 0
    pending_match_reviews: List[Dict[str, Any]] = []
    session_inserted_vendor_item_ids: set[int] = set()

    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    # Rate contracts: vendor_code is mandatory on product_fingerprint and always
    # comes from the logged-in / upload-target vendor account — not OCR text.
    vendor_code = (
        _resolve_logged_in_vendor_code(vendor, vendor_id)
        if sync_item_fingerprints
        else (_clean_str(getattr(vendor, "vendor_code", None) if vendor else None))
    )
    # Persist a real vendors.vendor_code when missing so VendorItem FK can succeed.
    if (
        sync_item_fingerprints
        and vendor is not None
        and not _clean_str(getattr(vendor, "vendor_code", None))
        and vendor_code
    ):
        vendor.vendor_code = vendor_code
        db.add(vendor)
        db.flush()

    for idx, row in enumerate(items, start=1):
        if not isinstance(row, dict):
            continue
        name = _resolve_item_name(row)
        if not name:
            continue

        product_name = _resolve_product_name(row, name)
        itype = _clip(_clean_str(row.get("item_type")) or "Service", 50) or "Service"
        uom = _clip(_clean_str(row.get("uom")) or "", 50) or ""
        desc = row.get("description")
        desc_str = None if desc is None else str(desc).strip() or None
        if not desc_str:
            desc_str = name

        erp = _clip(_clean_str(row.get("erp_code")), 20)
        # Codes are only generated when the user confirms the item.
        vpc = _clip(_clean_str(row.get("vendor_product_code")), 20)
        barcode = _clip(_clean_str(row.get("barcode")), 100)
        fingerprint = row.get("product_fingerprint")
        if fingerprint is not None and not isinstance(fingerprint, dict):
            fingerprint = None
        buyer_item_id = row.get("item_id")
        if buyer_item_id is not None and buyer_item_id != "":
            try:
                buyer_item_id = int(buyer_item_id)
            except (TypeError, ValueError):
                buyer_item_id = None
        else:
            buyer_item_id = None

        # Rate contracts always canonicalize pack size for match + insert.
        canonical_pack = (
            _canonical_pack_size(row) if sync_item_fingerprints else None
        )
        if sync_item_fingerprints and canonical_pack:
            row = {**row, "pack_size": canonical_pack}
        if sync_item_fingerprints and vendor_code:
            # Override any OCR-extracted vendor_code with the account value.
            row = {**row, "vendor_code": vendor_code}
            if isinstance(fingerprint, dict):
                fingerprint = {**fingerprint, "vendor_code": vendor_code}

        if sync_item_fingerprints:
            # Extract product_fingerprint from OpenAI once per rate-contract line
            # before insert/update so identity fields can populate vendor_items.
            fingerprint = _extract_rate_contract_fingerprint_with_openai(
                row=row,
                vendor_code=vendor_code,
                canonical_pack_size=canonical_pack,
                uom=uom or None,
                product_name=product_name,
                description=desc_str,
                line_index=idx,
            )
            row = _apply_fingerprint_fields_to_row(row, fingerprint)
            fp_uom = _clip(_clean_str(fingerprint.get("uom")), 50)
            if fp_uom:
                uom = fp_uom
            # Keep OCR/display product_name for identity matching. OpenAI
            # normalized_name (e.g. "WHEAT FLOUR") must not replace names like
            # "Aashirvaad"/"Aashirvaad Wheat Flour" or re-uploads insert duplicates.
            fp_name = _clean_str(fingerprint.get("normalized_name"))
            if fp_name and not _clean_str(product_name):
                product_name = _clip(fp_name, 255) or fp_name
            if not _clean_str(name):
                name = product_name or fp_name or name
            if not canonical_pack and _clean_str(fingerprint.get("pack_size")):
                row = {**row, "pack_size": fingerprint.get("pack_size")}
                canonical_pack = _canonical_pack_size(row)
                if canonical_pack:
                    row = {**row, "pack_size": canonical_pack}
                    fingerprint = {**fingerprint, "pack_size": canonical_pack}
            # Mandatory identity fields must exist on product_fingerprint.
            _require_fingerprint_mandatory_fields(fingerprint, line_index=idx)

            # Match UOM against Foundation global_parameter (type=UOM), same
            # spirit as pack-size / product-name alias resolution.
            uom_result = _resolve_line_uom(uom, rate_contract=True)
            if uom_result.matched and uom_result.canonical_code:
                uom = uom_result.canonical_code
            row, fingerprint = _enrich_row_with_uom(
                row,
                fingerprint,
                uom_result,
                resolved_uom=uom,
            )
        else:
            code = (_clip(uom, 3) or "").upper() if uom else ""
            uom_result = UomResolveResult(
                ocr_value=uom,
                canonical_code=code or None,
                description=uom or None,
                matched=False,
            )

        if sync_item_fingerprints:
            alternate_names: List[str] = []
            if isinstance(fingerprint, dict):
                for key in ("brand", "normalized_name", "generic"):
                    value = _clean_str(fingerprint.get(key))
                    if value:
                        alternate_names.append(value)
                brand = _clean_str(fingerprint.get("brand"))
                normalized = _clean_str(fingerprint.get("normalized_name"))
                if brand and normalized:
                    alternate_names.append(f"{brand} {normalized}")
            if name and name != product_name:
                alternate_names.append(name)

            match_result = match_rate_contract_line(
                db,
                vendor_id=vendor_id,
                vendor_code=vendor_code,
                product_name=product_name,
                canonical_pack_size=canonical_pack,
                alternate_names=alternate_names,
                fingerprint=fingerprint,
                product_code=erp or vpc,
                exclude_vendor_item_ids=session_inserted_vendor_item_ids,
            )
            if match_result.needs_review:
                review = dict(match_result.match_review or {})
                review.update(_uom_review_extra(uom_result))
                line_status = (
                    str(review.get("lineStatus") or LINE_STATUS_PENDING_SELECT)
                    .strip()
                    .upper()
                )
                if line_status == LINE_STATUS_NO_MATCH:
                    if uom_result.needs_confirm:
                        logger.info(
                            "[RATE CONTRACT] no catalog match + unknown UOM line=%s "
                            "ocr_uom=%r — deferring for confirm",
                            idx,
                            uom_result.ocr_value,
                        )
                        pending_match_reviews.append(
                            {
                                "lineIndex": idx,
                                "productName": product_name,
                                "packSize": canonical_pack,
                                "lineStatus": LINE_STATUS_NO_MATCH,
                                "matchReview": review,
                                "uomNeedsConfirm": True,
                                "ocrUom": uom_result.ocr_value or None,
                                "suggestedUomCode": uom_result.canonical_code,
                                "ocrLine": _ocr_line_for_pending_review(
                                    row,
                                    fingerprint,
                                    match_review=review,
                                ),
                            }
                        )
                        continue
                    logger.info(
                        "[RATE CONTRACT] no catalog match line=%s product_name=%r "
                        "— inserting as new product (PENDING)",
                        idx,
                        product_name,
                    )
                    db_item = None
                else:
                    logger.info(
                        "[RATE CONTRACT] pending review line=%s product_name=%r "
                        "candidates=%s uom_needs_confirm=%s",
                        idx,
                        product_name,
                        len(match_result.ranked_candidates),
                        uom_result.needs_confirm,
                    )
                    pending_match_reviews.append(
                        {
                            "lineIndex": idx,
                            "productName": product_name,
                            "packSize": canonical_pack,
                            "lineStatus": review.get("lineStatus")
                            or LINE_STATUS_PENDING_SELECT,
                            "matchReview": review,
                            "uomNeedsConfirm": bool(uom_result.needs_confirm),
                            "ocrUom": uom_result.ocr_value or None,
                            "suggestedUomCode": uom_result.canonical_code,
                            "ocrLine": _ocr_line_for_pending_review(
                                row,
                                fingerprint,
                                match_review=review,
                            ),
                        }
                    )
                    continue
            else:
                db_item = (
                    match_result.vendor_item if match_result.matched else None
                )
                # Unique product match but unknown UOM → ask user to confirm
                # (creates global_parameter UOM on confirm, like product aliases).
                if db_item is not None and uom_result.needs_confirm:
                    option = _vendor_item_review_option(db_item)
                    review = build_match_review(
                        product_name=product_name,
                        match_type=match_result.match_type or MATCH_TYPE_EXACT,
                        line_status=LINE_STATUS_PENDING_CONFIRM,
                        match_color=MATCH_COLOR_GREEN,
                        suggested=option,
                        ranked_candidates=[option],
                        extra={
                            "matchedFields": list(
                                (match_result.details or {}).get("matchedFields")
                                or (match_result.details or {}).get("matched_fields")
                                or []
                            )
                            + ["uom_confirm"],
                            **_uom_review_extra(uom_result),
                        },
                    )
                    logger.info(
                        "[RATE CONTRACT] UOM confirm required line=%s product_name=%r "
                        "ocr_uom=%r suggested_code=%r vendor_item_id=%s",
                        idx,
                        product_name,
                        uom_result.ocr_value,
                        uom_result.canonical_code,
                        db_item.id,
                    )
                    pending_match_reviews.append(
                        {
                            "lineIndex": idx,
                            "productName": product_name,
                            "packSize": canonical_pack,
                            "lineStatus": LINE_STATUS_PENDING_CONFIRM,
                            "matchReview": review,
                            "uomNeedsConfirm": True,
                            "ocrUom": uom_result.ocr_value or None,
                            "suggestedUomCode": uom_result.canonical_code,
                            "ocrLine": _ocr_line_for_pending_review(
                                row,
                                fingerprint,
                                match_review=review,
                            ),
                        }
                    )
                    continue
                if db_item is not None:
                    logger.info(
                        "[RATE CONTRACT] existing item matched vendor_item_id=%s "
                        "product_name=%r pack=%r match_type=%s",
                        db_item.id,
                        product_name,
                        canonical_pack,
                        match_result.match_type,
                    )
        else:
            db_item = _find_existing_vendor_item(
                db, vendor_id, name, erp, vpc, barcode
            )
            if db_item is not None and (erp or vpc):
                catalog_name = (
                    _clean_str(db_item.product_name)
                    or _clean_str(db_item.item_name)
                    or name
                )
                product_code = erp or vpc
                _, ok, _, _ = evaluate_product_code_embedding_gate(
                    product_name=product_name or name,
                    product_code=product_code,
                    candidate_name=catalog_name or "",
                    candidate_codes=[
                        c
                        for c in (
                            db_item.erp_code,
                            db_item.vendor_product_code,
                            db_item.barcode,
                            db_item.company_code,
                        )
                        if _clean_str(c)
                    ],
                    log_context="vendor item import",
                )
                if not ok:
                    logger.info(
                        "[VENDOR ITEM IMPORT] reject code match vendor_item_id=%s "
                        "product_name=%r code=%r — embedding mismatch",
                        db_item.id,
                        product_name or name,
                        erp or vpc,
                    )
                    db_item = None

        if db_item is None:
            # ERP item / Foundation product (erp_code, company_code) and
            # vendor_product_code are created on confirm, not on upload.
            db_item = VendorItem(
                vendor_id=vendor_id,
                vendor_code=vendor_code,
                item_name=name,
                item_type=itype,
                uom=uom,
                description=desc_str,
                erp_code=erp,
                company_code=None,
                vendor_product_code=vpc,
                barcode=barcode,
                product_fingerprint=fingerprint,
                product_name=product_name,
                item_id=buyer_item_id,
                status=VENDOR_ITEM_STATUS_PENDING,
            )
            _apply_product_attrs(
                db_item,
                row,
                overwrite=True,
                canonical_pack_size=canonical_pack,
            )
            _fill_vendor_item_required_fields(db_item, vendor_code=vendor_code)
            if sync_item_fingerprints and isinstance(fingerprint, dict):
                _fill_vendor_item_from_fingerprint(
                    db_item,
                    fingerprint,
                    canonical_pack_size=canonical_pack
                    or _clean_str(fingerprint.get("pack_size")),
                )
            db.add(db_item)
            db.flush()
            # item_id mirrors vendor_items.id when OCR did not supply one.
            if db_item.item_id is None and db_item.id is not None:
                db_item.item_id = int(db_item.id)
            if db_item.id is not None:
                session_inserted_vendor_item_ids.add(int(db_item.id))
            items_inserted += 1
        else:
            _update_existing_vendor_item(
                db_item,
                name=name,
                product_name=product_name,
                itype=itype,
                uom=uom,
                desc_str=desc_str,
                erp=erp,
                vpc=vpc,
                barcode=barcode,
                fingerprint=fingerprint,
                buyer_item_id=buyer_item_id,
                vendor_code=vendor_code,
                row=row,
                canonical_pack_size=canonical_pack,
                overwrite_attrs=sync_item_fingerprints,
            )
            _fill_vendor_item_required_fields(db_item, vendor_code=vendor_code)
            if sync_item_fingerprints and isinstance(fingerprint, dict):
                _fill_vendor_item_from_fingerprint(
                    db_item,
                    fingerprint,
                    canonical_pack_size=canonical_pack
                    or _clean_str(fingerprint.get("pack_size")),
                )
            if sync_item_fingerprints:
                db_item.status = VENDOR_ITEM_STATUS_PENDING

        if sync_item_fingerprints:
            # Finalize against the persisted vendor item (no second OpenAI call).
            fingerprint = _finalize_rate_contract_product_fingerprint(
                db_item,
                fingerprint=fingerprint or {},
                vendor_code=vendor_code,
                canonical_pack_size=canonical_pack,
            )
            _require_fingerprint_mandatory_fields(fingerprint, line_index=idx)
            # Full OpenAI fingerprint JSON on vendor_items.product_fingerprint.
            db_item.product_fingerprint = fingerprint
            # Fill vendor_items columns from product_fingerprint (mandatory fields).
            _fill_vendor_item_from_fingerprint(
                db_item,
                fingerprint,
                canonical_pack_size=canonical_pack
                or _clean_str(fingerprint.get("pack_size")),
            )
            _apply_product_attrs(
                db_item,
                _apply_fingerprint_fields_to_row(row, fingerprint),
                overwrite=True,
                canonical_pack_size=canonical_pack
                or _clean_str(fingerprint.get("pack_size")),
            )
            try:
                fingerprint_row = sync_vendor_item_fingerprint(
                    db,
                    db_item,
                    fingerprint,
                )
                if fingerprint_row is not None:
                    fingerprints_synced += 1
            except Exception as exc:
                logger.exception(
                    "vendor_item_fingerprint sync failed for vendor_item_id=%s: %s",
                    db_item.id,
                    exc,
                )
            try:
                sync_vendor_item_to_item_embeddings(
                    db_item,
                    vendor=vendor,
                    product_fingerprint=fingerprint,
                )
            except Exception as exc:
                logger.exception(
                    "item_embeddings sync failed for vendor_item_id=%s: %s",
                    db_item.id,
                    exc,
                )

        rate_val = _parse_rate(row.get("rate"))
        if rate_val is None:
            continue

        vf = _parse_date(row.get("valid_from"))
        vt = _parse_date(row.get("valid_to"))
        cur = _clip(_clean_str(row.get("currency")) or "INR", 10) or "INR"

        db_rate = VendorRate(
            item_id=db_item.id,
            vendor_id=vendor_id,
            rate=rate_val,
            currency=cur,
            valid_from=vf,
            valid_to=vt,
            status="pending",
        )
        db.add(db_rate)
        rates_inserted += 1

    return (items_inserted, rates_inserted, fingerprints_synced, pending_match_reviews)


def apply_rate_contract_line_confirm(
    db: Session,
    vendor_id: int,
    row: Dict[str, Any],
    *,
    existing_vendor_item_id: Optional[int] = None,
) -> Tuple[VendorItem, bool, int]:
    """
    Persist one rate-contract OCR line after user confirms a match choice.

    When ``existing_vendor_item_id`` is set, update that vendor item; otherwise
    insert a new row. Adds a vendor_rates row when a numeric rate is present.

    Returns (vendor_item, was_inserted, fingerprints_synced_count).
    """
    if not isinstance(row, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OCR line payload",
        )

    name = _resolve_item_name(row)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OCR line is missing item name",
        )

    product_name = _resolve_product_name(row, name)
    itype = _clip(_clean_str(row.get("item_type")) or "Service", 50) or "Service"
    uom = _clip(_clean_str(row.get("uom")) or "", 50) or ""
    desc = row.get("description")
    desc_str = None if desc is None else str(desc).strip() or None
    if not desc_str:
        desc_str = name

    erp = _clip(_clean_str(row.get("erp_code")), 20)
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    vpc = _clip(_clean_str(row.get("vendor_product_code")), 20)
    barcode = _clip(_clean_str(row.get("barcode")), 100)
    fingerprint = row.get("product_fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, dict):
        fingerprint = None
    buyer_item_id = row.get("item_id")
    if buyer_item_id is not None and buyer_item_id != "":
        try:
            buyer_item_id = int(buyer_item_id)
        except (TypeError, ValueError):
            buyer_item_id = None
    else:
        buyer_item_id = None

    vendor_code = _resolve_logged_in_vendor_code(vendor, vendor_id)
    canonical_pack = _canonical_pack_size(row)
    if canonical_pack:
        row = {**row, "pack_size": canonical_pack}
    if vendor_code:
        row = {**row, "vendor_code": vendor_code}
        if isinstance(fingerprint, dict):
            fingerprint = {**fingerprint, "vendor_code": vendor_code}

    db_item: Optional[VendorItem] = None
    was_inserted = False
    linking_existing = existing_vendor_item_id is not None
    if linking_existing:
        db_item = (
            db.query(VendorItem)
            .filter(
                VendorItem.id == int(existing_vendor_item_id),
                VendorItem.vendor_id == vendor_id,
            )
            .first()
        )
        if db_item is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Selected vendor item not found for this vendor",
            )
        catalog_name = _clean_str(db_item.product_name) or _clean_str(db_item.item_name)
        if catalog_name:
            name = catalog_name
            product_name = catalog_name
        catalog_desc = _clean_str(db_item.description)
        if catalog_desc:
            desc_str = catalog_desc
        existing_fp = db_item.product_fingerprint
        if isinstance(existing_fp, dict) and existing_fp:
            fingerprint = dict(existing_fp)

    if not linking_existing and (
        not isinstance(fingerprint, dict) or not fingerprint
    ):
        fingerprint = _extract_rate_contract_fingerprint_with_openai(
            row=row,
            vendor_code=vendor_code,
            canonical_pack_size=canonical_pack,
            uom=uom or None,
            product_name=product_name,
            description=desc_str,
            line_index=1,
        )
        row = _apply_fingerprint_fields_to_row(row, fingerprint)
    if isinstance(fingerprint, dict) and fingerprint:
        _require_fingerprint_mandatory_fields(fingerprint, line_index=1)

    if db_item is None:
        db_item = VendorItem(
            vendor_id=vendor_id,
            vendor_code=vendor_code,
            item_name=name,
            item_type=itype,
            uom=uom,
            description=desc_str,
            erp_code=erp,
            company_code=None,
            vendor_product_code=vpc,
            barcode=barcode,
            product_fingerprint=fingerprint,
            product_name=product_name,
            item_id=buyer_item_id,
            status=VENDOR_ITEM_STATUS_PENDING,
        )
        _apply_product_attrs(
            db_item,
            row,
            overwrite=True,
            canonical_pack_size=canonical_pack,
        )
        _fill_vendor_item_required_fields(db_item, vendor_code=vendor_code)
        _fill_vendor_item_from_fingerprint(
            db_item,
            fingerprint,
            canonical_pack_size=canonical_pack
            or _clean_str(fingerprint.get("pack_size")),
        )
        db.add(db_item)
        db.flush()
        if db_item.item_id is None and db_item.id is not None:
            db_item.item_id = int(db_item.id)
        was_inserted = True
    else:
        _update_existing_vendor_item(
            db_item,
            name=name,
            product_name=product_name,
            itype=itype,
            uom=uom,
            desc_str=desc_str,
            erp=erp,
            vpc=vpc,
            barcode=barcode,
            fingerprint=fingerprint,
            buyer_item_id=buyer_item_id,
            vendor_code=vendor_code,
            row=row,
            canonical_pack_size=canonical_pack,
            overwrite_attrs=not linking_existing,
        )
        if linking_existing and not (db_item.uom or "").strip() and uom:
            db_item.uom = uom
        _fill_vendor_item_required_fields(db_item, vendor_code=vendor_code)
        if not linking_existing and isinstance(fingerprint, dict):
            _fill_vendor_item_from_fingerprint(
                db_item,
                fingerprint,
                canonical_pack_size=canonical_pack
                or _clean_str(fingerprint.get("pack_size")),
            )

    fingerprints_synced = 0
    if not linking_existing:
        fingerprint = _finalize_rate_contract_product_fingerprint(
            db_item,
            fingerprint=fingerprint or {},
            vendor_code=vendor_code,
            canonical_pack_size=canonical_pack,
        )
        if isinstance(fingerprint, dict) and fingerprint:
            _require_fingerprint_mandatory_fields(fingerprint, line_index=1)
    catalog_product_name = (
        _clean_str(db_item.product_name) or _clean_str(db_item.item_name) or product_name
    )
    db_item.status = VENDOR_ITEM_STATUS_CONFIRMED
    db_item.product_fingerprint = {
        **(fingerprint if isinstance(fingerprint, dict) else {}),
        "matchReview": {
            "matchType": "manual",
            "lineStatus": LINE_STATUS_CONFIRMED,
            "productName": catalog_product_name,
        },
    }
    if not linking_existing:
        _fill_vendor_item_from_fingerprint(
            db_item,
            db_item.product_fingerprint,
            canonical_pack_size=canonical_pack
            or _clean_str((db_item.product_fingerprint or {}).get("pack_size")),
        )
        _apply_product_attrs(
            db_item,
            _apply_fingerprint_fields_to_row(row, db_item.product_fingerprint),
            overwrite=True,
            canonical_pack_size=canonical_pack
            or _clean_str((db_item.product_fingerprint or {}).get("pack_size")),
        )
    # The user confirmed this line: generate any missing item codes now.
    assign_vendor_item_master_codes(db, db_item)
    try:
        fingerprint_row = sync_vendor_item_fingerprint(db, db_item, fingerprint)
        if fingerprint_row is not None:
            fingerprints_synced += 1
    except Exception as exc:
        logger.exception(
            "vendor_item_fingerprint sync failed for vendor_item_id=%s: %s",
            db_item.id,
            exc,
        )
    try:
        sync_vendor_item_to_item_embeddings(
            db_item,
            vendor=vendor,
            product_fingerprint=fingerprint,
        )
    except Exception as exc:
        logger.exception(
            "item_embeddings sync failed for vendor_item_id=%s: %s",
            db_item.id,
            exc,
        )

    rate_val = _parse_rate(row.get("rate"))
    if rate_val is not None:
        vf = _parse_date(row.get("valid_from"))
        vt = _parse_date(row.get("valid_to"))
        cur = _clip(_clean_str(row.get("currency")) or "INR", 10) or "INR"
        db_rate = VendorRate(
            item_id=db_item.id,
            vendor_id=vendor_id,
            rate=rate_val,
            currency=cur,
            valid_from=vf,
            valid_to=vt,
            status="pending",
        )
        db.add(db_rate)

    db.flush()
    return (db_item, was_inserted, fingerprints_synced)
