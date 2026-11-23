"""Tests for stock pipeline qty computation."""

import unittest
from decimal import Decimal
from unittest import mock

from services.stock_pipeline_qty import enrich_stock_quantities


class TestStockPipelineQty(unittest.TestCase):
    def test_enrich_stock_quantities_adds_pipeline(self):
        result = enrich_stock_quantities(
            available_stock=10,
            reorder_level=20,
            pipeline_qty=Decimal("5"),
        )
        self.assertEqual(result["pipeline_qty"], Decimal("5.000"))
        self.assertEqual(result["stock_with_pipeline"], Decimal("15.000"))
        self.assertTrue(result["needs_reorder"])

    def test_enrich_stock_quantities_no_reorder_when_above_level(self):
        result = enrich_stock_quantities(
            available_stock=10,
            reorder_level=5,
            pipeline_qty=Decimal("3"),
        )
        self.assertFalse(result["needs_reorder"])

    def test_enrich_stock_quantities_pipeline_covers_reorder(self):
        result = enrich_stock_quantities(
            available_stock=5,
            reorder_level=20,
            pipeline_qty=Decimal("20"),
        )
        self.assertEqual(result["stock_with_pipeline"], Decimal("25.000"))
        self.assertFalse(result["needs_reorder"])


if __name__ == "__main__":
    unittest.main()
