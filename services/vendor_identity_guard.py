"""
Guard vendor identity fields (especially company_name) against accidental clears
or placeholder OCR / form-label values during non-edit side effects.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, MutableMapping, Optional

_PLACEHOLDER_RE = re.compile(
    r"(to be completed|details to be completed|enter (company|supplier|buyer|contact)|n/?a\b|not available|tbd\b)",
    re.IGNORECASE,
)

# Fields that must not be wiped or replaced with OCR form-label junk once set.
_PROTECTED_STRING_FIELDS = (
    "company_name",
    "contact_person",
    "gst_number",
    "pan_number",
    "email",
    "mobile",
    "address_line1",
    "address_line2",
    "city",
    "state",
    "postal_code",
)


def _norm(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def is_placeholder_vendor_value(val: Any) -> bool:
    cleaned = _norm(val)
    if cleaned is None:
        return False
    return bool(_PLACEHOLDER_RE.search(cleaned))


def sanitize_identity_update(
    existing: Any,
    incoming: Any,
    *,
    field: str = "company_name",
) -> Optional[str]:
    """
    Decide the value to persist for an identity field.

    - Never clear an existing non-empty value with blank / null.
    - Never replace an existing value with placeholder OCR / form-label text.
    - Allow intentional renames to a real non-placeholder string.
    - Allow first-time fill when existing is blank and incoming is real.

    Returns the value to write, or None when the caller should leave the
    existing DB value untouched (skip the update for this field).
    """
    _ = field
    current = _norm(existing)
    next_val = _norm(incoming)

    if next_val is None:
        # Do not wipe an established value via empty / null payload.
        return None

    if is_placeholder_vendor_value(next_val):
        return None

    return next_val


def protect_vendor_update_payload(
    vendor: Any,
    updates: Mapping[str, Any],
) -> Dict[str, Any]:
    """
    Filter a setattr payload so protected identity fields are not accidentally
    cleared or replaced with placeholder junk. Explicit real renames still pass.
    """
    out: Dict[str, Any] = dict(updates)
    for field in _PROTECTED_STRING_FIELDS:
        if field not in out:
            continue
        existing = getattr(vendor, field, None)
        sanitized = sanitize_identity_update(existing, out.get(field), field=field)
        if sanitized is None:
            # Keep DB value: drop the key so setattr is skipped.
            out.pop(field, None)
        else:
            out[field] = sanitized
    return out


def apply_protected_vendor_updates(
    vendor: Any,
    updates: MutableMapping[str, Any],
) -> None:
    """Apply ``updates`` onto ``vendor`` after identity protection."""
    safe = protect_vendor_update_payload(vendor, updates)
    for key, value in safe.items():
        if hasattr(vendor, key):
            setattr(vendor, key, value)
