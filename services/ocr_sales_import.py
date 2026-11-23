"""
Parse sales documents (OCR/CSV) and insert into sales_transactions.

Expected line fields: Date, Product_ID, Product_Name, Category, Qty_Sold.
"""

from __future__ import annotations

import csv
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Product, SalesTransaction
from services.sales_foundation_catalog import build_sales_match_candidates
from services.sales_product_matching_engine import (
    MATCH_COLOR_GREEN,
    MATCH_COLOR_RED,
    MATCH_COLOR_YELLOW,
    SalesProductMatchingEngine,
)
from services.product_matching_common import extract_line_product_code
from services.pdf_inspection import (
    MIN_DOCUMENT_TEXT_CHARS,
    extract_pdf_with_targeted_ocr,
    extract_selectable_text,
    inspect_pdf,
)

from services.sales_upload_perf import SalesUploadPerf, perf_stage

logger = logging.getLogger(__name__)

_DATE_KEYS = {
    "date",
    "transaction_date",
    "transactiondate",
    "sale_date",
    "sales_date",
    "txn_date",
}
_PRODUCT_ID_KEYS = {
    "product_id",
    "productid",
    "product id",
    "item_id",
    "itemid",
}
_PRODUCT_NAME_KEYS = {
    "product_name",
    "productname",
    "product name",
    "item_name",
    "itemname",
    "item name",
    "description",
}
_CATEGORY_KEYS = {"category", "product_category", "product category"}
_QTY_KEYS = {
    "qty_sold",
    "qtysold",
    "qty sold",
    "quantity_sold",
    "quantity sold",
    "qty",
    "quantity",
}


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
    for key, value in row.items():
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


def _parse_date(val: Any) -> Optional[date]:
    if val is None or val == "":
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if not isinstance(val, str):
        return None

    s = val.strip()
    if not s:
        return None

    # ISO first (YYYY-MM-DD…)
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass

    for fmt in (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%m/%d/%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%Y/%m/%d",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _normalize_line(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a raw OCR/CSV row to sales_transactions fields. Returns None if incomplete."""
    if not isinstance(row, dict):
        return None

    # Prefer explicit snake_case keys from Gemini, then header aliases.
    transaction_date = _parse_date(
        row.get("transaction_date")
        if row.get("transaction_date") is not None
        else _pick(row, _DATE_KEYS)
    )
    product_id = _parse_int(
        row.get("product_id")
        if row.get("product_id") is not None
        else _pick(row, _PRODUCT_ID_KEYS)
    )
    product_name = _clip(
        _clean_str(
            row.get("product_name")
            if row.get("product_name") is not None
            else _pick(row, _PRODUCT_NAME_KEYS)
        ),
        255,
    )
    category = _clip(
        _clean_str(
            row.get("category")
            if row.get("category") is not None
            else _pick(row, _CATEGORY_KEYS)
        ),
        100,
    )
    qty_sold = _parse_decimal(
        row.get("qty_sold")
        if row.get("qty_sold") is not None
        else _pick(row, _QTY_KEYS)
    )

    if transaction_date is None or not product_name or qty_sold is None:
        return None

    product_code = _clip(extract_line_product_code(row), 50)

    result = {
        "transaction_date": transaction_date,
        "product_id": product_id,
        "product_name": product_name,
        "category": category,
        "qty_sold": qty_sold,
    }
    if product_code:
        result["product_code"] = product_code
    return result


def extract_sales_lines_from_payload(ocr_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    sales = ocr_payload.get("sales") if isinstance(ocr_payload, dict) else None
    raw_lines: List[Any] = []
    if isinstance(sales, dict):
        raw_lines = sales.get("lines") or []
    elif isinstance(ocr_payload.get("sales_lines"), list):
        raw_lines = ocr_payload.get("sales_lines") or []

    lines: List[Dict[str, Any]] = []
    if not isinstance(raw_lines, list):
        return lines
    for row in raw_lines:
        normalized = _normalize_line(row) if isinstance(row, dict) else None
        if normalized:
            lines.append(normalized)
    return lines


def csv_has_product_id_column(path: str) -> bool:
    """True when the CSV header row includes a product/item id column."""
    file_path = Path(path)
    if not file_path.exists():
        return False
    with open(file_path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(fh, dialect=dialect)
        try:
            headers = next(reader)
        except StopIteration:
            return False
    for header in headers:
        norm = _normalize_header(header)
        spaced = norm.replace("_", " ")
        if (
            norm in _PRODUCT_ID_KEYS
            or spaced in _PRODUCT_ID_KEYS
            or str(header or "").strip().lower() in _PRODUCT_ID_KEYS
        ):
            return True
    return False


def source_has_product_id_column(saved_files: List[str]) -> bool:
    """True when any uploaded CSV declares a Product_ID / Item_ID column."""
    for path in saved_files:
        if is_csv_path(path) and csv_has_product_id_column(path):
            return True
    return False


def parse_sales_csv(path: str) -> List[Dict[str, Any]]:
    """Parse a CSV sales file into normalized sales line dicts."""
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
            # Skip blank rows
            if not any(_clean_str(v) for v in row.values()):
                continue
            normalized = _normalize_line(row)
            if normalized:
                lines.append(normalized)
    return lines


def _ensure_products_for_sales_lines(
    db: Session,
    lines: List[Dict[str, Any]],
    *,
    created_by: Optional[int],
) -> int:
    """Create products rows for IDs in sales lines that are not yet in the catalog."""
    sample_by_id: Dict[int, Dict[str, Any]] = {}
    for line in lines:
        pid = line.get("product_id")
        if pid is None:
            continue
        pid = int(pid)
        if pid not in sample_by_id:
            sample_by_id[pid] = line

    if not sample_by_id:
        return 0

    existing_ids = {
        row[0]
        for row in db.query(Product.product_id)
        .filter(Product.product_id.in_(sample_by_id.keys()))
        .all()
    }

    created = 0
    missing_mappings = []
    for pid, line in sample_by_id.items():
        if pid in existing_ids:
            continue
        missing_mappings.append(
            {
                "product_id": pid,
                "name": _clip(line["product_name"], 100) or f"Product {pid}",
                "category": _clip(line.get("category"), 50),
                "created_by": created_by,
                "is_active": True,
            }
        )

    if missing_mappings:
        db.bulk_insert_mappings(Product, missing_mappings)
        db.flush()
        created = len(missing_mappings)
    return created


def _resolve_product_ids_by_name(
    db: Session,
    lines: List[Dict[str, Any]],
    *,
    created_by: Optional[int],
) -> int:
    """
    Fill missing product_id using product name.

    Creates catalog rows for unknown names so sales lines can be inserted.
    """
    sample_by_name: Dict[str, Dict[str, Any]] = {}
    for line in lines:
        if line.get("product_id") is not None:
            continue
        name = _clean_str(line.get("product_name"))
        if not name:
            continue
        sample_by_name.setdefault(name, line)

    if not sample_by_name:
        return 0

    names = list(sample_by_name.keys())
    rows = (
        db.query(Product.product_id, Product.name)
        .filter(Product.name.in_(names))
        .all()
    )
    name_to_id = {name: int(pid) for pid, name in rows}

    created = 0
    next_id = db.query(func.max(Product.product_id)).scalar() or 0
    for name, sample_line in sample_by_name.items():
        pid = name_to_id.get(name)
        if pid is None:
            next_id += 1
            db.add(
                Product(
                    product_id=next_id,
                    name=_clip(name, 100),
                    category=_clip(sample_line.get("category"), 50),
                    created_by=created_by,
                    is_active=True,
                )
            )
            pid = next_id
            created += 1
        name_to_id[name] = pid

    if created:
        db.flush()

    for line in lines:
        if line.get("product_id") is not None:
            continue
        name = _clean_str(line.get("product_name"))
        if not name:
            continue
        pid = name_to_id.get(name)
        if pid is not None:
            line["product_id"] = pid

    return created


def _line_match_payload(
    idx: int,
    line: Dict[str, Any],
    *,
    match_color: str,
    line_status: str,
    match_type: str,
    product_id: Optional[int] = None,
    inserted: bool = False,
    match_review: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "lineIndex": idx,
        "transaction_date": line["transaction_date"].isoformat()
        if hasattr(line.get("transaction_date"), "isoformat")
        else line.get("transaction_date"),
        "product_name": line.get("product_name"),
        "category": line.get("category"),
        "qty_sold": str(line.get("qty_sold")),
        "matchColor": match_color,
        "matchStatus": line_status,
        "matchType": match_type,
        "inserted": inserted,
    }
    if product_id is not None:
        payload["productId"] = int(product_id)
    if match_review:
        payload["matchReview"] = match_review
    return payload


def insert_sales_transaction_lines(
    db: Session,
    lines: List[Dict[str, Any]],
    *,
    tenant_id: int = 1,
    company_id: int = 3,
    created_by: Optional[int] = None,
    created_ip: Optional[str] = None,
    use_product_matching: bool = False,
    perf: Optional["SalesUploadPerf"] = None,
) -> Dict[str, Any]:
    """
    Insert normalized sales lines into sales_transactions.

    When ``use_product_matching`` is True (upload has no Product_ID column),
    lines are matched against existing product / stock catalog entries:
      - green  → single match, inserted immediately
      - yellow → multiple matches, deferred for user selection
      - red    → no match, skipped (does not create product master rows)

    Otherwise missing product_id values are resolved by name (legacy path).
    """
    if not lines:
        return {
            "inserted": 0,
            "skipped": 0,
            "skipped_reasons": [],
            "sales_transaction_ids": [],
            "products_created": 0,
            "affected_product_ids": [],
            "use_product_matching": use_product_matching,
            "line_matches": [],
            "pending_match_reviews": [],
            "match_summary": {"green": 0, "yellow": 0, "red": 0},
        }

    products_created = 0
    match_engine: Optional[SalesProductMatchingEngine] = None

    if use_product_matching:
        with perf_stage(perf, "sales_insert:build_match_pool"):
            candidates, foundation_by_code, foundation_by_name = build_sales_match_candidates(db)
            match_engine = SalesProductMatchingEngine(
                candidates=candidates,
                foundation_by_code=foundation_by_code,
                foundation_by_name=foundation_by_name,
            )
    else:
        with perf_stage(perf, "sales_insert:resolve_product_ids"):
            _resolve_product_ids_by_name(db, lines, created_by=created_by)
        with perf_stage(perf, "sales_insert:ensure_products"):
            products_created = _ensure_products_for_sales_lines(
                db, lines, created_by=created_by
            )

    with perf_stage(perf, "sales_insert:lookup_products"):
        product_ids = {
            int(line["product_id"])
            for line in lines
            if line.get("product_id") is not None
        }
        existing_ids = {
            row[0]
            for row in db.query(Product.product_id)
            .filter(Product.product_id.in_(product_ids))
            .all()
        } if product_ids else set()

    pending_rows: List[SalesTransaction] = []
    skipped = 0
    skipped_reasons: List[Dict[str, Any]] = []
    line_matches: List[Dict[str, Any]] = []
    pending_match_reviews: List[Dict[str, Any]] = []
    match_summary = {"green": 0, "yellow": 0, "red": 0}
    affected_product_ids: List[int] = []
    created_ip_val = _clip(created_ip, 45) if created_ip else None

    with perf_stage(perf, "sales_insert:build_rows"):
        for idx, line in enumerate(lines, start=1):
            product_id = line.get("product_id")

            if use_product_matching and product_id is None and match_engine is not None:
                match_result = match_engine.run_for_line(
                    db=db,
                    product_name=line.get("product_name") or "",
                    category=line.get("category"),
                    product_code=line.get("product_code"),
                )
                line_payload = _line_match_payload(
                    idx,
                    line,
                    match_color=match_result.match_color,
                    line_status=match_result.line_status,
                    match_type=match_result.match_type,
                    match_review=match_result.match_review,
                )
                line_matches.append(line_payload)
                match_summary[match_result.match_color] = (
                    match_summary.get(match_result.match_color, 0) + 1
                )

                if match_result.match_color == MATCH_COLOR_GREEN and match_result.product:
                    product_id = int(match_result.product.product_id)
                    line["product_id"] = product_id
                    existing_ids.add(product_id)
                    line_payload["productId"] = product_id
                    line_payload["inserted"] = True
                elif match_result.match_color == MATCH_COLOR_YELLOW:
                    pending_match_reviews.append(line_payload)
                    skipped += 1
                    skipped_reasons.append(
                        {
                            "row": idx,
                            "product_name": line.get("product_name"),
                            "reason": "Multiple product matches — select one to confirm",
                            "matchColor": MATCH_COLOR_YELLOW,
                        }
                    )
                    continue
                else:
                    skipped += 1
                    skipped_reasons.append(
                        {
                            "row": idx,
                            "product_name": line.get("product_name"),
                            "product_code": line.get("product_code"),
                            "reason": (
                                "No matching product in catalog or stock — "
                                "upload rate contract first to create product master"
                            ),
                            "matchColor": MATCH_COLOR_RED,
                        }
                    )
                    continue

            if product_id is None:
                skipped += 1
                skipped_reasons.append(
                    {
                        "row": idx,
                        "product_name": line.get("product_name"),
                        "reason": "Could not resolve product_id for product name",
                        "matchColor": MATCH_COLOR_RED,
                    }
                )
                if use_product_matching:
                    payload = _line_match_payload(
                        idx,
                        line,
                        match_color=MATCH_COLOR_RED,
                        line_status="NO_MATCH",
                        match_type="none",
                    )
                    line_matches.append(payload)
                    match_summary[MATCH_COLOR_RED] = match_summary.get(MATCH_COLOR_RED, 0) + 1
                continue

            product_id = int(product_id)
            if product_id not in existing_ids:
                skipped += 1
                skipped_reasons.append(
                    {
                        "row": idx,
                        "product_id": product_id,
                        "reason": f"Product with id {product_id} not found",
                        "matchColor": MATCH_COLOR_RED,
                    }
                )
                continue

            pending_rows.append(
                SalesTransaction(
                    transaction_date=line["transaction_date"],
                    product_id=product_id,
                    product_name=line["product_name"],
                    category=line.get("category"),
                    qty_sold=line["qty_sold"],
                    tenant_id=tenant_id,
                    company_id=company_id,
                    created_by=created_by,
                    created_ip=created_ip_val,
                )
            )
            affected_product_ids.append(product_id)

            if use_product_matching and not any(
                lm.get("lineIndex") == idx for lm in line_matches
            ):
                line_matches.append(
                    _line_match_payload(
                        idx,
                        line,
                        match_color=MATCH_COLOR_GREEN,
                        line_status="CONFIRMED",
                        match_type="product_id",
                        product_id=product_id,
                        inserted=True,
                    )
                )
                match_summary[MATCH_COLOR_GREEN] = match_summary.get(MATCH_COLOR_GREEN, 0) + 1

    inserted_ids: List[int] = []
    if pending_rows:
        with perf_stage(perf, "sales_insert:db_flush"):
            db.add_all(pending_rows)
            db.flush()
        inserted_ids = [int(row.sales_transaction_id) for row in pending_rows]

    return {
        "inserted": len(inserted_ids),
        "skipped": skipped,
        "skipped_reasons": skipped_reasons,
        "sales_transaction_ids": inserted_ids,
        "products_created": products_created,
        "affected_product_ids": list(dict.fromkeys(affected_product_ids)),
        "use_product_matching": use_product_matching,
        "line_matches": line_matches,
        "pending_match_reviews": pending_match_reviews,
        "match_summary": match_summary,
    }


def insert_ocr_sales_transactions(
    db: Session,
    ocr_payload: Dict[str, Any],
    *,
    tenant_id: int = 1,
    company_id: int = 3,
    created_by: Optional[int] = None,
    created_ip: Optional[str] = None,
) -> Dict[str, Any]:
    lines = extract_sales_lines_from_payload(ocr_payload)
    return insert_sales_transaction_lines(
        db,
        lines,
        tenant_id=tenant_id,
        company_id=company_id,
        created_by=created_by,
        created_ip=created_ip,
    )


def is_csv_path(path: str) -> bool:
    return Path(path).suffix.lower() == ".csv"


def is_pdf_path(path: str) -> bool:
    return Path(path).suffix.lower() == ".pdf"


def is_image_path(path: str) -> bool:
    return Path(path).suffix.lower() in {".png", ".jpg", ".jpeg"}


_PRODUCT_CODE_TOKEN_RE = r"[A-Za-z0-9][A-Za-z0-9_-]*"

# Stacked PDF layout with Item ID: Date / Item ID / Product Name / Category / Qty Sold.
_STACKED_SALES_ROW_RE = re.compile(
    r"(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s*\n"
    rf"\s*(?P<pid>{_PRODUCT_CODE_TOKEN_RE})\s*\n"
    r"\s*(?P<name>[^\n]+?)\s*\n"
    r"\s*(?P<cat>[^\n]+?)\s*\n"
    r"\s*(?P<qty>\d+(?:\.\d+)?)",
    re.MULTILINE,
)

# Stacked PDF with ERP/vendor code + UOM: Date / Product Code / Name / Category / UOM / Qty.
_STACKED_SALES_6FIELD_RE = re.compile(
    r"(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s*\n"
    rf"\s*(?P<pcode>{_PRODUCT_CODE_TOKEN_RE})\s*\n"
    r"\s*(?P<name>[^\n]+?)\s*\n"
    r"\s*(?P<cat>[^\n]+?)\s*\n"
    r"\s*(?P<uom>[^\n]+?)\s*\n"
    r"\s*(?P<qty>\d+(?:\.\d+)?)",
    re.MULTILINE,
)

# Stacked PDF layout without Item ID: Date / Product Name / Category / Qty Sold.
_STACKED_SALES_4FIELD_RE = re.compile(
    r"(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s*\n"
    r"\s*(?P<name>[^\n]+?)\s*\n"
    r"\s*(?P<cat>[^\n]+?)\s*\n"
    r"\s*(?P<qty>\d+(?:\.\d+)?)",
    re.MULTILINE,
)

# Inline row: date, id, name, category, qty on one line (CSV/TSV or space-separated).
_INLINE_SALES_ROW_RE = re.compile(
    r"^(?P<date>\d{1,2}[-/][A-Za-z]{3}[-/]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"
    r"[\s,;\t|]+(?P<pid>\d+)"
    r"[\s,;\t|]+(?P<name>.+?)"
    r"[\s,;\t|]+(?P<cat>[^,;\t|]+?)"
    r"[\s,;\t|]+(?P<qty>\d+(?:\.\d+)?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _row_from_parts(
    date_val: str,
    pid_val: str,
    name_val: str,
    cat_val: str,
    qty_val: str,
) -> Optional[Dict[str, Any]]:
    row: Dict[str, Any] = {
        "Date": date_val,
        "Product Name": name_val,
        "Category": cat_val,
        "Qty Sold": qty_val,
    }
    if pid_val.isdigit():
        row["Item ID"] = pid_val
    else:
        row["Product Code"] = pid_val
    return _normalize_line(row)


def parse_sales_plaintext(text: str) -> List[Dict[str, Any]]:
    """
    Parse sales register text without LLM calls.
    Handles stacked PDF text and inline CSV/TSV rows.
    """
    if not text or not text.strip():
        return []

    lines: List[Dict[str, Any]] = []

    # CSV/TSV with header row
    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    if re.search(r"[,;\t|]", first_line):
        try:
            import io

            sample = text[:4096]
            dialect = csv.excel
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                pass
            reader = csv.DictReader(io.StringIO(text), dialect=dialect)
            for row in reader:
                if not isinstance(row, dict):
                    continue
                if not any(_clean_str(v) for v in row.values()):
                    continue
                normalized = _normalize_line(row)
                if normalized:
                    lines.append(normalized)
            if lines:
                return lines
        except Exception:
            pass

    for match in _STACKED_SALES_6FIELD_RE.finditer(text):
        normalized = _row_from_parts(
            match.group("date"),
            match.group("pcode"),
            match.group("name"),
            match.group("cat"),
            match.group("qty"),
        )
        if normalized:
            lines.append(normalized)
    if lines:
        return lines

    for match in _STACKED_SALES_ROW_RE.finditer(text):
        normalized = _row_from_parts(*match.groups())
        if normalized:
            lines.append(normalized)
    if lines:
        return lines

    for match in _STACKED_SALES_4FIELD_RE.finditer(text):
        normalized = _normalize_line(
            {
                "Date": match.group("date"),
                "Product Name": match.group("name"),
                "Category": match.group("cat"),
                "Qty Sold": match.group("qty"),
            }
        )
        if normalized:
            lines.append(normalized)
    if lines:
        return lines

    for match in _INLINE_SALES_ROW_RE.finditer(text):
        normalized = _row_from_parts(*match.groups())
        if normalized:
            lines.append(normalized)
    return lines


def _parse_sales_from_pdf_text(path: str, text: str) -> List[Dict[str, Any]]:
    if len(text.strip()) <= MIN_DOCUMENT_TEXT_CHARS:
        return []
    return parse_sales_plaintext(text)


def parse_sales_pdf_fast(
    path: str,
    *,
    perf: Optional["SalesUploadPerf"] = None,
    stage_prefix: str = "pdf:",
) -> List[Dict[str, Any]]:
    """
    Extract text from a PDF locally and parse sales lines (no LLM).

    1. Inspect per-page selectable text vs embedded images.
    2. Parse immediately when enough text is present.
    3. Otherwise OCR only pages that need it (native resolution, no full-doc 300 DPI).
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(path)

    file_path_str = str(file_path)
    with perf_stage(perf, f"{stage_prefix}inspect"):
        inspection = inspect_pdf(file_path_str)

    text_sources: List[str] = []
    if inspection.selectable_text.strip():
        text_sources.append(inspection.selectable_text)

    if not text_sources:
        with perf_stage(perf, f"{stage_prefix}extract_fallback"):
            fallback_text = extract_selectable_text(file_path_str)
        if fallback_text.strip():
            text_sources.append(fallback_text)

    for source_text in text_sources:
        with perf_stage(perf, f"{stage_prefix}parse_text"):
            lines = _parse_sales_from_pdf_text(file_path_str, source_text)
        if lines:
            return lines

    if not inspection.pages_needing_ocr:
        return []

    with perf_stage(perf, f"{stage_prefix}targeted_ocr"):
        ocr_text = extract_pdf_with_targeted_ocr(file_path_str)
    with perf_stage(perf, f"{stage_prefix}parse_ocr_text"):
        return _parse_sales_from_pdf_text(file_path_str, ocr_text)


def collect_lines_from_files(
    saved_files: List[str],
    *,
    perf: Optional["SalesUploadPerf"] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Parse CSV / text PDF files into sales lines locally.
    Returns (lines, paths that still need OCR — scanned PDFs and images).
    """
    lines: List[Dict[str, Any]] = []
    scan_files: List[str] = []
    for path in saved_files:
        file_name = Path(path).name
        if is_csv_path(path):
            if perf is not None:
                with perf.stage(f"collect:csv:{file_name}"):
                    parsed = parse_sales_csv(path)
            else:
                parsed = parse_sales_csv(path)
            lines.extend(parsed)
        elif is_pdf_path(path):
            fast_lines = parse_sales_pdf_fast(
                path,
                perf=perf,
                stage_prefix=f"pdf:{file_name}:",
            )
            if fast_lines:
                lines.extend(fast_lines)
            else:
                scan_files.append(path)
        elif is_image_path(path):
            scan_files.append(path)
        else:
            scan_files.append(path)
    if perf is not None and scan_files:
        print(
            f"[SALES-UPLOAD-PERF] collect: {len(scan_files)} file(s) need OCR pipeline: "
            + ", ".join(Path(p).name for p in scan_files),
            flush=True,
        )
    if perf is not None and lines:
        print(
            f"[SALES-UPLOAD-PERF] collect: parsed {len(lines)} line(s) locally from "
            f"{len(saved_files) - len(scan_files)} file(s)",
            flush=True,
        )
    return lines, scan_files
