"""Helpers for purchase_order_line suggested/buyer/supplier qty fields.

qty_ordered is the effective PO quantity (what callers historically call \"po qty\").

Rules:
- On create: buyer_qty and supplier_qty default to suggested_qty; qty_ordered follows.
- Buyer edit: qty_ordered = buyer_qty; supplier_qty defaults to that buyer_qty.
- Supplier edit: supplier_qty is what the supplier edits/submits; qty_ordered follows.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, MutableMapping, Optional, Union

from models import PurchaseOrderLine

NumberLike = Union[Decimal, float, int, str, None]


def to_qty_decimal(value: NumberLike) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def apply_qty_defaults_on_create(
    data: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """
    Normalize qty fields when a PO line is created.

    suggested_qty is the base (from spike/forecast/payload). If omitted, falls back
    to qty_ordered / buyer_qty. buyer_qty and supplier_qty default to suggested_qty.
    qty_ordered defaults to buyer_qty (effective PO qty).
    """
    suggested = to_qty_decimal(data.get("suggested_qty"))
    buyer = to_qty_decimal(data.get("buyer_qty"))
    supplier = to_qty_decimal(data.get("supplier_qty"))
    ordered = to_qty_decimal(data.get("qty_ordered"))

    if suggested is None:
        if buyer is not None:
            suggested = buyer
        elif ordered is not None:
            suggested = ordered
        else:
            suggested = Decimal("0")

    if "buyer_qty" not in data or data.get("buyer_qty") is None:
        buyer = suggested
    if "supplier_qty" not in data or data.get("supplier_qty") is None:
        supplier = suggested
    if "qty_ordered" not in data or data.get("qty_ordered") is None:
        ordered = buyer if buyer is not None else suggested

    data["suggested_qty"] = suggested
    data["buyer_qty"] = buyer
    data["supplier_qty"] = supplier
    data["qty_ordered"] = ordered
    return data


def _to_rfq_qty(value: NumberLike) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def qty_fields_for_create(
    *,
    suggested_qty: NumberLike = None,
    qty_ordered: NumberLike = None,
    buyer_qty: NumberLike = None,
    supplier_qty: NumberLike = None,
    rfq_qty: NumberLike = None,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if suggested_qty is not None:
        data["suggested_qty"] = suggested_qty
    if qty_ordered is not None:
        data["qty_ordered"] = qty_ordered
    if buyer_qty is not None:
        data["buyer_qty"] = buyer_qty
    if supplier_qty is not None:
        data["supplier_qty"] = supplier_qty
    apply_qty_defaults_on_create(data)
    result: Dict[str, Any] = {
        "suggested_qty": data["suggested_qty"],
        "buyer_qty": data["buyer_qty"],
        "supplier_qty": data["supplier_qty"],
        "qty_ordered": data["qty_ordered"],
    }
    parsed_rfq = _to_rfq_qty(rfq_qty)
    if parsed_rfq is not None:
        result["rfq_qty"] = parsed_rfq
    return result


def apply_buyer_qty_edit(line: PurchaseOrderLine, new_qty: Decimal) -> None:
    """Buyer changed qty: effective PO qty and supplier default become buyer_qty."""
    line.buyer_qty = new_qty
    line.qty_ordered = new_qty
    line.supplier_qty = new_qty


def apply_supplier_qty_edit(line: PurchaseOrderLine, new_qty: Decimal) -> None:
    """Supplier edited/submitted qty: store on supplier_qty and as effective PO qty."""
    line.supplier_qty = new_qty
    line.qty_ordered = new_qty


def normalize_editor_role(current_editor: Optional[str]) -> str:
    role = (current_editor or "BUYER").strip().upper()
    if role in ("VENDOR", "SUPPLIER"):
        return "VENDOR"
    if role == "BUYER":
        return "BUYER"
    return role


def apply_line_qty_update(
    line: PurchaseOrderLine,
    update_data: Mapping[str, Any],
    *,
    current_editor: Optional[str],
) -> Dict[str, Any]:
    """
    Apply qty-related fields from an update payload.

    Returns a copy of update_data with qty keys removed once handled, so callers
    can setattr the remaining fields without double-applying.
    """
    remaining = dict(update_data)
    editor = normalize_editor_role(current_editor)

    buyer = to_qty_decimal(remaining.pop("buyer_qty", None)) if "buyer_qty" in remaining else None
    supplier = (
        to_qty_decimal(remaining.pop("supplier_qty", None))
        if "supplier_qty" in remaining
        else None
    )
    ordered = (
        to_qty_decimal(remaining.pop("qty_ordered", None))
        if "qty_ordered" in remaining
        else None
    )

    # suggested_qty is system baseline — allow explicit set but do not cascade.
    if "suggested_qty" in remaining:
        suggested = to_qty_decimal(remaining.pop("suggested_qty"))
        if suggested is not None:
            line.suggested_qty = suggested

    # rfq_qty is RFQ snapshot — allow explicit set but do not cascade.
    if "rfq_qty" in remaining:
        parsed_rfq = _to_rfq_qty(remaining.pop("rfq_qty"))
        if parsed_rfq is not None:
            line.rfq_qty = parsed_rfq

    if buyer is not None:
        apply_buyer_qty_edit(line, buyer)
    elif supplier is not None:
        apply_supplier_qty_edit(line, supplier)
    elif ordered is not None:
        if editor == "BUYER":
            apply_buyer_qty_edit(line, ordered)
        elif editor == "VENDOR":
            apply_supplier_qty_edit(line, ordered)
        else:
            line.qty_ordered = ordered

    return remaining
