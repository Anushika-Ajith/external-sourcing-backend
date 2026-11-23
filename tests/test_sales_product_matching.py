"""Unit tests for sales product matching on upload."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.ocr_sales_import import (
    csv_has_product_id_column,
    insert_sales_transaction_lines,
    source_has_product_id_column,
)
from services.sales_product_matching_engine import (
    MATCH_COLOR_GREEN,
    MATCH_COLOR_RED,
    MATCH_COLOR_YELLOW,
    ProductCatalogCandidate,
    SalesProductMatchingEngine,
    SalesProductMatchResult,
    match_product_name_to_aliases,
)


class TestCsvProductIdColumnDetection(unittest.TestCase):
    def test_detects_product_id_header(self):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".csv",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write("Date,Product Name,Category,Qty Sold\n")
            fh.write("2024-01-01,Widget,A,1\n")
            path = fh.name
        try:
            self.assertFalse(csv_has_product_id_column(path))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_detects_item_id_header(self):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".csv",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write("Date,Item ID,Product Name,Category,Qty Sold\n")
            fh.write("2024-01-01,10,Widget,A,1\n")
            path = fh.name
        try:
            self.assertTrue(csv_has_product_id_column(path))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_source_has_product_id_column(self):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".csv",
            delete=False,
            encoding="utf-8",
        ) as fh:
            fh.write("Date,Product_ID,Product Name,Qty Sold\n")
            fh.write("2024-01-01,5,Widget,1\n")
            path = fh.name
        try:
            self.assertTrue(source_has_product_id_column([path]))
        finally:
            Path(path).unlink(missing_ok=True)


class TestSalesProductMatchingEngine(unittest.TestCase):
    def _candidate(self, product_id: int, name: str) -> ProductCatalogCandidate:
        product = MagicMock()
        product.product_id = product_id
        product.name = name
        product.category = "Cat"
        return ProductCatalogCandidate(
            product_id=product_id,
            name=name,
            category="Cat",
            aliases=[name],
            product=product,
        )

    def test_exact_single_match_is_green(self):
        candidates = [self._candidate(1, "Paracetamol 500mg")]
        engine = SalesProductMatchingEngine(candidates=candidates)
        db = MagicMock()
        result = engine.run_for_line(
            db=db,
            product_name="Paracetamol 500mg",
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.match_color, MATCH_COLOR_GREEN)
        self.assertEqual(result.match_type, "exact")

    def test_multiple_exact_matches_are_yellow(self):
        candidates = [
            self._candidate(1, "Paracetamol 500mg"),
            self._candidate(2, "Paracetamol 500mg"),
        ]
        engine = SalesProductMatchingEngine(candidates=candidates)
        with patch(
            "services.sales_foundation_catalog.collect_vendor_foundation_matches",
            return_value=[],
        ), patch(
            "services.sales_product_matching_engine.match_via_stock_master",
            return_value=None,
        ):
            result = engine.run_for_line(db=MagicMock(), product_name="Paracetamol 500mg")
        self.assertFalse(result.matched)
        self.assertEqual(result.match_color, MATCH_COLOR_YELLOW)
        self.assertGreaterEqual(len(result.ranked_candidates), 2)

    def test_no_match_is_red(self):
        candidates = [self._candidate(1, "Aspirin")]
        engine = SalesProductMatchingEngine(candidates=candidates)
        with patch(
            "services.sales_foundation_catalog.collect_vendor_foundation_matches",
            return_value=[],
        ), patch(
            "services.sales_product_matching_engine.partition_pool_embedding_matches",
            return_value=([], []),
        ), patch(
            "services.sales_product_matching_engine.match_via_stock_master",
            return_value=None,
        ):
            result = engine.run_for_line(
                db=MagicMock(),
                product_name="Totally Unknown Product XYZ",
            )
        self.assertFalse(result.matched)
        self.assertEqual(result.match_color, MATCH_COLOR_RED)

    def test_spelling_variant_brownys_matches_brownies(self):
        candidates = [self._candidate(7, "Brownies")]
        engine = SalesProductMatchingEngine(candidates=candidates)
        with patch(
            "services.sales_foundation_catalog.collect_vendor_foundation_matches",
            return_value=[],
        ), patch(
            "services.sales_product_matching_engine.match_via_stock_master",
            return_value=None,
        ), patch(
            "services.sales_product_matching_engine.partition_pool_embedding_matches"
        ) as mock_embed:
            result = engine.run_for_line(
                db=MagicMock(),
                product_name="Brownys",
                category="Bakery",
            )
        mock_embed.assert_not_called()
        self.assertTrue(result.matched)
        self.assertEqual(result.match_color, MATCH_COLOR_GREEN)
        self.assertEqual(result.match_type, "auto")

    def test_match_cache_avoids_repeat_work(self):
        candidates = [self._candidate(7, "Brownies")]
        engine = SalesProductMatchingEngine(candidates=candidates)
        db = MagicMock()
        first = engine.run_for_line(db=db, product_name="Brownys", category="Bakery")
        second = engine.run_for_line(db=db, product_name="Brownys", category="Bakery")
        self.assertIs(first, second)
        self.assertTrue(second.matched)

    def test_alias_multiple_matches_are_yellow(self):
        candidates = [
            ProductCatalogCandidate(
                product_id=1,
                name="Catalog A",
                aliases=["widget"],
                product=MagicMock(product_id=1, name="Catalog A"),
            ),
            ProductCatalogCandidate(
                product_id=2,
                name="Catalog B",
                aliases=["widget"],
                product=MagicMock(product_id=2, name="Catalog B"),
            ),
        ]
        hits = match_product_name_to_aliases("widget", candidates)
        self.assertEqual(len(hits), 2)


class TestInsertWithProductMatching(unittest.TestCase):
    def test_green_lines_inserted_yellow_deferred(self):
        db = MagicMock()
        green_product = MagicMock()
        green_product.product_id = 42
        green_match = SalesProductMatchResult(
            matched=True,
            product=green_product,
            match_type="exact",
            match_color=MATCH_COLOR_GREEN,
            line_status="CONFIRMED",
            match_review={"matchColor": "green"},
        )
        yellow_match = SalesProductMatchResult(
            matched=False,
            match_type="multiple",
            match_color=MATCH_COLOR_YELLOW,
            line_status="PENDING_SELECT",
            match_review={
                "matchColor": "yellow",
                "rankedCandidates": [{"productId": 1}, {"productId": 2}],
            },
            ranked_candidates=[{"productId": 1}, {"productId": 2}],
        )

        engine = MagicMock()
        engine.run_for_line.side_effect = [green_match, yellow_match]

        db.query.return_value.filter.return_value.all.return_value = [(42,)]

        lines = [
            {
                "transaction_date": date(2024, 1, 1),
                "product_name": "Green Product",
                "category": "A",
                "qty_sold": Decimal("5"),
            },
            {
                "transaction_date": date(2024, 1, 2),
                "product_name": "Ambiguous Product",
                "category": "B",
                "qty_sold": Decimal("3"),
            },
        ]

        def _assign_ids(rows):
            for i, row in enumerate(rows):
                row.sales_transaction_id = 100 + i

        db.add_all.side_effect = _assign_ids

        with patch(
            "services.ocr_sales_import.build_sales_match_candidates",
            return_value=([], {}, {}),
        ), patch(
            "services.ocr_sales_import.SalesProductMatchingEngine",
            return_value=engine,
        ):
            result = insert_sales_transaction_lines(
                db,
                lines,
                use_product_matching=True,
            )

        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["match_summary"]["green"], 1)
        self.assertEqual(result["match_summary"]["yellow"], 1)
        self.assertEqual(len(result["pending_match_reviews"]), 1)
        self.assertEqual(result["pending_match_reviews"][0]["matchColor"], "yellow")
        db.add_all.assert_called_once()


if __name__ == "__main__":
    unittest.main()
