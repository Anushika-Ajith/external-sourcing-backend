"""Unit tests for the internal requisition client and payload mapping."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from config import settings
from services import requisition_client as rc


def _po_line(*, item_id, item_no, description, rfq_qty):
    return SimpleNamespace(
        item_id=item_id,
        item_no=item_no,
        description=description,
        rfq_qty=rfq_qty,
    )


def _response(*, status_code=200, body=None, json_exc=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = ""
    if json_exc is not None:
        resp.json.side_effect = json_exc
    else:
        resp.json.return_value = body if body is not None else {}
    return resp


def test_payload_fields_exact():
    lines = [
        {
            "itemId": 10,
            "itemNo": "A",
            "description": "Alpha",
            "departmentId": 3,
            "qtyRequested": 7,
        }
    ]

    payload = rc.build_requisition_payload(
        tenant_id=1,
        company_id=2,
        requester_id=3,
        created_by=4,
        requisition_date=date(2026, 11, 26),
        lines=lines,
    )

    assert payload == {
        "tenantId": 1,
        "companyId": 2,
        "requesterId": 3,
        "createdBy": 4,
        "requisitionDate": "2026-11-26",
        "documentType": "REQ",
        "lines": lines,
    }
    assert "departmentId" not in payload
    assert "priority" not in payload
    assert "remarks" not in payload


def test_rfq_qty_maps_to_qtyRequested():
    mapped = rc.map_po_lines_to_requisition_lines(
        [_po_line(item_id=11, item_no="X", description="Item X", rfq_qty=7)]
    )

    assert len(mapped) == 1
    assert mapped[0]["qtyRequested"] == 7
    assert isinstance(mapped[0]["qtyRequested"], int)
    assert mapped[0]["itemId"] == 11
    assert mapped[0]["itemNo"] == "X"
    assert mapped[0]["description"] == "Item X"


def test_department_id_is_three():
    lines = [
        _po_line(item_id=1, item_no="A", description="A", rfq_qty=7),
        _po_line(item_id=2, item_no="B", description="B", rfq_qty=9),
    ]

    mapped = rc.map_po_lines_to_requisition_lines(lines)
    assert len(mapped) == 2
    assert [line["departmentId"] for line in mapped] == [3, 3]

    with patch.object(rc, "DEFAULT_DEPARTMENT_ID", 99):
        remapped = rc.map_po_lines_to_requisition_lines(lines)
    assert [line["departmentId"] for line in remapped] == [99, 99]


def test_only_positive_rfq_qty_lines_included():
    lines = [
        _po_line(item_id=1, item_no="A", description="A", rfq_qty=7),
        _po_line(item_id=2, item_no="B", description="B", rfq_qty=0),
        _po_line(item_id=3, item_no="C", description="C", rfq_qty=None),
    ]

    mapped = rc.map_po_lines_to_requisition_lines(lines)

    assert [line["itemId"] for line in mapped] == [1]
    assert mapped[0]["qtyRequested"] == 7


def test_url_uses_internal_sourcing_api_url():
    payload = {"documentType": "REQ", "lines": []}
    with (
        patch.object(settings, "internal_sourcing_api_url", "http://sourcing:8001/"),
        patch.object(settings, "internal_api_url", "http://foundation:3003"),
        patch.object(rc.requests, "post", return_value=_response(body={"ok": True})) as post,
    ):
        rc.create_requisition(payload=payload, bearer_token="Bearer jwt")

    url = post.call_args.args[0]
    assert url == "http://sourcing:8001/requisition-header/create-full"
    assert "foundation:3003" not in url
    assert post.call_args.kwargs["json"] is payload
    assert post.call_args.kwargs["timeout"] == 30


def test_authorization_is_normalized_to_bearer():
    for raw_token in ("jwt-token", "Bearer jwt-token", " bearer jwt-token "):
        with (
            patch.object(rc.requests, "post", return_value=_response(body={})) as post,
        ):
            rc.create_requisition(payload={}, bearer_token=raw_token)
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer jwt-token"


def test_http_error_raises_requisition_client_error():
    with patch.object(rc.requests, "post", side_effect=requests.RequestException("down")):
        with pytest.raises(rc.RequisitionClientError):
            rc.create_requisition(payload={}, bearer_token="jwt")

    with patch.object(
        rc.requests, "post", return_value=_response(status_code=503, body={"message": "down"})
    ):
        with pytest.raises(rc.RequisitionClientError):
            rc.create_requisition(payload={}, bearer_token="jwt")


def test_invalid_json_raises_requisition_client_error():
    for exc in (ValueError("malformed json"), TypeError("not json serializable")):
        with patch.object(rc.requests, "post", return_value=_response(json_exc=exc)):
            with pytest.raises(rc.RequisitionClientError):
                rc.create_requisition(payload={}, bearer_token="jwt")

    with patch.object(rc.requests, "post", return_value=_response(body=["not", "a", "dict"])):
        with pytest.raises(rc.RequisitionClientError):
            rc.create_requisition(payload={}, bearer_token="jwt")


def test_requisition_response_passed_through():
    body = {"requisitionId": 55, "requisitionNo": "REQ-1"}
    with patch.object(rc.requests, "post", return_value=_response(body=body)):
        result = rc.create_requisition(payload={"documentType": "REQ"}, bearer_token="jwt")

    assert result == body
