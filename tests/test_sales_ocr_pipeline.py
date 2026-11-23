"""Tests for sales OCR pipeline fallback behavior."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.sales_ocr_pipeline import run_sales_ocr_pipeline


class TestSalesOcrPipeline(unittest.TestCase):
    @patch("services.sales_ocr_pipeline.extract_sales_from_paths")
    @patch("services.ocr_sales_import.parse_sales_pdf_fast")
    def test_selectable_pdf_falls_back_to_ocr_http_when_local_parse_empty(
        self,
        mock_parse_pdf,
        mock_extract_sales,
    ) -> None:
        mock_parse_pdf.return_value = []
        mock_extract_sales.return_value = (
            "",
            {"document_type": "Sales Report"},
            {
                "sales": {
                    "lines": [
                        {
                            "transaction_date": "2026-05-01",
                            "product_name": "Brownys",
                            "category": "Bakery",
                            "qty_sold": 5,
                        }
                    ]
                }
            },
        )

        _, _, raw = run_sales_ocr_pipeline(["/tmp/stock_inventory_formatted.pdf"])

        mock_extract_sales.assert_called_once_with(
            ["/tmp/stock_inventory_formatted.pdf"],
            perf_label="ocr_http:sales_extract [stock_inventory_formatted.pdf]",
        )
        self.assertEqual(len(raw["sales"]["lines"]), 1)


if __name__ == "__main__":
    unittest.main()
