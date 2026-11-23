"""Schemas for the document OCR service API."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class OcrFileReceived(BaseModel):
    filename: str
    content_type: str


class OcrExtractResponse(BaseModel):
    status: str
    combined_text_length: int
    combined_text: str = ""
    document_classification: Any
    raw_text: Any


class OcrVendorProcessResponse(BaseModel):
    vendor_id: int
    status: str
    files_received: List[OcrFileReceived]
    document_classification: Optional[Any] = None
    vendor_items_inserted: Optional[int] = None
    vendor_rates_inserted: Optional[int] = None
    items_rates_inserted: Optional[int] = None
    vendor_banks_inserted: Optional[int] = None
    vendor_documents_inserted: Optional[int] = None
    grn_import: Optional[Any] = None
    invoice_import: Optional[Any] = None
    invoice_notifications: Optional[Any] = None
    dispatch_note_import: Optional[Any] = None
    sales_import: Optional[Any] = None
    rfq_revisions: Optional[List[Any]] = None
    rate_contract_match_reviews: Optional[List[Any]] = None
    raw_text: Optional[Any] = None


class OcrSalesProcessResponse(BaseModel):
    status: str
    files_received: List[OcrFileReceived]
    source: Optional[str] = None
    document_classification: Optional[Any] = None
    sales_import: Optional[Any] = None
    stock_trend_sync: Optional[Any] = None
    raw_text: Optional[Any] = None
    performance: Optional[Any] = None


class OcrStockProcessResponse(BaseModel):
    status: str
    files_received: List[OcrFileReceived]
    source: Optional[str] = None
    document_classification: Optional[Any] = None
    stock_import: Optional[Any] = None
    raw_text: Optional[Any] = None


class OcrPoExtractResponse(BaseModel):
    status: str
    document_classification: Any
    raw_text: Any
    business_fields: Dict[str, Any]
