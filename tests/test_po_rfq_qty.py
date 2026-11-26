"""Tests for the independent RFQ-stage quantity on purchase order lines."""

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

from pydantic import TypeAdapter
from sqlalchemy import Integer

from models import PurchaseOrderLine, PurchaseOrderLineHistory
from routers.purchase_order import WhatsAppPoLineUpdateBody
from schemas.purchase_order import (
    PurchaseOrderLineCreate,
    PurchaseOrderLineResponse,
    PurchaseOrderLineUpdate,
)
from schemas.purchase_order_history import (
    PurchaseOrderLineHistoryBase,
    PurchaseOrderLineHistoryUpdate,
)
from services.po_history_snapshot import _line_to_history_dict
from services.po_line_qty import apply_line_qty_update, apply_qty_defaults_on_create

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _response_payload(**overrides):
    """Minimum required fields for validating a PO line response."""
    payload = {
        "tenant_id": 1,
        "company_id": 2,
        "document_type": "PO",
        "po_number": "PO-1",
        "version_no": 1,
        "line_no": 1,
        "line_id": 10,
        "po_id": 20,
        "created_at": _TIMESTAMP,
        "updated_at": _TIMESTAMP,
    }
    payload.update(overrides)
    return payload


def test_rfq_qty_create_dto_round_trip():
    dto = PurchaseOrderLineCreate(line_no=1, rfq_qty=7)
    dumped = dto.model_dump()
    assert dumped["rfq_qty"] == 7

    # po_id is filled from the header at persistence time, not by the create DTO.
    response = PurchaseOrderLineResponse.model_validate(
        _response_payload(**{key: value for key, value in dumped.items() if key != "po_id"})
    )
    assert response.rfq_qty == 7
    assert isinstance(response.rfq_qty, int)


def test_rfq_qty_update_dto_round_trip():
    dumped = PurchaseOrderLineUpdate(rfq_qty=0).model_dump(exclude_unset=True)
    assert dumped == {"rfq_qty": 0}
    assert isinstance(dumped["rfq_qty"], int)


def test_rfq_qty_read_and_list_response_include_field():
    one = PurchaseOrderLineResponse.model_validate(_response_payload(rfq_qty=4))
    assert "rfq_qty" in one.model_dump()
    assert one.model_dump()["rfq_qty"] == 4

    many = TypeAdapter(List[PurchaseOrderLineResponse]).validate_python(
        [
            _response_payload(rfq_qty=4),
            _response_payload(line_no=2, rfq_qty=None),
        ]
    )
    assert [line.rfq_qty for line in many] == [4, None]
    assert all("rfq_qty" in line.model_dump() for line in many)


def test_nullable_rfq_qty_is_accepted():
    assert PurchaseOrderLineCreate(line_no=1, rfq_qty=None).rfq_qty is None
    assert PurchaseOrderLineUpdate(rfq_qty=None).model_dump(exclude_unset=True) == {
        "rfq_qty": None
    }
    assert (
        PurchaseOrderLineResponse.model_validate(_response_payload(rfq_qty=None)).rfq_qty
        is None
    )


def test_model_column_is_integer_nullable_no_default():
    for model in (PurchaseOrderLine, PurchaseOrderLineHistory):
        column = model.__table__.c.rfq_qty
        assert isinstance(column.type, Integer)
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None


def test_whatsapp_line_update_body_accepts_rfq_qty():
    body = WhatsAppPoLineUpdateBody(rfq_qty=3)
    assert body.rfq_qty == 3
    dumped = body.model_dump(exclude_unset=True)
    assert dumped["rfq_qty"] == 3
    assert isinstance(dumped["rfq_qty"], int)


def test_history_snapshot_includes_rfq_qty():
    line = MagicMock()
    line.rfq_qty = 5
    snapshot = _line_to_history_dict(line, 2)
    assert snapshot["rfq_qty"] == 5

    history_payload = {
        "tenant_id": 1,
        "company_id": 2,
        "document_type": "PO",
        "po_number": "PO-1",
        "version_no": 1,
        "line_no": 1,
        "po_id": 20,
    }
    assert PurchaseOrderLineHistoryBase(**history_payload, rfq_qty=5).rfq_qty == 5
    assert PurchaseOrderLineHistoryUpdate(rfq_qty=0).model_dump(exclude_unset=True) == {
        "rfq_qty": 0
    }


def test_qty_defaults_do_not_touch_rfq_qty():
    data = {"suggested_qty": 10, "rfq_qty": 7}
    result = apply_qty_defaults_on_create(data)
    assert result["rfq_qty"] == 7
    assert isinstance(result["rfq_qty"], int)
    assert result["suggested_qty"] == Decimal("10")

    line = SimpleNamespace(
        rfq_qty=7,
        suggested_qty=None,
        buyer_qty=None,
        supplier_qty=None,
        qty_ordered=None,
    )
    remaining = apply_line_qty_update(
        line, {"buyer_qty": 10, "rfq_qty": 0}, current_editor="BUYER"
    )
    assert remaining["rfq_qty"] == 0
    assert line.rfq_qty == 7
    assert line.qty_ordered == Decimal("10")
