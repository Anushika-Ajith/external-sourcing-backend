"""vendor_has_login must not 500 when local users.email is missing."""

from unittest.mock import MagicMock, patch

from models import Vendor
from services.vendor_phone_lookup import vendor_has_login


def test_vendor_has_login_true_when_local_password_set():
    db = MagicMock()
    vendor = Vendor(vendor_id=10, email="coastal@gmail.com")
    user = MagicMock(hashed_password="hashed")
    db.query.return_value.filter.return_value.first.return_value = user

    assert vendor_has_login(db, vendor) is True


def test_vendor_has_login_falls_back_to_foundation_on_schema_mismatch():
    db = MagicMock()
    vendor = Vendor(vendor_id=46, email="coastal@gmail.com")
    db.query.return_value.filter.return_value.first.side_effect = Exception(
        "column users.email does not exist"
    )

    with patch(
        "services.foundation_user._lookup_foundation_user_id_by_email",
        return_value=154,
    ) as mock_lookup:
        assert vendor_has_login(db, vendor) is True
        mock_lookup.assert_called_once_with("coastal@gmail.com")
    db.rollback.assert_called()


def test_vendor_has_login_false_when_foundation_missing_after_schema_mismatch():
    db = MagicMock()
    vendor = Vendor(vendor_id=46, email="coastal@gmail.com")
    db.query.return_value.filter.return_value.first.side_effect = Exception(
        "column users.email does not exist"
    )

    with patch(
        "services.foundation_user._lookup_foundation_user_id_by_email",
        return_value=None,
    ):
        assert vendor_has_login(db, vendor) is False
