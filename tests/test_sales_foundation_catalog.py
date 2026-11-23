"""Tests for vendor_items → Foundation product sales matching."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from services.sales_foundation_catalog import (
    collect_vendor_foundation_matches,
    find_vendor_items_for_sales_line,
    index_foundation_products,
    resolve_foundation_product_for_vendor_item,
)
from services.sales_product_matching_engine import ProductCatalogCandidate


class TestSalesFoundationCatalog(unittest.TestCase):
    def test_index_foundation_products(self) -> None:
        rows = [
            {"productId": 31, "productCode": "IT00044", "name": "Brownies"},
            {"productId": 30, "productCode": "IT00043", "name": "Cookies"},
        ]
        by_code, by_name = index_foundation_products(rows)
        self.assertIn("IT00044", by_code)
        self.assertIn("brownies", by_name)

    def test_resolve_foundation_product_for_vendor_item(self) -> None:
        vendor_item = MagicMock()
        vendor_item.company_code = "IT00044"
        vendor_item.erp_code = "ERP00044"
        vendor_item.vendor_product_code = "SUP00082"
        vendor_item.product_name = "Brownies"
        vendor_item.item_name = "Brownies"
        vendor_item.description = None
        vendor_item.product_fingerprint = {}

        foundation_by_code, foundation_by_name = index_foundation_products(
            [{"productId": 31, "productCode": "IT00044", "name": "Brownies"}]
        )
        row = resolve_foundation_product_for_vendor_item(
            vendor_item,
            foundation_by_code=foundation_by_code,
            foundation_by_name=foundation_by_name,
        )
        self.assertEqual(row["productId"], 31)

    def test_find_vendor_items_by_erp_code(self) -> None:
        db = MagicMock()
        item = MagicMock()
        item.id = 61
        item.erp_code = "ERP00044"
        item.product_name = "Brownies"
        item.item_name = "Brownies"
        item.description = None
        item.product_fingerprint = {}
        item.status = "CONFIRMED"
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [item]

        hits = find_vendor_items_for_sales_line(
            db,
            product_code="ERP00044",
            product_name="Brownys",
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].id, 61)

    def test_collect_vendor_foundation_matches_single(self) -> None:
        db = MagicMock()
        vendor_item = MagicMock()
        vendor_item.id = 60
        vendor_item.company_code = "IT00043"
        vendor_item.erp_code = "ERP00043"
        vendor_item.vendor_product_code = "SUP00091"
        vendor_item.product_name = "Cookies"
        vendor_item.item_name = "Cookies"
        vendor_item.description = None
        vendor_item.product_fingerprint = {}
        vendor_item.status = "CONFIRMED"

        product = MagicMock()
        product.product_id = 30
        product.name = "Cookies"
        product.category = "FMCG"

        foundation_rows = [{"productId": 30, "productCode": "IT00043", "name": "Cookies"}]
        by_code, by_name = index_foundation_products(foundation_rows)
        pool = [
            ProductCatalogCandidate(
                product_id=30,
                name="Cookies",
                category="FMCG",
                aliases=["Cookies"],
                product=product,
            )
        ]

        with patch(
            "services.sales_foundation_catalog.find_vendor_items_for_sales_line",
            return_value=[vendor_item],
        ), patch(
            "services.sales_foundation_catalog.ensure_local_product_for_sales",
            return_value=product,
        ):
            hits = collect_vendor_foundation_matches(
                db,
                product_code="ERP00043",
                product_name="Cookies",
                pool=pool,
                foundation_by_code=by_code,
                foundation_by_name=by_name,
            )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].product_id, 30)


if __name__ == "__main__":
    unittest.main()
