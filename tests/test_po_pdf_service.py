"""Unit tests for purchase order PDF generation."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.po_pdf_service import (
    build_purchase_order_pdf_bytes,
    generate_and_attach_po_pdf,
    persist_po_pdf,
)


def _header(**overrides):
    base = dict(
        po_id=101,
        po_number="PO-2026-0001",
        version_no=1,
        po_date=date(2026, 11, 16),
        vendor_id=7,
        vendor_name_snapshot="Acme Supplies",
        currency_code="INR",
        status="OPEN",
        approval_status="DRAFT",
        document_type="PO",
        tenant_id=1,
        company_id=1,
        source_type="STOCK_MASTER",
        expected_delivery_date=date(2026, 11, 30),
        incoterms="FOB",
        delivery_terms="Net 30",
        current_editor="BUYER",
        document_number=None,
        remarks="Created from stock spike",
        po_total_amount=Decimal("200.00"),
        tax_total_amount=Decimal("36.00"),
        discount_amount=Decimal("0.00"),
        net_total_amount=Decimal("236.00"),
        attachment_path=None,
        attachment_count=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _line(**overrides):
    base = dict(
        line_no=1,
        item_id=55,
        item_no="SKU-1",
        description="Basmati Rice 25kg",
        uom="BAG",
        qty_ordered=Decimal("2"),
        unit_price=Decimal("100.00"),
        po_item_rate=Decimal("100.00"),
        tax_percent=Decimal("18"),
        total_amount=Decimal("236.00"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class PoPdfServiceTests(unittest.TestCase):
    def test_build_pdf_contains_pdf_header(self):
        pdf = build_purchase_order_pdf_bytes(_header(), [_line()])
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 500)

    def test_persist_and_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("services.po_pdf_service.PO_ATTACHMENT_DIR", root):
                header = _header()
                path = generate_and_attach_po_pdf(header, [_line()])
                self.assertIsNotNone(path)
                self.assertTrue(Path(path).is_file())
                self.assertEqual(header.attachment_path, path)
                self.assertEqual(header.attachment_count, 1)
                # Second call without overwrite should keep existing path
                again = generate_and_attach_po_pdf(header, [_line()])
                self.assertEqual(again, path)
                self.assertEqual(header.attachment_count, 1)

    def test_persist_po_pdf_writes_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("services.po_pdf_service.PO_ATTACHMENT_DIR", root):
                path = persist_po_pdf(b"%PDF-1.4 test", 42, "PO/100")
                saved = Path(path)
                self.assertTrue(saved.is_file())
                self.assertEqual(saved.read_bytes(), b"%PDF-1.4 test")
                self.assertIn("PO-100", saved.name)


if __name__ == "__main__":
    unittest.main()
