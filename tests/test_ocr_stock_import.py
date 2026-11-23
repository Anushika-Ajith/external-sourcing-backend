"""Unit tests for stock upload product_code normalization."""

from __future__ import annotations

import unittest
from unittest import mock

from models import Vendor, VendorItem
from services.item_embedding_catalog import ItemEmbeddingMatch
from services.ocr_stock_import import (
    _apply_vendor_item_product_code,
    _resolve_line_product_identity,
    _stock_product_code_from_vendor_item,
)


class TestOcrStockImport(unittest.TestCase):
    def test_stock_product_code_from_vendor_item(self):
        item = VendorItem(vendor_product_code="SUP00044", erp_code="ERP00044")
        self.assertEqual(_stock_product_code_from_vendor_item(item), "SUP00044")

    def test_apply_vendor_item_product_code_overwrites_erp_code(self):
        vendor_item = VendorItem(
            id=61,
            vendor_id=12,
            vendor_product_code="SUP00044",
            erp_code="ERP00044",
            product_name="Brownies",
        )
        db = mock.Mock()
        db.query.return_value.filter.return_value.first.return_value = Vendor(
            vendor_id=12,
            company_name="Britannia Vendor",
        )
        resolved = {
            "product_code": "ERP00044",
            "product_name": "Brownies",
            "available_stock": 10,
        }
        out = _apply_vendor_item_product_code(resolved, vendor_item, db=db)
        self.assertEqual(out["product_code"], "SUP00044")
        self.assertEqual(out["preferred_supplier"], "Britannia Vendor")
        self.assertEqual(out["matched_vendor_item_id"], 61)

    @mock.patch("services.ocr_stock_import.evaluate_product_code_embedding_gate")
    @mock.patch("services.ocr_stock_import._find_vendor_item_by_product_code")
    @mock.patch("services.ocr_stock_import._product_code_matches_vendor_catalog")
    def test_resolve_line_uses_vendor_product_code_on_catalog_match(
        self,
        mock_catalog_match,
        mock_find_item,
        mock_embedding_gate,
    ):
        mock_catalog_match.return_value = True
        vendor_item = VendorItem(
            id=60,
            vendor_id=12,
            vendor_product_code="SUP00043",
            erp_code="ERP00043",
            product_name="Cookies",
        )
        mock_find_item.return_value = vendor_item
        mock_embedding_gate.return_value = (None, True, None, None)

        db = mock.Mock()
        db.query.return_value.filter.return_value.first.return_value = Vendor(
            vendor_id=12,
            company_name="Britannia Vendor",
        )

        line = {
            "product_code": "ERP00043",
            "product_name": "Cookies",
            "available_stock": 5,
        }
        resolved, skip = _resolve_line_product_identity(line, db=db)
        self.assertIsNone(skip)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved["product_code"], "SUP00043")
        self.assertEqual(resolved["preferred_supplier"], "Britannia Vendor")

    @mock.patch("services.ocr_stock_import._apply_vendor_item_product_code")
    @mock.patch("services.ocr_stock_import._find_vendor_item_for_embedding_match")
    @mock.patch("services.ocr_stock_import.match_stock_line_to_catalog")
    @mock.patch("services.ocr_stock_import._product_code_matches_vendor_catalog")
    def test_resolve_line_embedding_match_normalizes_to_vendor_product_code(
        self,
        mock_catalog_match,
        mock_match_catalog,
        mock_find_for_embedding,
        mock_apply_vendor_code,
    ):
        mock_catalog_match.return_value = False
        match = ItemEmbeddingMatch(
            item_code="ERP00042",
            item_name="Ice Cream",
            vendor_code="VR00012",
            item_description="",
            embedding_text="Ice Cream",
            distance=0.1,
            item_embedding_id=99,
        )
        mock_match_catalog.return_value = match
        vendor_item = VendorItem(
            id=59,
            vendor_id=12,
            vendor_product_code="SUP00042",
            erp_code="ERP00042",
        )
        mock_find_for_embedding.return_value = vendor_item
        mock_apply_vendor_code.side_effect = lambda resolved, item, db: {
            **resolved,
            "product_code": "SUP00042",
            "preferred_supplier": "Britannia Vendor",
        }

        line = {
            "product_code": "WRONG",
            "product_name": "Ice Cream",
            "available_stock": 3,
        }
        resolved, skip = _resolve_line_product_identity(line, db=mock.Mock())
        self.assertIsNone(skip)
        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(resolved["product_code"], "SUP00042")
        mock_apply_vendor_code.assert_called_once()


if __name__ == "__main__":
    unittest.main()
