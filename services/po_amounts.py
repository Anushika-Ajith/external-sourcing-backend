"""Compute purchase-order line and header monetary totals.

Line total = (qty_ordered × unit_price) + tax − discount.
Header:
  po_total_amount  = Σ (qty_ordered × unit_price)   # agreement / subtotal
  tax_total_amount = Σ tax_amount
  discount_amount  = Σ line discount_amount
  net_total_amount = po_total_amount + tax_total_amount − discount_amount
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Sequence, Union

from models import PurchaseOrderHeader, PurchaseOrderLine

NumberLike = Union[Decimal, float, int, str, None]
TWOPLACES = Decimal("0.01")
ZERO = Decimal("0")


def _to_decimal(value: NumberLike) -> Decimal:
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO


def _money(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _line_unit_price(line: PurchaseOrderLine) -> Decimal:
    """First positive rate among unit_price, contract rate, and PO/OCR rate."""
    for candidate in (
        line.unit_price,
        line.rate_contract_item_rate,
        line.po_item_rate,
    ):
        val = _to_decimal(candidate)
        if val > ZERO:
            return val
    return ZERO


def compute_line_amounts(line: PurchaseOrderLine) -> Decimal:
    """
    Fill tax/discount when only percent is set, then set line.total_amount.

    Returns the taxable amount (qty × unit_price) for header rollup.
    """
    qty = _to_decimal(line.qty_ordered)
    price = _line_unit_price(line)
    taxable = qty * price

    if line.tax_amount is None and line.tax_percent is not None:
        line.tax_amount = _money(taxable * _to_decimal(line.tax_percent) / Decimal("100"))

    if line.discount_amount is None and line.discount_percent is not None:
        line.discount_amount = _money(
            taxable * _to_decimal(line.discount_percent) / Decimal("100")
        )

    tax = _to_decimal(line.tax_amount)
    discount = _to_decimal(line.discount_amount)
    line.total_amount = _money(taxable + tax - discount)
    return taxable


def recalculate_po_totals(
    header: PurchaseOrderHeader,
    lines: Optional[Sequence[PurchaseOrderLine]] = None,
) -> None:
    """
    Recompute each line.total_amount and roll up header amount fields.

    Call after lines are flushed so qty/price are persisted on the ORM objects.
    """
    if lines is None:
        lines = list(getattr(header, "po_lines", None) or [])

    po_total = ZERO
    tax_total = ZERO
    discount_total = ZERO

    for line in lines:
        taxable = compute_line_amounts(line)
        po_total += taxable
        tax_total += _to_decimal(line.tax_amount)
        discount_total += _to_decimal(line.discount_amount)

    header.po_total_amount = _money(po_total)
    header.tax_total_amount = _money(tax_total)
    header.discount_amount = _money(discount_total)
    header.net_total_amount = _money(po_total + tax_total - discount_total)


def _line_needs_rate_refresh(line: PurchaseOrderLine) -> bool:
    return (
        _line_unit_price(line) <= ZERO
        or _to_decimal(line.total_amount) <= ZERO
    )


def ensure_po_line_rates_and_totals(
    db,
    header: PurchaseOrderHeader,
    *,
    commit: bool = False,
) -> bool:
    """
    Resolve missing vendor contract rates on PO lines and recalculate totals.

    Used after PO create and as a lazy backfill when existing rows were saved
    with unit_price=0 even though vendor_rates exist.
    """
    from services.po_vendor_rate_enrich import resolve_po_line_rate_fields

    lines = (
        db.query(PurchaseOrderLine)
        .filter(PurchaseOrderLine.po_id == header.po_id)
        .order_by(PurchaseOrderLine.line_no)
        .all()
    )
    if not lines:
        return False

    changed = False
    vendor_id = int(header.vendor_id)
    for line in lines:
        if not _line_needs_rate_refresh(line):
            continue
        rate_fields = resolve_po_line_rate_fields(
            db,
            vendor_id=vendor_id,
            line={
                "item_id": line.item_id,
                "item_no": line.item_no,
                "description": line.description,
            },
        )
        contract = rate_fields.get("rate_contract_item_rate")
        po_rate = rate_fields.get("po_item_rate")
        unit = rate_fields.get("unit_price")
        if contract is not None:
            line.rate_contract_item_rate = contract
            changed = True
        if po_rate is not None:
            line.po_item_rate = po_rate
            changed = True
        if unit is not None and _to_decimal(unit) > ZERO:
            line.unit_price = unit
            changed = True

    old_total = _to_decimal(header.po_total_amount)
    recalculate_po_totals(header, lines)
    if _to_decimal(header.po_total_amount) != old_total:
        changed = True

    if changed and commit:
        db.commit()
        db.refresh(header)
    return changed
