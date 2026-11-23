"""
Insert vendor_banks from structured OCR (Gemini) payload (`banks` array).

Rows require all NOT NULL columns (bank name, account number, IFSC, branch, account type).
Skips duplicates by account number for the same vendor.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models import VendorBank


def _clean_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _clip(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[:max_len]


def _normalize_account_type(raw: Any) -> str:
    """Map OCR text to Savings | Current | OD."""
    s = (_clean_str(raw) or "Savings").lower()
    if "current" in s:
        return "Current"
    if "od" in s or "overdraft" in s:
        return "OD"
    return "Savings"


def _norm_acct_num(s: str) -> str:
    return "".join(s.split())


def insert_ocr_vendor_banks(
    db: Session,
    vendor_id: int,
    ocr_payload: Dict[str, Any],
) -> int:
    banks_in: List[Any] = ocr_payload.get("banks") or []
    if not isinstance(banks_in, list):
        return 0

    existing_nums = {
        _norm_acct_num(r[0] or "")
        for r in db.query(VendorBank.account_number)
        .filter(VendorBank.vendor_id == vendor_id)
        .all()
    }

    vendor_has_primary = (
        db.query(VendorBank.id)
        .filter(
            VendorBank.vendor_id == vendor_id,
            VendorBank.is_primary == True,  # noqa: E712
        )
        .first()
        is not None
    )

    pending: List[Dict[str, Any]] = []
    for row in banks_in:
        if not isinstance(row, dict):
            continue
        bank_name = _clean_str(row.get("bank_name"))
        account_number = _clean_str(row.get("account_number"))
        ifsc_code = _clean_str(row.get("ifsc_code"))
        branch_name = _clean_str(row.get("branch_name"))
        if branch_name is None and bank_name:
            branch_name = "Main Branch"
        if not all([bank_name, account_number, ifsc_code, branch_name]):
            continue
        acct_key = _norm_acct_num(account_number)
        if not acct_key or acct_key in existing_nums:
            continue
        pending.append(
            {
                "bank_name": bank_name,
                "account_number": account_number,
                "ifsc_code": ifsc_code,
                "branch_name": branch_name,
                "account_type": _normalize_account_type(row.get("account_type")),
            }
        )
        existing_nums.add(acct_key)

    inserted = 0
    for idx, pr in enumerate(pending):
        if vendor_has_primary:
            is_primary = False
        else:
            is_primary = idx == 0

        db_bank = VendorBank(
            vendor_id=vendor_id,
            bank_name=_clip(pr["bank_name"], 255),
            account_number=_clip(_norm_acct_num(pr["account_number"]), 50),
            ifsc_code=_clip(pr["ifsc_code"].upper(), 20),
            branch_name=_clip(pr["branch_name"], 100),
            account_type=_clip(pr["account_type"], 20),
            is_primary=is_primary,
        )
        db.add(db_bank)
        inserted += 1

    return inserted
