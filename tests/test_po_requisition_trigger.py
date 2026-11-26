"""Tests for the requisition trigger on initial purchase order creation."""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from models import PurchaseOrderLine
from routers import purchase_order as po_router
from schemas.purchase_order import (
    PurchaseOrderCompleteCreate,
    PurchaseOrderLineNestedCreate,
    PurchaseOrderLineUpdate,
)
from services.requisition_client import RequisitionClientError

_TENANT_ID = 1
_COMPANY_ID = 2
_PO_NUMBER = "PO-REQ-1"
_VERSION_NO = 1
_PO_DATE = date(2026, 11, 26)
_VENDOR_ID = 10
_TIMESTAMP = datetime(2026, 11, 26, tzinfo=timezone.utc)


def _po_line(*, line_no, item_id, item_no, description, rfq_qty):
    """PO-line-like object as returned by the created_lines query."""
    return SimpleNamespace(
        line_no=line_no,
        item_id=item_id,
        item_no=item_no,
        description=description,
        rfq_qty=rfq_qty,
    )


def _body(*, created_by=None, lines=None):
    if lines is None:
        lines = [
            PurchaseOrderLineNestedCreate(
                line_no=1, item_id=11, item_no="A", description="Alpha", rfq_qty=7
            ),
            PurchaseOrderLineNestedCreate(
                line_no=2, item_id=12, item_no="B", description="Beta", rfq_qty=9
            ),
        ]
    return PurchaseOrderCompleteCreate(
        tenant_id=_TENANT_ID,
        company_id=_COMPANY_ID,
        document_type="PO",
        po_number=_PO_NUMBER,
        version_no=_VERSION_NO,
        po_date=_PO_DATE,
        vendor_id=_VENDOR_ID,
        created_by=created_by,
        lines=lines,
    )


def _response_line(line, line_id):
    return SimpleNamespace(
        tenant_id=_TENANT_ID,
        company_id=_COMPANY_ID,
        document_type="PO",
        po_number=_PO_NUMBER,
        version_no=_VERSION_NO,
        line_no=line.line_no,
        line_id=line_id,
        po_id=99,
        item_id=line.item_id,
        item_no=line.item_no,
        description=line.description,
        rfq_qty=line.rfq_qty,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
    )


def _response_header(lines):
    return SimpleNamespace(
        tenant_id=_TENANT_ID,
        company_id=_COMPANY_ID,
        document_type="PO",
        po_number=_PO_NUMBER,
        version_no=_VERSION_NO,
        po_date=_PO_DATE,
        vendor_id=_VENDOR_ID,
        po_id=99,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        po_lines=lines,
    )


def _make_db(created_lines):
    response_lines = [
        _response_line(line, 100 + index) for index, line in enumerate(created_lines, start=1)
    ]
    return _db(created_lines, _response_header(response_lines))


def _db(created_lines, response_header):
    """Mock Session: duplicate check, created-line query, response-header query."""
    db = MagicMock()
    header_query = MagicMock()
    header_query.filter.return_value.first.side_effect = [None, response_header]
    header_query.options.return_value.filter.return_value.first.return_value = response_header
    line_query = MagicMock()
    line_query.filter.return_value.order_by.return_value.all.return_value = created_lines
    db.query.side_effect = lambda model, *args, **kwargs: (
        line_query if model is PurchaseOrderLine else header_query
    )
    return db


@pytest.fixture
def deps():
    """Patch everything the endpoint does except the requisition trigger."""
    with (
        patch.object(po_router, "resolve_company_id", side_effect=lambda value: value),
        patch.object(po_router, "_ensure_vendor_exists"),
        patch.object(
            po_router,
            "enrich_po_lines_with_vendor_rates",
            side_effect=lambda db, *, vendor_id, lines: (lines, None),
        ),
        patch.object(po_router, "apply_qty_defaults_on_create", side_effect=lambda data: data),
        patch.object(po_router, "attach_product_fingerprint_to_line_dict"),
        patch.object(po_router, "recalculate_po_totals"),
        patch.object(po_router, "create_embeddings_for_po_lines"),
        patch.object(po_router, "generate_and_attach_po_pdf"),
    ):
        yield


def _run_complete(body, db, *, current_user_id=7, token="Bearer jwt-token"):
    request = SimpleNamespace(headers={"authorization": token})
    current_user = SimpleNamespace(user_id=current_user_id)
    return asyncio.run(
        po_router.create_purchase_order_complete(
            body=body,
            request=request,
            db=db,
            current_user=current_user,
        )
    )


def test_no_call_when_all_rfq_qty_zero_or_null(deps):
    lines = [
        _po_line(line_no=1, item_id=11, item_no="A", description="Alpha", rfq_qty=0),
        _po_line(line_no=2, item_id=12, item_no="B", description="Beta", rfq_qty=None),
    ]
    db = _make_db(lines)
    body = _body(lines=[PurchaseOrderLineNestedCreate(line_no=1), PurchaseOrderLineNestedCreate(line_no=2)])

    with (
        patch.object(po_router, "create_requisition") as create,
        patch.object(po_router, "logger", MagicMock()),
    ):
        result = _run_complete(body, db)

    create.assert_not_called()
    db.commit.assert_called_once()
    assert result.po_number == _PO_NUMBER


def test_one_call_with_all_positive_lines(deps):
    lines = [
        _po_line(line_no=1, item_id=11, item_no="A", description="Alpha", rfq_qty=7),
        _po_line(line_no=2, item_id=12, item_no="B", description="Beta", rfq_qty=9),
    ]
    db = _make_db(lines)

    with (
        patch.object(po_router, "create_requisition", return_value={"requisitionId": 55}) as create,
        patch.object(po_router, "logger", MagicMock()),
    ):
        result = _run_complete(_body(), db)

    create.assert_called_once()
    payload = create.call_args.kwargs["payload"]
    assert payload == {
        "tenantId": _TENANT_ID,
        "companyId": _COMPANY_ID,
        "requesterId": 7,
        "createdBy": 7,
        "requisitionDate": "2026-11-26",
        "documentType": "REQ",
        "lines": [
            {
                "itemId": 11,
                "itemNo": "A",
                "description": "Alpha",
                "departmentId": 3,
                "qtyRequested": 7,
            },
            {
                "itemId": 12,
                "itemNo": "B",
                "description": "Beta",
                "departmentId": 3,
                "qtyRequested": 9,
            },
        ],
    }
    assert "departmentId" not in payload
    assert result.po_number == _PO_NUMBER


def test_created_by_used_for_requester_and_created_by(deps):
    lines = [_po_line(line_no=1, item_id=11, item_no="A", description="Alpha", rfq_qty=7)]
    db = _make_db(lines)

    with (
        patch.object(po_router, "create_requisition", return_value={}) as create,
        patch.object(po_router, "logger", MagicMock()),
    ):
        _run_complete(_body(created_by=42), db, current_user_id=7)

    payload = create.call_args.kwargs["payload"]
    # A1 (documented assumption): both requesterId and createdBy use header created_by.
    assert payload["requesterId"] == 42
    assert payload["createdBy"] == 42


def test_requester_id_falls_back_to_current_user(deps):
    lines = [_po_line(line_no=1, item_id=11, item_no="A", description="Alpha", rfq_qty=7)]
    db = _make_db(lines)

    with (
        patch.object(po_router, "create_requisition", return_value={}) as create,
        patch.object(po_router, "logger", MagicMock()),
    ):
        _run_complete(_body(created_by=None), db, current_user_id=77)

    payload = create.call_args.kwargs["payload"]
    assert payload["requesterId"] == 77
    assert payload["createdBy"] == 77


@pytest.mark.parametrize(
    "error",
    [
        RequisitionClientError("Requisition API request failed: connection reset"),
        RequisitionClientError("Requisition API returned HTTP 503"),
        RequisitionClientError("Requisition API returned invalid JSON"),
    ],
    ids=["network", "http", "invalid_json"],
)
def test_po_retained_and_error_logged_when_client_raises(deps, error):
    lines = [_po_line(line_no=1, item_id=11, item_no="A", description="Alpha", rfq_qty=7)]
    db = _make_db(lines)

    with (
        patch.object(po_router, "create_requisition", side_effect=error),
        patch.object(po_router, "logger", MagicMock()) as logger,
    ):
        result = _run_complete(_body(), db)

    db.commit.assert_called_once()
    db.rollback.assert_not_called()
    logger.exception.assert_called_once()
    message = " ".join(str(arg) for arg in logger.exception.call_args.args)
    assert _PO_NUMBER in message
    for identifier in ("tenant_id", "company_id", "po_number", "version_no"):
        assert identifier in message
    assert str(_TENANT_ID) in message
    assert str(_COMPANY_ID) in message
    assert str(_VERSION_NO) in message
    assert result.po_number == _PO_NUMBER
    assert [line.line_no for line in result.lines] == [1]


def test_jwt_forwarded_to_client(deps):
    lines = [_po_line(line_no=1, item_id=11, item_no="A", description="Alpha", rfq_qty=7)]
    db = _make_db(lines)

    with (
        patch.object(po_router, "create_requisition", return_value={}) as create,
        patch.object(po_router, "logger", MagicMock()),
    ):
        _run_complete(_body(), db, token="Bearer jwt-token-abc")

    assert create.call_args.kwargs["bearer_token"] == "Bearer jwt-token-abc"


def test_no_requisition_on_line_update(deps):
    """Updating rfq_qty on an existing line must not trigger requisition creation."""
    row = _po_line(line_no=1, item_id=11, item_no="A", description="Alpha", rfq_qty=5)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    header = SimpleNamespace(po_id=99, current_editor="BUYER")

    with (
        patch.object(po_router, "_get_po_header", return_value=header),
        patch.object(po_router, "apply_line_qty_update", return_value={"rfq_qty": 7}),
        patch.object(po_router, "recalculate_po_totals"),
        patch.object(po_router, "create_requisition") as create,
    ):
        result = asyncio.run(
            po_router.update_purchase_order_line(
                tenant_id=_TENANT_ID,
                company_id=_COMPANY_ID,
                document_type="PO",
                po_number=_PO_NUMBER,
                version_no=_VERSION_NO,
                line_no=1,
                body=PurchaseOrderLineUpdate(rfq_qty=7),
                db=db,
                current_user=SimpleNamespace(user_id=7),
            )
        )

    create.assert_not_called()
    db.commit.assert_called_once()
    assert result.rfq_qty == 7  # the update is applied to the row, not sent as a requisition
