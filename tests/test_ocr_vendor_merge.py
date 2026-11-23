"""OCR must not overwrite an existing supplier company name."""

from types import SimpleNamespace

from services.ocr_vendor_merge import merge_ocr_vendor_master
from services.vendor_master_merge import merge_vendor_master_from_dict


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None


class _FakeDb:
    def query(self, *args, **kwargs):
        return _FakeQuery()


def test_merge_ocr_does_not_overwrite_existing_company_name():
    vendor = SimpleNamespace(
        vendor_id=104,
        company_name="Green Baskets",
        vendor_type="Service",
        gst_number=None,
        pan_number=None,
        contact_person="Alice",
        email="ajithkk2005@gmail.com",
        mobile="7829599438",
        address_line1=None,
        address_line2=None,
        city=None,
        state=None,
        country="India",
        postal_code=None,
        is_branch=False,
        parent_vendor_id=None,
    )

    merge_ocr_vendor_master(
        _FakeDb(),
        vendor,
        {
            "vendor": {
                "company_name": "Supplier details to be completed",
                "contact_person": "Buyer details to be completed",
                "gst_number": "27AAAAA0000A1Z5",
                "address_line1": "Thalakkottu Moola Road Junction, Edachira Rd",
            }
        },
    )

    assert vendor.company_name == "Green Baskets"
    assert vendor.contact_person == "Alice"
    assert vendor.gst_number == "27AAAAA0000A1Z5"
    assert vendor.address_line1 == "Thalakkottu Moola Road Junction, Edachira Rd"


def test_merge_dict_ignores_placeholder_company_name_on_blank_vendor():
    vendor = SimpleNamespace(
        vendor_id=1,
        company_name=None,
        vendor_type=None,
        gst_number=None,
        pan_number=None,
        contact_person=None,
        email=None,
        mobile=None,
        address_line1=None,
        address_line2=None,
        city=None,
        state=None,
        country=None,
        postal_code=None,
        is_branch=False,
        parent_vendor_id=None,
    )

    changed = merge_vendor_master_from_dict(
        _FakeDb(),
        vendor,
        {
            "company_name": "Supplier details to be completed",
            "contact_person": "Real Contact",
        },
    )

    assert changed is True
    assert vendor.company_name is None
    assert vendor.contact_person == "Real Contact"
