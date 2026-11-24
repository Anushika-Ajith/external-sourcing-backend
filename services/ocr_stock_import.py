"""
Parse stock documents (OCR/CSV/Excel) and upsert into:
  - stock (master — one row per product_code; available_stock overwritten)
  - stock_upload_header
  - stock_upload_detail
"""

from __future__ import annotations

import csv
import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import Stock, StockUploadDetail, StockUploadHeader, Vendor, VendorItem
from services.item_embedding_catalog import ItemEmbeddingMatch, match_stock_line_to_catalog
from services.product_matching_common import (
    collect_product_codes,
    evaluate_product_code_embedding_gate,
)

logger = logging.getLogger(__name__)

_PRODUCT_CODE_KEYS = {
    "product_code",
    "productcode",
    "product code",
    "sku",
    "item_code",
    "itemcode",
    "item code",
    "item_no",
    "itemno",
    "item no",
    "erp_code",
    "erpcode",
}
_BARCODE_KEYS = {"barcode", "bar_code", "ean", "upc"}
_PRODUCT_NAME_KEYS = {
    "product_name",
    "productname",
    "product name",
    "product",
    "item_name",
    "itemname",
    "item name",
    "description",
    "name",
}
_CATEGORY_KEYS = {"category", "product_category", "product category"}
_BRAND_KEYS = {"brand", "manufacturer"}
_UNIT_KEYS = {"unit", "uom", "unit_of_measure"}
# Prefer on-hand / current stock over contract / ordered qty when both appear.
_PREFERRED_QTY_KEYS = {
    "available_stock",
    "availablestock",
    "available stock",
    "current_stock",
    "currentstock",
    "current stock",
    "on_hand",
    "onhand",
    "on hand",
    "stock_qty",
    "stockqty",
    "stock qty",
}
_QTY_KEYS = {
    *_PREFERRED_QTY_KEYS,
    "qty",
    "quantity",
    "quantity_balance",
    "quantitybalance",
    "quantity balance",
    "qty_balance",
    "qtybalance",
    "stock",
    "balance",
}
_REORDER_LEVEL_KEYS = {"reorder_level", "reorderlevel", "reorder level", "min_stock"}
_REORDER_QTY_KEYS = {"reorder_qty", "reorderqty", "reorder qty", "reorder_quantity"}
_SUPPLIER_KEYS = {
    "preferred_supplier",
    "preferredsupplier",
    "preferred supplier",
    "supplier",
    "vendor",
}
_PRODUCT_ID_KEYS = {"product_id", "productid", "product id", "supplier_product_id"}


def _clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _clip(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    return s if len(s) <= max_len else s[:max_len]


def _normalize_header(val: Any) -> str:
    s = str(val or "").strip().lower()
    s = s.replace("-", "_").replace(" ", "_")
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def _pick(row: Dict[str, Any], keys: Set[str]) -> Any:
    """Return the first non-empty value whose key matches ``keys``.

    Skips null/blank values so a null ``item_name`` does not shadow a populated
    ``product_name`` (common in OCR vendor-item shaped payloads).
    """
    for key, value in row.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        norm = _normalize_header(key)
        spaced = norm.replace("_", " ")
        if norm in keys or spaced in keys or key.strip().lower() in keys:
            return value
    return None


def _parse_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        return int(Decimal(str(val).strip().replace(",", "")))
    except Exception:
        return None


def _parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None or val == "":
        return None
    try:
        return Decimal(str(val).strip().replace(",", ""))
    except Exception:
        return None


def _normalize_line(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    product_code = _clip(_clean_str(_pick(row, _PRODUCT_CODE_KEYS)), 50)
    barcode = _clip(_clean_str(_pick(row, _BARCODE_KEYS)), 50)
    product_name = _clip(_clean_str(_pick(row, _PRODUCT_NAME_KEYS)), 255)
    # Prefer Current Stock / available_stock over Contract Qty / quantity_balance.
    qty = _parse_decimal(_pick(row, _PREFERRED_QTY_KEYS))
    if qty is None:
        qty = _parse_decimal(_pick(row, _QTY_KEYS))
    supplier_product_id = _parse_int(_pick(row, _PRODUCT_ID_KEYS))

    if not product_name and product_code:
        product_name = product_code
    # Allow name-only lines — product_code may be resolved via item_embeddings.
    if not product_code and not product_name:
        return None
    if qty is None:
        qty = Decimal("0")

    return {
        "product_code": product_code,
        "barcode": barcode,
        "product_name": product_name,
        "category": _clip(_clean_str(_pick(row, _CATEGORY_KEYS)), 100),
        "brand": _clip(_clean_str(_pick(row, _BRAND_KEYS)), 100),
        "unit": _clip(_clean_str(_pick(row, _UNIT_KEYS)), 20),
        "available_stock": qty,
        "reorder_level": _parse_decimal(_pick(row, _REORDER_LEVEL_KEYS)) or Decimal("0"),
        "reorder_qty": _parse_decimal(_pick(row, _REORDER_QTY_KEYS)) or Decimal("0"),
        "preferred_supplier": _clip(_clean_str(_pick(row, _SUPPLIER_KEYS)), 150),
        "supplier_product_id": supplier_product_id,
        "needs_catalog_match": not bool(product_code) and supplier_product_id is None,
    }


def extract_stock_lines_from_payload(ocr_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Pull stock lines from Gemini / OCR structured JSON."""
    raw_lines: List[Any] = []

    for nest_key in ("stock", "inventory", "stock_report", "inventory_report"):
        block = ocr_payload.get(nest_key) if isinstance(ocr_payload, dict) else None
        if isinstance(block, dict):
            candidate = block.get("lines") or block.get("items") or block.get("rows")
            if isinstance(candidate, list):
                raw_lines = candidate
                break
        if isinstance(block, list):
            raw_lines = block
            break

    if not raw_lines and isinstance(ocr_payload.get("stock_lines"), list):
        raw_lines = ocr_payload["stock_lines"]
    if not raw_lines and isinstance(ocr_payload.get("lines"), list):
        raw_lines = ocr_payload["lines"]
    # OCR often puts stock qty on vendor-style items[] as quantity_balance
    # when the model mis-nests stock into the generic items schema.
    if not raw_lines and isinstance(ocr_payload.get("items"), list):
        raw_lines = ocr_payload["items"]

    lines: List[Dict[str, Any]] = []
    if not isinstance(raw_lines, list):
        return lines
    for row in raw_lines:
        if not isinstance(row, dict):
            continue
        # Map common OCR aliases onto stock field names before normalize.
        aliased = dict(row)
        if (
            aliased.get("available_stock") is None
            and aliased.get("quantity_balance") is not None
        ):
            aliased["available_stock"] = aliased.get("quantity_balance")
        if not _clean_str(aliased.get("product_name")):
            name = (
                _clean_str(aliased.get("item_name"))
                or _clean_str(aliased.get("description"))
            )
            if name:
                aliased["product_name"] = name
        if not _clean_str(aliased.get("product_code")):
            # Prefer vendor_product_code for stock.product_code; erp_code is ERP-only.
            code = (
                _clean_str(aliased.get("vendor_product_code"))
                or _clean_str(aliased.get("sku"))
                or _clean_str(aliased.get("erp_code"))
            )
            if code:
                aliased["product_code"] = code
        if not _clean_str(aliased.get("unit")) and _clean_str(aliased.get("uom")):
            aliased["unit"] = aliased.get("uom")
        normalized = _normalize_line(aliased)
        if normalized:
            lines.append(normalized)
    return lines


def parse_stock_lines_from_text(text: str) -> List[Dict[str, Any]]:
    """
    Fallback parser for whitespace/CSV-like stock tables in plain PDF text.

    Expected header tokens include product_code / product_name / available_stock
    / Current Stock. ReportLab-style single-space tables with multi-word product
    names are handled via a row regex when column splitting would misalign.
    """
    if not text or not str(text).strip():
        return []

    lines_out: List[Dict[str, Any]] = []
    raw_lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    header_idx = -1
    headers: List[str] = []

    # ReportLab stock reports: "1 Basmati Rice BAG 25 12 15 25 1,890.00 22,680.00"
    # Columns after product: UOM, Contract Qty, Current Stock, Reorder Level,
    # Reorder Qty, Unit Rate, Stock Value.
    stock_row_re = re.compile(
        r"^\s*(\d+)\s+"
        r"(.+?)\s+"
        r"([A-Za-z]{2,12})\s+"
        r"([\d,]+(?:\.\d+)?)\s+"
        r"([\d,]+(?:\.\d+)?)\s+"
        r"([\d,]+(?:\.\d+)?)\s+"
        r"([\d,]+(?:\.\d+)?)"
        r"(?:\s+[\d,]+(?:\.\d+)?){0,2}"
        r"\s*$"
    )

    for i, ln in enumerate(raw_lines):
        lower = ln.lower()
        compact = lower.replace(" ", "_")
        looks_like_stock_header = (
            "available_stock" in compact
            or "current_stock" in compact
            or ("available" in lower and "stock" in lower and "product" in lower)
            or ("current" in lower and "stock" in lower and "product" in lower)
            or (
                "product" in lower
                and "stock" in lower
                and ("reorder" in lower or "uom" in lower or "qty" in lower)
            )
        )
        if not looks_like_stock_header:
            continue

        # Prefer comma/tab split when present, else multi-space columns.
        if "," in ln or "\t" in ln:
            parts = re.split(r"[\t,;|]+", ln)
            headers = [_normalize_header(p) for p in parts if _clean_str(p)]
            headers = [re.sub(r"_*\([^)]*\)_*", "", h).strip("_") for h in headers]
            has_name = any(
                h in _PRODUCT_CODE_KEYS or h in _PRODUCT_NAME_KEYS for h in headers
            )
            has_qty = any(h in _QTY_KEYS or h in _PREFERRED_QTY_KEYS for h in headers)
            if has_name and has_qty:
                header_idx = i
                break
        # Single-space ReportLab headers (multi-word column titles) — use regex rows.
        header_idx = i
        headers = []
        break

    if header_idx < 0:
        return []

    # Regex path: Current Stock / Contract Qty style inventory tables.
    if not headers:
        for ln in raw_lines[header_idx + 1 :]:
            lower = ln.lower()
            if lower.startswith("page ") or lower.startswith("---"):
                continue
            if lower.startswith("total") or lower.startswith("reorder status"):
                break
            m = stock_row_re.match(ln)
            if not m:
                continue
            _sl, product_name, uom, _contract_qty, current_stock, reorder_level, reorder_qty = (
                m.group(1),
                m.group(2).strip(),
                m.group(3).strip(),
                m.group(4),
                m.group(5),
                m.group(6),
                m.group(7),
            )
            normalized = _normalize_line(
                {
                    "product_name": product_name,
                    "unit": uom,
                    "current_stock": current_stock,
                    "available_stock": current_stock,
                    "reorder_level": reorder_level,
                    "reorder_qty": reorder_qty,
                }
            )
            if normalized:
                lines_out.append(normalized)
        return lines_out

    for ln in raw_lines[header_idx + 1 :]:
        lower = ln.lower()
        if lower.startswith("page ") or lower.startswith("---"):
            continue
        if lower.startswith("total") or lower.startswith("reorder status"):
            break
        if "," in ln or "\t" in ln:
            parts = [p.strip() for p in re.split(r"[\t,;|]+", ln) if p.strip()]
        else:
            parts = [p.strip() for p in re.split(r"\s{2,}|\t+", ln) if p.strip()]
            if len(parts) < 2:
                parts = [p.strip() for p in ln.split() if p.strip()]
        if len(parts) < 2:
            continue
        # Align trailing extras into last column.
        if len(parts) > len(headers):
            parts = parts[: len(headers) - 1] + [" ".join(parts[len(headers) - 1 :])]
        while len(parts) < len(headers):
            parts.append("")
        row = {headers[i]: parts[i] for i in range(len(headers))}
        normalized = _normalize_line(row)
        if normalized:
            lines_out.append(normalized)
    return lines_out


def parse_stock_csv(path: str) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(path)

    with open(file_path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        for row in reader:
            if not isinstance(row, dict):
                continue
            if not any(_clean_str(v) for v in row.values()):
                continue
            normalized = _normalize_line(row)
            if normalized:
                lines.append(normalized)
    return lines


def parse_stock_tabular(path: str) -> List[Dict[str, Any]]:
    """Parse CSV or Excel stock sheet into normalized lines."""
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return parse_stock_csv(path)

    if suffix in {".xls", ".xlsx"}:
        try:
            import pandas as pd
        except ImportError as exc:
            raise RuntimeError("pandas is required to parse Excel stock files") from exc

        try:
            # openpyxl for .xlsx; xlrd for legacy .xls
            engine = "openpyxl" if suffix == ".xlsx" else None
            df = pd.read_excel(path, engine=engine)
        except ImportError as exc:
            missing = "openpyxl" if suffix == ".xlsx" else "xlrd/openpyxl"
            raise RuntimeError(
                f"Missing Excel dependency '{missing}'. "
                f"Install it in the backend image (pip install {missing})."
            ) from exc
        df = df.where(pd.notnull(df), None)
        lines: List[Dict[str, Any]] = []
        for record in df.to_dict(orient="records"):
            if not isinstance(record, dict):
                continue
            if not any(_clean_str(v) for v in record.values()):
                continue
            normalized = _normalize_line(record)
            if normalized:
                lines.append(normalized)
        return lines

    return []


def is_tabular_stock_path(path: str) -> bool:
    return Path(path).suffix.lower() in {".csv", ".xls", ".xlsx"}


def collect_stock_lines_from_files(
    saved_files: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    lines: List[Dict[str, Any]] = []
    scan_files: List[str] = []
    for path in saved_files:
        if is_tabular_stock_path(path):
            lines.extend(parse_stock_tabular(path))
        else:
            scan_files.append(path)
    return lines, scan_files


def _find_vendor_item_by_product_code(
    db: Session,
    product_code: str,
) -> Optional[VendorItem]:
    code = _clean_str(product_code)
    if not code:
        return None
    upper = code.upper()
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
        .first()
    )


def _find_vendor_item_by_product_name(
    db: Session,
    product_name: str,
) -> Optional[VendorItem]:
    """Exact case-insensitive match on vendor_items.product_name / item_name."""
    name = _clean_str(product_name)
    if not name:
        return None
    upper = name.upper()
    return (
        db.query(VendorItem)
        .filter(
            VendorItem.vendor_id.isnot(None),
            or_(
                func.upper(func.trim(VendorItem.product_name)) == upper,
                func.upper(func.trim(VendorItem.item_name)) == upper,
            ),
        )
        .order_by(VendorItem.id.desc())
        .first()
    )


def _vendor_item_product_codes(item: VendorItem) -> List[str]:
    return collect_product_codes(
        item.erp_code,
        item.company_code,
        item.vendor_product_code,
        item.barcode,
        str(item.id) if item.id is not None else None,
        str(item.item_id) if item.item_id is not None else None,
    )


def _product_code_matches_vendor_catalog(db: Session, product_code: str) -> bool:
    """True when code matches vendor_items erp_code, company_code, or vendor_product_code."""
    return _find_vendor_item_by_product_code(db, product_code) is not None


def _stock_product_code_from_vendor_item(vendor_item: VendorItem) -> Optional[str]:
    """Canonical stock.product_code is vendor_items.vendor_product_code."""
    return _clip(_clean_str(vendor_item.vendor_product_code), 50)


def _vendor_code_matches(vendor: Vendor, vendor_code: str) -> bool:
    code = _clean_str(vendor_code)
    if not code:
        return False
    upper = code.upper()
    for attr in ("vendor_code", "vendor_registration_no", "company_code", "company_name"):
        val = _clean_str(getattr(vendor, attr, None))
        if val and val.upper() == upper:
            return True
    if vendor.vendor_id is not None and upper == f"V{vendor.vendor_id}".upper():
        return True
    return False


def _preferred_supplier_from_vendor(db: Session, vendor_item: VendorItem) -> Optional[str]:
    vendor = getattr(vendor_item, "vendor", None)
    if vendor is None and vendor_item.vendor_id is not None:
        vendor = (
            db.query(Vendor)
            .filter(Vendor.vendor_id == vendor_item.vendor_id)
            .first()
        )
    if vendor is None:
        return None
    return _clip(_clean_str(vendor.company_name), 150)


def _apply_vendor_item_product_code(
    resolved: Dict[str, Any],
    vendor_item: VendorItem,
    *,
    db: Session,
) -> Dict[str, Any]:
    """
    Store vendor_product_code on stock rows and prefill preferred_supplier.

    stock.product_code must be vendor_items.vendor_product_code only.
    erp_code stays on vendor_items and is never written as stock.product_code.
    """
    out = dict(resolved)
    vendor_product_code = _stock_product_code_from_vendor_item(vendor_item)
    if vendor_product_code:
        out["product_code"] = vendor_product_code
    if not _clean_str(out.get("preferred_supplier")):
        supplier = _preferred_supplier_from_vendor(db, vendor_item)
        if supplier:
            out["preferred_supplier"] = supplier
    if vendor_item.id is not None:
        out["matched_vendor_item_id"] = int(vendor_item.id)
    return out


def _has_vendor_product_code(vendor_item: VendorItem) -> bool:
    return bool(_stock_product_code_from_vendor_item(vendor_item))


def _find_vendor_item_for_embedding_match(
    db: Session,
    match: ItemEmbeddingMatch,
) -> Optional[VendorItem]:
    """Resolve vendor_items row for an item_embeddings hit."""
    item_code = _clean_str(match.item_code)
    if not item_code:
        return None
    upper = item_code.upper()
    rows = (
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
    if not rows:
        return None
    vendor_code = _clean_str(match.vendor_code)
    if vendor_code and len(rows) > 1:
        for item in rows:
            vendor = (
                db.query(Vendor)
                .filter(Vendor.vendor_id == item.vendor_id)
                .first()
            )
            if vendor is not None and _vendor_code_matches(vendor, vendor_code):
                return item
    return rows[0]


def _apply_embedding_catalog_match(
    resolved: Dict[str, Any],
    *,
    db: Session,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve stock.product_code via item_embeddings → vendor_product_code only."""
    match = match_stock_line_to_catalog(resolved)
    if match is None:
        return None, "No matching item_embeddings catalog entry"

    vendor_item = _find_vendor_item_for_embedding_match(db, match)
    if vendor_item is None or not _has_vendor_product_code(vendor_item):
        return None, (
            "item_embeddings match has no vendor_items.vendor_product_code "
            f"(item_code={match.item_code!r})"
        )

    resolved = dict(resolved)
    if not _clean_str(resolved.get("product_name")):
        resolved["product_name"] = _clip(match.item_name, 255)
    if not _clean_str(resolved.get("preferred_supplier")):
        resolved["preferred_supplier"] = _clip(match.vendor_code, 150)
    resolved["matched_item_embedding_id"] = match.item_embedding_id
    resolved["catalog_match_distance"] = match.distance
    resolved = _apply_vendor_item_product_code(resolved, vendor_item, db=db)
    resolved["needs_catalog_match"] = False

    logger.info(
        "Stock line matched item_embeddings id=%s item_code=%s distance=%.4f name=%s product_code=%s",
        match.item_embedding_id,
        match.item_code,
        match.distance,
        resolved.get("product_name"),
        resolved.get("product_code"),
    )
    return resolved, None


def _resolve_line_product_identity(
    line: Dict[str, Any],
    *,
    db: Session,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Ensure line has stock.product_code = vendor_items.vendor_product_code.

    ERP codes may be used only to look up the vendor item; they are never stored
    as stock.product_code. Lines without a vendor_product_code are skipped.
    Returns (resolved_line, skip_reason).
    """
    resolved = dict(line)
    product_code = _clean_str(resolved.get("product_code"))
    product_name = _clean_str(resolved.get("product_name"))
    has_code = bool(product_code)
    has_product_id = resolved.get("supplier_product_id") is not None

    if has_product_id:
        vendor_item = (
            db.query(VendorItem)
            .filter(VendorItem.id == int(resolved["supplier_product_id"]))
            .first()
        )
        if vendor_item is not None and _has_vendor_product_code(vendor_item):
            resolved = _apply_vendor_item_product_code(resolved, vendor_item, db=db)
            if not _clean_str(resolved.get("product_name")):
                resolved["product_name"] = (
                    _clean_str(vendor_item.product_name)
                    or _clean_str(vendor_item.item_name)
                    or resolved["product_code"]
                )
            resolved["needs_catalog_match"] = False
            return resolved, None
        return None, (
            "product_id has no vendor_items.vendor_product_code "
            f"(product_id={resolved['supplier_product_id']})"
        )

    if has_code:
        if not product_name:
            resolved["product_name"] = product_code
            product_name = product_code
        if _product_code_matches_vendor_catalog(db, product_code):
            vendor_item = _find_vendor_item_by_product_code(db, product_code)
            if vendor_item is not None:
                catalog_name = (
                    _clean_str(vendor_item.product_name)
                    or _clean_str(vendor_item.item_name)
                    or product_code
                )
                _, ok, _, stop_reason = evaluate_product_code_embedding_gate(
                    product_name=resolved.get("product_name") or product_code,
                    product_code=product_code,
                    candidate_name=catalog_name or "",
                    candidate_codes=_vendor_item_product_codes(vendor_item),
                    log_context="stock upload",
                )
                if ok and _has_vendor_product_code(vendor_item):
                    resolved = _apply_vendor_item_product_code(
                        resolved, vendor_item, db=db
                    )
                    resolved["needs_catalog_match"] = False
                    return resolved, None
                if ok and not _has_vendor_product_code(vendor_item):
                    logger.info(
                        "Stock line code=%r name=%r matched vendor_item_id=%s "
                        "but vendor_product_code is missing — skipping ERP/VI fallback",
                        product_code,
                        resolved.get("product_name"),
                        vendor_item.id,
                    )
                    return None, (
                        "matched vendor_item has no vendor_product_code "
                        f"(vendor_item_id={vendor_item.id})"
                    )
                logger.info(
                    "Stock line code=%r name=%r matched vendor_item_id=%s "
                    "but embedding gate failed: %s — trying catalog embedding match",
                    product_code,
                    resolved.get("product_name"),
                    vendor_item.id,
                    stop_reason,
                )

    # Prefer exact vendor_items name match before vector search — rate-contract
    # products often appear on stock docs by name without a printed SKU.
    if product_name:
        vendor_item = _find_vendor_item_by_product_name(db, product_name)
        if vendor_item is not None:
            if _has_vendor_product_code(vendor_item):
                resolved = _apply_vendor_item_product_code(
                    resolved, vendor_item, db=db
                )
                resolved["needs_catalog_match"] = False
                logger.info(
                    "Stock line name=%r matched vendor_item_id=%s product_code=%r",
                    product_name,
                    vendor_item.id,
                    resolved.get("product_code"),
                )
                return resolved, None
            logger.info(
                "Stock line name=%r matched vendor_item_id=%s "
                "but vendor_product_code is missing — skipping ERP/VI fallback",
                product_name,
                vendor_item.id,
            )
            return None, (
                "matched vendor_item has no vendor_product_code "
                f"(vendor_item_id={vendor_item.id})"
            )

    matched, skip_reason = _apply_embedding_catalog_match(resolved, db=db)
    if matched is not None:
        return matched, None

    if has_code:
        return None, (
            skip_reason
            or "product_code not in vendor catalog and no matching item_embeddings entry"
        )
    return None, (
        skip_reason
        or "No product_code/product_id and no matching vendor_items / item_embeddings entry"
    )


def _upsert_stock_master(
    db: Session,
    line: Dict[str, Any],
    *,
    tenant_id: int,
    company_id: int,
    user_id: Optional[int],
) -> Tuple[Stock, bool]:
    """
    One master row per product_code.
    On match: overwrite available_stock (and refresh descriptive fields).
    """
    product_code = line["product_code"]
    existing = (
        db.query(Stock)
        .filter(Stock.product_code == product_code)
        .first()
    )

    if existing is None:
        row = Stock(
            product_code=product_code,
            barcode=line.get("barcode"),
            product_name=line["product_name"],
            category=line.get("category"),
            brand=line.get("brand"),
            unit=line.get("unit"),
            available_stock=line["available_stock"],
            reorder_level=line.get("reorder_level") or Decimal("0"),
            reorder_qty=line.get("reorder_qty") or Decimal("0"),
            preferred_supplier=line.get("preferred_supplier"),
            tenant_id=tenant_id,
            company_id=company_id,
            created_by=user_id,
            updated_by=user_id,
        )
        db.add(row)
        db.flush()
        return row, True

    # Overwrite quantity on re-upload; keep one master row.
    existing.available_stock = line["available_stock"]
    existing.product_name = line["product_name"] or existing.product_name
    if line.get("barcode"):
        existing.barcode = line["barcode"]
    if line.get("category") is not None:
        existing.category = line["category"]
    if line.get("brand") is not None:
        existing.brand = line["brand"]
    if line.get("unit") is not None:
        existing.unit = line["unit"]
    if line.get("reorder_level") is not None:
        existing.reorder_level = line["reorder_level"]
    if line.get("reorder_qty") is not None:
        existing.reorder_qty = line["reorder_qty"]
    if line.get("preferred_supplier") is not None:
        existing.preferred_supplier = line["preferred_supplier"]
    existing.tenant_id = tenant_id
    existing.company_id = company_id
    existing.updated_by = user_id
    db.flush()
    return existing, False


def import_stock_lines(
    db: Session,
    lines: List[Dict[str, Any]],
    *,
    tenant_id: int = 1,
    company_id: int = 3,
    document_no: str,
    upload_source: str = "WEB",
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create upload header + detail rows and upsert stock master.
    available_stock on stock is overwritten for existing product_code.

    Lines without product_code/product_id, or with a product_code that does not
    match vendor_items, are resolved via VECTOR_DB.item_embeddings (rate-contract
    catalog) using embedding similarity.
    """
    header = StockUploadHeader(
        tenant_id=tenant_id,
        company_id=company_id,
        document_no=_clip(document_no, 30) or f"STOCK-{created_by or 0}",
        upload_source=_clip(upload_source, 20) or "WEB",
        status="PROCESSING",
        created_by=created_by,
        updated_by=created_by,
    )
    db.add(header)
    db.flush()

    if not lines:
        header.status = "COMPLETED"
        db.flush()
        return {
            "stock_upload_hdr_id": header.stock_upload_hdr_id,
            "document_no": header.document_no,
            "status": header.status,
            "inserted_details": 0,
            "stock_created": 0,
            "stock_updated": 0,
            "catalog_matched": 0,
            "skipped": 0,
            "skipped_reasons": [],
            "detail_ids": [],
            "stock_ids": [],
        }

    detail_ids: List[int] = []
    stock_ids: List[int] = []
    created = 0
    updated = 0
    catalog_matched = 0
    skipped = 0
    skipped_reasons: List[Dict[str, Any]] = []

    # Resolve identity (including embedding match) before dedupe by product_code.
    resolved_lines: List[Dict[str, Any]] = []
    for idx, raw_line in enumerate(lines, start=1):
        resolved, skip_reason = _resolve_line_product_identity(raw_line, db=db)
        if resolved is None:
            skipped += 1
            skipped_reasons.append(
                {
                    "row": idx,
                    "product_code": raw_line.get("product_code"),
                    "product_name": raw_line.get("product_name"),
                    "reason": skip_reason or "Could not resolve product identity",
                }
            )
            continue
        if resolved.get("matched_item_embedding_id") is not None:
            catalog_matched += 1
        resolved_lines.append(resolved)

    # Last line wins for master qty when duplicates appear in the same file.
    deduped: Dict[str, Dict[str, Any]] = {}
    ordered_codes: List[str] = []
    for line in resolved_lines:
        code = line["product_code"]
        if code not in deduped:
            ordered_codes.append(code)
        deduped[code] = line

    for line_no, code in enumerate(ordered_codes, start=1):
        line = deduped[code]
        try:
            with db.begin_nested():
                stock_row, was_created = _upsert_stock_master(
                    db,
                    line,
                    tenant_id=tenant_id,
                    company_id=company_id,
                    user_id=created_by,
                )
                supplier_product_id = line.get("supplier_product_id") or int(
                    stock_row.stock_id
                )
                detail = StockUploadDetail(
                    tenant_id=tenant_id,
                    company_id=company_id,
                    stock_upload_hdr_id=header.stock_upload_hdr_id,
                    line_no=line_no,
                    supplier_product_id=int(supplier_product_id),
                    available_stock=line["available_stock"],
                    created_by=created_by,
                    updated_by=created_by,
                )
                db.add(detail)
                db.flush()
                detail_ids.append(int(detail.stock_upload_dtl_id))
                stock_ids.append(int(stock_row.stock_id))
                if was_created:
                    created += 1
                else:
                    updated += 1
        except Exception as exc:
            skipped += 1
            skipped_reasons.append(
                {
                    "row": line_no,
                    "product_code": code,
                    "reason": str(exc),
                }
            )
            logger.warning("Stock import skipped line %s (%s): %s", line_no, code, exc)

    header.status = "COMPLETED" if (created + updated) > 0 else "FAILED"
    header.updated_by = created_by
    db.flush()

    return {
        "stock_upload_hdr_id": header.stock_upload_hdr_id,
        "document_no": header.document_no,
        "status": header.status,
        "inserted_details": len(detail_ids),
        "stock_created": created,
        "stock_updated": updated,
        "catalog_matched": catalog_matched,
        "skipped": skipped,
        "skipped_reasons": skipped_reasons,
        "detail_ids": detail_ids,
        "stock_ids": stock_ids,
    }
