"""Vendor identity fields must not be cleared or replaced with placeholder junk."""

from types import SimpleNamespace

from services.vendor_identity_guard import (
    is_placeholder_vendor_value,
    protect_vendor_update_payload,
    sanitize_identity_update,
)


def test_placeholder_detection():
    assert is_placeholder_vendor_value("Supplier details to be completed")
    assert is_placeholder_vendor_value("Buyer details to be completed")
    assert not is_placeholder_vendor_value("Green Baskets")


def test_sanitize_keeps_existing_when_incoming_blank_or_placeholder():
    assert sanitize_identity_update("Green Baskets", "") is None
    assert sanitize_identity_update("Green Baskets", None) is None
    assert (
        sanitize_identity_update(
            "Green Baskets", "Supplier details to be completed"
        )
        is None
    )


def test_sanitize_allows_real_rename_and_first_fill():
    assert (
        sanitize_identity_update("Green Baskets", "Green Baskets Pvt Ltd")
        == "Green Baskets Pvt Ltd"
    )
    assert sanitize_identity_update(None, "Green Baskets") == "Green Baskets"
    assert sanitize_identity_update("", "Supplier details to be completed") is None


def test_protect_payload_drops_bad_company_name():
    vendor = SimpleNamespace(
        company_name="Green Baskets",
        contact_person="Alice",
        gst_number="27AAAAA0000A1Z5",
        pan_number=None,
        email="a@b.com",
        mobile="7829599438",
        address_line1="Road",
        address_line2=None,
        city="Kochi",
        state="Kerala",
        postal_code="682030",
    )
    safe = protect_vendor_update_payload(
        vendor,
        {
            "company_name": "Supplier details to be completed",
            "contact_person": "",
            "vendor_type": "Product",
            "city": "Kakkanad",
        },
    )
    assert "company_name" not in safe
    assert "contact_person" not in safe
    assert safe["vendor_type"] == "Product"
    assert safe["city"] == "Kakkanad"
