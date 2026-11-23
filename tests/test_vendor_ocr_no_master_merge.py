"""Operational OCR must not import vendor master / banks / registration docs."""

import inspect

from services.document_ocr import registration_service, vendor_service


def test_process_vendor_documents_does_not_touch_vendor_identity():
    src = inspect.getsource(vendor_service.process_vendor_documents)
    assert "merge_ocr_vendor_master" not in src
    assert "insert_ocr_vendor_banks" not in src
    assert "insert_ocr_vendor_documents" not in src


def test_process_registration_documents_still_updates_vendor_identity():
    src = inspect.getsource(registration_service.process_registration_documents)
    assert "merge_ocr_vendor_master" in src
    assert "insert_ocr_vendor_banks" in src
    assert "insert_ocr_vendor_documents" in src
