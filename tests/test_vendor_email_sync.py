"""Tests for vendor email sync across vendors, users, and Foundation."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from models import User, Vendor
from services.vendor_email_sync import normalize_vendor_email, sync_vendor_email_change


def test_normalize_vendor_email():
    assert normalize_vendor_email("  User@Example.COM ") == "user@example.com"
    assert normalize_vendor_email("") is None
    assert normalize_vendor_email(None) is None


@patch("services.vendor_email_sync.update_foundation_user_email")
def test_sync_vendor_email_updates_linked_user(mock_foundation):
    db = MagicMock()
    vendor = Vendor(vendor_id=10, email="old@example.com")
    user = User(user_id=1, email="old@example.com", vendor_id=10)

    db.query.return_value.filter.return_value.first.side_effect = [
        None,  # no vendor conflict
    ]
    db.query.return_value.filter.return_value.all.side_effect = [
        [user],  # users by vendor_id
    ]
    db.query.return_value.filter.return_value.first.side_effect = [
        None,  # no vendor conflict
        None,  # no user conflict
    ]

    # Reconfigure side_effect for sequential query() calls
    vendor_conflict = MagicMock()
    vendor_conflict.first.return_value = None

    users_q = MagicMock()
    users_q.all.return_value = [user]

    user_conflict = MagicMock()
    user_conflict.first.return_value = None

    db.query.side_effect = [
        vendor_conflict,
        users_q,
        user_conflict,
    ]

    sync_vendor_email_change(
        db,
        vendor=vendor,
        old_email="old@example.com",
        new_email="new@example.com",
    )

    assert user.email == "new@example.com"
    mock_foundation.assert_called_once_with(
        old_email="old@example.com",
        new_email="new@example.com",
    )


@patch("services.vendor_email_sync.update_foundation_user_email")
def test_sync_skips_local_users_when_email_column_missing(mock_foundation):
    db = MagicMock()
    vendor = Vendor(vendor_id=3, email="old@example.com")

    vendor_conflict = MagicMock()
    vendor_conflict.first.return_value = None

    users_q = MagicMock()
    users_q.all.side_effect = Exception("column users.email does not exist")

    db.query.side_effect = [vendor_conflict, users_q]

    sync_vendor_email_change(
        db,
        vendor=vendor,
        old_email="old@example.com",
        new_email="new@example.com",
    )

    db.rollback.assert_called_once()
    mock_foundation.assert_called_once_with(
        old_email="old@example.com",
        new_email="new@example.com",
    )


@patch("services.vendor_email_sync.update_foundation_user_email")
def test_sync_vendor_email_noop_when_unchanged(mock_foundation):
    db = MagicMock()
    vendor = Vendor(vendor_id=10, email="same@example.com")

    sync_vendor_email_change(
        db,
        vendor=vendor,
        old_email="same@example.com",
        new_email="same@example.com",
    )

    mock_foundation.assert_not_called()
    db.query.assert_not_called()


@patch("services.vendor_email_sync.update_foundation_user_email")
def test_sync_vendor_email_rejects_duplicate_user(mock_foundation):
    db = MagicMock()
    vendor = Vendor(vendor_id=10, email="old@example.com")
    user = User(user_id=1, email="old@example.com", vendor_id=10)

    vendor_conflict = MagicMock()
    vendor_conflict.first.return_value = None

    users_q = MagicMock()
    users_q.all.return_value = [user]

    user_conflict = MagicMock()
    user_conflict.first.return_value = (99,)

    db.query.side_effect = [vendor_conflict, users_q, user_conflict]

    with pytest.raises(HTTPException) as exc:
        sync_vendor_email_change(
            db,
            vendor=vendor,
            old_email="old@example.com",
            new_email="taken@example.com",
        )

    assert exc.value.status_code == 400
    mock_foundation.assert_not_called()


@patch("services.vendor_email_sync.update_foundation_user_email")
def test_sync_vendor_email_skips_when_linked_user_already_has_target(mock_foundation):
    db = MagicMock()
    vendor = Vendor(vendor_id=67, email="rahultest@gmail.com")
    user_old = User(user_id=139, email="rahultest@gmail.com", vendor_id=67)
    user_target = User(user_id=138, email="rahul@gmail.com", vendor_id=67)

    vendor_conflict = MagicMock()
    vendor_conflict.first.return_value = None

    users_q = MagicMock()
    users_q.all.return_value = [user_old, user_target]

    user_conflict = MagicMock()
    user_conflict.first.return_value = None

    taken_q = MagicMock()
    taken_q.first.return_value = (138,)

    db.query.side_effect = [
        vendor_conflict,
        users_q,
        user_conflict,
        taken_q,
    ]

    sync_vendor_email_change(
        db,
        vendor=vendor,
        old_email="rahultest@gmail.com",
        new_email="rahul@gmail.com",
        current_user_id=139,
    )

    assert user_target.email == "rahul@gmail.com"
    assert user_old.email == "rahultest@gmail.com"
    mock_foundation.assert_called_once()
