"""Shared constants for the document OCR service."""

from pathlib import Path

ALLOWED_OCR_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
}

ALLOWED_SALES_OCR_MIME_TYPES = {
    *ALLOWED_OCR_MIME_TYPES,
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
}

ALLOWED_STOCK_OCR_MIME_TYPES = {
    *ALLOWED_OCR_MIME_TYPES,
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

OCR_UPLOAD_DIR = Path("/tmp/vendor_ocr_sync")
OCR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

PO_OCR_UPLOAD_DIR = Path("uploads") / "po_ocr_jobs"
PO_OCR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
