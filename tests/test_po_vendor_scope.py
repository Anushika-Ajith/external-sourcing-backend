"""Tests for vendor email → vendor_id resolution used after login."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from services.po_vendor_scope import (
    find_vendor_by_email,
    resolve_actor_vendor_id,
    resolve_vendor_for_user,
)


def test_resolve_actor_vendor_id_prefers_user_vendor_id():
    user = SimpleNamespace(vendor_id=42, email="a@b.com")
    db = MagicMock()
    assert resolve_actor_vendor_id(db, user) == 42
    db.query.assert_not_called()


def test_find_vendor_by_email_normalizes_case(monkeypatch):
    vendor = SimpleNamespace(vendor_id=7, email="Vendor@Example.COM")
    query = MagicMock()
    query.filter.return_value.order_by.return_value.first.return_value = vendor
    db = MagicMock()
    db.query.return_value = query

    found = find_vendor_by_email(db, "  vendor@example.com ")
    assert found is vendor
    db.query.assert_called_once()


def test_resolve_vendor_for_user_links_missing_vendor_id():
    vendor = SimpleNamespace(vendor_id=9, email="v@x.com")
    user = SimpleNamespace(user_id=1, vendor_id=None, email="v@x.com", role="vendor")

    db = MagicMock()
    # First call path: vendor_id is None → find_vendor_by_email query chain
    query = MagicMock()
    query.filter.return_value.order_by.return_value.first.return_value = vendor
    db.query.return_value = query

    result = resolve_vendor_for_user(db, user, persist_link=True)
    assert result is vendor
    assert user.vendor_id == 9
    db.commit.assert_called()
