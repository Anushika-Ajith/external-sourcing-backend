"""Unit tests for cross-vendor internal product identity resolution."""

from __future__ import annotations

import unittest
from unittest import mock

from models import VendorItem
from services.internal_product_identity import (
    _names_refer_to_same_product,
    list_vendors_for_stock_product_codes,
    resolve_shared_internal_product_codes,
)


class TestInternalProductIdentity(unittest.TestCase):
    def test_names_refer_to_same_product_prefix(self):
        self.assertTrue(_names_refer_to_same_product("Good Day", "Good Day Biscuits"))
        self.assertTrue(_names_refer_to_same_product("Marie Gold", "Marie Gold Biscuits"))
        self.assertFalse(_names_refer_to_same_product("Good Day", "Marie Gold Biscuits"))

    @mock.patch("services.internal_product_identity.find_vendor_items_for_sales_line")
    @mock.patch("services.internal_product_identity._lookup_by_internal_codes")
    def test_resolve_reuses_single_cross_vendor_identity(
        self,
        mock_lookup_codes,
        mock_find_sales,
    ):
        mock_lookup_codes.return_value = None
        mock_find_sales.return_value = [
            VendorItem(
                id=49,
                vendor_id=47,
                erp_code="ERP00032",
                company_code="IT00032",
                product_name="Good Day Biscuits",
            )
        ]
        db = mock.Mock()
        result = resolve_shared_internal_product_codes(
            db,
            product_name="Good Day",
        )
        self.assertEqual(
            result,
            {"erp_code": "ERP00032", "company_code": "IT00032"},
        )

    @mock.patch("services.internal_product_identity.find_vendor_items_for_sales_line")
    @mock.patch("services.internal_product_identity._lookup_by_internal_codes")
    def test_resolve_ambiguous_when_multiple_internal_identities(
        self,
        mock_lookup_codes,
        mock_find_sales,
    ):
        mock_lookup_codes.return_value = None
        mock_find_sales.return_value = [
            VendorItem(
                id=49,
                vendor_id=47,
                erp_code="ERP00032",
                company_code="IT00032",
                product_name="Good Day Biscuits",
            ),
            VendorItem(
                id=65,
                vendor_id=68,
                erp_code="ERP00048",
                company_code="IT00048",
                product_name="Good Day",
            ),
        ]
        db = mock.Mock()
        result = resolve_shared_internal_product_codes(
            db,
            product_name="Good Day",
        )
        self.assertIsNone(result)

    @mock.patch(
        "services.internal_product_identity._expand_vendor_items_by_product_names"
    )
    @mock.patch(
        "services.internal_product_identity._find_vendor_items_by_exact_product_names"
    )
    @mock.patch(
        "services.internal_product_identity._find_vendor_items_by_internal_identity"
    )
    @mock.patch(
        "services.internal_product_identity._find_vendor_items_matching_code"
    )
    def test_list_vendors_expands_by_internal_identity(
        self,
        mock_match_code,
        mock_match_internal,
        mock_exact_names,
        mock_expand_names,
    ):
        seed = VendorItem(
            id=50,
            vendor_id=47,
            vendor_product_code="SUP00043",
            erp_code="ERP00033",
            company_code="IT00033",
            product_name="Marie Gold",
        )
        mock_match_code.return_value = [seed]
        mock_exact_names.return_value = []
        mock_match_internal.return_value = [
            seed,
            VendorItem(
                id=66,
                vendor_id=68,
                vendor_product_code="SUP00101",
                erp_code="ERP00033",
                company_code="IT00033",
                product_name="Marie Gold",
            ),
        ]
        mock_expand_names.return_value = []
        db = mock.Mock()
        result = list_vendors_for_stock_product_codes(db, ["SUP00043"])
        self.assertEqual(result["SUP00043"], [47, 68])
        mock_match_internal.assert_called()

    @mock.patch(
        "services.internal_product_identity._expand_vendor_items_by_product_names"
    )
    @mock.patch(
        "services.internal_product_identity._find_vendor_items_by_exact_product_names"
    )
    @mock.patch(
        "services.internal_product_identity._find_vendor_items_by_internal_identity"
    )
    @mock.patch(
        "services.internal_product_identity._find_vendor_items_by_item_ids"
    )
    @mock.patch(
        "services.internal_product_identity._find_vendor_items_matching_code"
    )
    def test_list_vendors_expands_by_stock_product_name(
        self,
        mock_match_code,
        mock_item_ids,
        mock_match_internal,
        mock_exact_names,
        mock_expand_names,
    ):
        mock_match_code.return_value = [
            VendorItem(
                id=50,
                vendor_id=47,
                vendor_product_code="SUP00043",
                product_name="Marie Gold",
            )
        ]
        mock_match_internal.return_value = []
        mock_item_ids.return_value = []
        mock_exact_names.return_value = [
            VendorItem(
                id=50,
                vendor_id=47,
                vendor_product_code="SUP00043",
                product_name="Marie Gold",
            ),
            VendorItem(
                id=66,
                vendor_id=68,
                vendor_product_code="SUP00101",
                product_name="Marie Gold",
            ),
        ]
        mock_expand_names.return_value = []
        db = mock.Mock()
        result = list_vendors_for_stock_product_codes(
            db,
            ["SUP00043"],
            code_to_product_name={"SUP00043": "Marie Gold"},
        )
        self.assertEqual(result["SUP00043"], [47, 68])
        mock_exact_names.assert_called_once()


if __name__ == "__main__":
    unittest.main()
