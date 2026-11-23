"""Tests for 3-way match invoice review queue."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.three_way_match_queue_service import list_three_way_match_queue


class ThreeWayMatchQueueServiceTests(unittest.TestCase):
    @patch("services.three_way_match_queue_service.list_all_grn_headers")
    @patch("services.three_way_match_queue_service.list_all_invoice_headers")
    def test_only_includes_pos_with_invoice_and_grn(
        self, mock_invoices, mock_grns
    ):
        mock_invoices.return_value = [
            {
                "po_number": "PO-1",
                "invoice_no": "INV-1",
                "invoice_date": "2026-01-10",
                "invoice_amount": 100,
                "currency_code": "INR",
                "status": "DRAFT",
                "vendor_id": 10,
                "po_id": 1,
            },
            {
                "po_number": "PO-2",
                "invoice_no": "INV-2",
                "invoice_date": "2026-01-11",
                "invoice_amount": 200,
                "currency_code": "INR",
                "status": "DRAFT",
                "vendor_id": 11,
                "po_id": 2,
            },
        ]
        mock_grns.return_value = [
            {
                "po_number": "PO-1",
                "grn_number": "GRN-1",
                "grn_date": "2026-01-09",
                "status": "OPEN",
                "vendor_id": 10,
                "po_id": 1,
            },
            {
                "po_number": "PO-3",
                "grn_number": "GRN-3",
                "grn_date": "2026-01-08",
                "status": "OPEN",
                "vendor_id": 12,
                "po_id": 3,
            },
        ]

        result = list_three_way_match_queue(
            tenant_id=1,
            company_id=3,
            page=1,
            limit=100,
            db=None,
        )

        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertEqual(row["po_number"], "PO-1")
        self.assertEqual(row["invoice_no"], "INV-1")
        self.assertEqual(row["grn_no"], "GRN-1")
        self.assertEqual(row["invoice_count"], 1)
        self.assertEqual(row["grn_count"], 1)

    @patch("services.three_way_match_queue_service.list_all_grn_headers")
    @patch("services.three_way_match_queue_service.list_all_invoice_headers")
    def test_pagination(self, mock_invoices, mock_grns):
        mock_invoices.return_value = [
            {"po_number": f"PO-{i}", "invoice_no": f"INV-{i}", "invoice_date": f"2026-01-{10 + i:02d}"}
            for i in range(3)
        ]
        mock_grns.return_value = [
            {"po_number": f"PO-{i}", "grn_number": f"GRN-{i}", "grn_date": f"2026-01-{9 + i:02d}"}
            for i in range(3)
        ]

        result = list_three_way_match_queue(
            tenant_id=1,
            company_id=3,
            page=2,
            limit=1,
            db=None,
        )

        self.assertEqual(result["total"], 3)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["skip"], 1)


if __name__ == "__main__":
    unittest.main()
