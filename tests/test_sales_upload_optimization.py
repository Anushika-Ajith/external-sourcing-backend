"""Unit tests for sales upload performance optimizations."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.ocr_sales_import import (
    collect_lines_from_files,
    insert_sales_transaction_lines,
    parse_sales_csv,
    parse_sales_plaintext,
)
from services.sales_upload_perf import SalesUploadPerf


class TestSalesUploadPerf(unittest.TestCase):
    def test_stage_timings(self) -> None:
        perf = SalesUploadPerf()
        with perf.stage("file_save"):
            pass
        with perf.stage("sales_insert"):
            pass
        summary = perf.as_dict()
        self.assertIn("file_save", summary)
        self.assertIn("sales_insert", summary)
        self.assertIn("total", summary)


class TestDeterministicSalesParsing(unittest.TestCase):
    def test_parse_csv_plaintext(self) -> None:
        text = "Date,Product_ID,Product_Name,Category,Qty_Sold\n2024-01-01,101,Widget A,Tools,5\n"
        lines = parse_sales_plaintext(text)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["product_id"], 101)
        self.assertEqual(lines[0]["qty_sold"], Decimal("5"))

    def test_parse_sales_csv_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Date", "Product_ID", "Product_Name", "Category", "Qty_Sold"])
            writer.writerow(["2024-02-01", "202", "Widget B", "Hardware", "3"])
            path = fh.name
        try:
            lines = parse_sales_csv(path)
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["product_name"], "Widget B")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_parse_datewise_stacked_pdf_text(self) -> None:
        text = (
            "Date\nProduct_Name\nCategory\nQty_Sold\n"
            "01-May-2026\nBritannia 100 g\nBiscuits\n15\n"
            "02-May-2026\nGood Day 100 g\nBiscuits\n8\n"
        )
        lines = parse_sales_plaintext(text)
        self.assertEqual(len(lines), 2)
        self.assertIsNone(lines[0].get("product_id"))
        self.assertEqual(lines[0]["product_name"], "Britannia 100 g")
        self.assertEqual(lines[0]["qty_sold"], Decimal("15"))

    def test_parse_datewise_pdf_with_product_code_and_uom(self) -> None:
        text = (
            "Date\nProduct Code\nProduct Name\nCategory\nUOM\nQty Sold\n"
            "01-May-2026\nERP00044\nBrownys\nBakery\nBOX\n5\n"
            "01-May-2026\nERP00043\nCookies\nBiscuits\nCARTON\n6\n"
            "02-May-2026\nERP00039\nCream Ice\nIce Cream\nBOX\n8\n"
        )
        lines = parse_sales_plaintext(text)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["product_name"], "Brownys")
        self.assertEqual(lines[0]["qty_sold"], Decimal("5"))
        self.assertIsNone(lines[0].get("product_id"))

    def test_parse_brownys_cookies_cream_ice_pdf_fast(self) -> None:
        path = (
            "/home/anushika-ajith/Downloads/"
            "Brownys_Cookies_Cream_Ice_Datewise_Sales_Register_Last_3_Months.pdf"
        )
        if not Path(path).exists():
            self.skipTest("fixture PDF not available")
        lines, scan = collect_lines_from_files([path])
        self.assertEqual(scan, [])
        self.assertGreater(len(lines), 200)

    def test_parse_datewise_pdf_fast(self) -> None:
        path = (
            "/home/anushika-ajith/Downloads/"
            "SUP004_Datewise_Sales_Register_Last_3_Months (3).pdf"
        )
        if not Path(path).exists():
            self.skipTest("fixture PDF not available")
        lines, scan = collect_lines_from_files([path])
        self.assertEqual(scan, [])
        self.assertGreater(len(lines), 200)

    def test_collect_lines_csv_bypasses_scan(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Date", "Product_ID", "Product_Name", "Category", "Qty_Sold"])
            writer.writerow(["2024-03-01", "303", "Widget C", "Parts", "7"])
            path = fh.name
        try:
            lines, scan_files = collect_lines_from_files([path])
            self.assertEqual(len(lines), 1)
            self.assertEqual(scan_files, [])
        finally:
            Path(path).unlink(missing_ok=True)


class TestBulkSalesInsert(unittest.TestCase):
    def test_resolve_product_ids_applies_to_all_lines(self) -> None:
        from services.ocr_sales_import import _resolve_product_ids_by_name

        db = MagicMock()
        db.query.return_value.filter.return_value.all.side_effect = [
            [],  # no existing products by name
            [(232,)],  # existing product ids after create
        ]
        db.query.return_value.scalar.return_value = 231

        lines = [
            {
                "product_name": "Britannia 100 g",
                "category": "Biscuits",
                "transaction_date": date(2026, 5, 1),
                "qty_sold": Decimal("15"),
            },
            {
                "product_name": "Britannia 100 g",
                "category": "Biscuits",
                "transaction_date": date(2026, 5, 2),
                "qty_sold": Decimal("9"),
            },
        ]

        created = _resolve_product_ids_by_name(db, lines, created_by=1)
        self.assertEqual(created, 1)
        self.assertEqual(lines[0]["product_id"], 232)
        self.assertEqual(lines[1]["product_id"], 232)

    def test_resolve_product_ids_creates_multiple_distinct_products(self):
        from services.ocr_sales_import import _resolve_product_ids_by_name

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        db.query.return_value.scalar.return_value = 255

        lines = [
            {
                "product_name": "Brownys",
                "category": "Bakery",
                "transaction_date": date(2026, 5, 1),
                "qty_sold": Decimal("5"),
            },
            {
                "product_name": "Cookies",
                "category": "Biscuits",
                "transaction_date": date(2026, 5, 1),
                "qty_sold": Decimal("6"),
            },
        ]

        created = _resolve_product_ids_by_name(db, lines, created_by=1)
        self.assertEqual(created, 2)
        self.assertEqual(lines[0]["product_id"], 256)
        self.assertEqual(lines[1]["product_id"], 257)
        self.assertEqual(db.add.call_count, 2)

    def test_bulk_insert_uses_add_all(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.all.side_effect = [
            [],  # existing products for ensure
            [(101,)],  # existing product ids
        ]

        lines = [
            {
                "transaction_date": date(2024, 1, 1),
                "product_id": 101,
                "product_name": "A",
                "category": "Cat",
                "qty_sold": Decimal("1"),
            },
            {
                "transaction_date": date(2024, 1, 2),
                "product_id": 101,
                "product_name": "A",
                "category": "Cat",
                "qty_sold": Decimal("2"),
            },
        ]

        with patch("services.ocr_sales_import.SalesTransaction") as mock_model:
            mock_rows = [MagicMock(sales_transaction_id=i + 1) for i in range(2)]
            mock_model.side_effect = mock_rows
            result = insert_sales_transaction_lines(db, lines)

        db.add_all.assert_called_once()
        db.flush.assert_called()
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["affected_product_ids"], [101])


class TestAffectedProductTrendScope(unittest.TestCase):
    def test_trends_accepts_product_ids(self) -> None:
        from services.sales_transaction_trends import list_sales_trends_by_product

        db = MagicMock()
        chain = db.query.return_value
        chain.filter.return_value = chain
        chain.group_by.return_value = chain
        chain.order_by.return_value = chain
        chain.offset.return_value = chain
        chain.limit.return_value.all.return_value = []

        result = list_sales_trends_by_product(db, product_ids=[1, 2, 3])
        self.assertEqual(result, [])
        self.assertTrue(db.query.called)


if __name__ == "__main__":
    unittest.main()
