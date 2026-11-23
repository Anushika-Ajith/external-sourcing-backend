# OPENAI API CALL FOR STRUCTURING THE RAW EXTRACTED OCR DATA INTO STRUCTURED JSON

import json
import logging
import time
from datetime import date
from typing import List, Literal, Optional

import openai
from openai import OpenAI
from pydantic import BaseModel, Field

from dotenv import load_dotenv
from config import settings

load_dotenv()

logger = logging.getLogger(__name__)


class DocumentAIUnavailableError(Exception):
    """OpenAI API unreachable or overloaded after retries (caller should map to HTTP 503)."""


# Backward-compatible alias used by pipeline / callers.
GeminiUnavailableError = DocumentAIUnavailableError


def _is_transient_openai_error(exc: BaseException) -> bool:
    if isinstance(exc, (openai.RateLimitError, openai.APITimeoutError, openai.APIConnectionError)):
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    msg = str(exc).upper()
    return any(
        token in msg
        for token in ("RATE LIMIT", "TIMEOUT", "UNAVAILABLE", "OVERLOADED", "503", "429")
    )


def _generate_openai_json(
    *,
    prompt: str,
    response_schema: dict,
    temperature: float,
) -> str:
    """
    Call OpenAI with retries and optional fallback model.
    Returns raw JSON text from the model response.
    """
    models: List[str] = []
    for name in (settings.openai_model, settings.openai_fallback_model):
        if name and name not in models:
            models.append(name)

    last_exc: Optional[BaseException] = None
    max_retries = int(settings.openai_max_retries)
    schema_hint = json.dumps(response_schema)
    user_content = (
        f"{prompt}\n\nReturn ONLY valid JSON matching this JSON Schema:\n{schema_hint}"
    )

    for model in models:
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You extract structured document data and respond with JSON only."
                            ),
                        },
                        {"role": "user", "content": user_content},
                    ],
                    temperature=temperature,
                    response_format={"type": "json_object"},
                )
                text = response.choices[0].message.content if response.choices else None
                if not text or not str(text).strip():
                    raise DocumentAIUnavailableError(
                        "OpenAI returned an empty response for document processing."
                    )
                return str(text)
            except DocumentAIUnavailableError:
                raise
            except Exception as exc:
                last_exc = exc
                if not _is_transient_openai_error(exc):
                    raise DocumentAIUnavailableError(
                        f"OpenAI request failed: {exc}"
                    ) from exc

            wait = min(30.0, 1.5 * (2**attempt))
            logger.warning(
                "[openai] model=%s attempt=%s/%s transient error: %s; retry in %.1fs",
                model,
                attempt + 1,
                max_retries,
                last_exc,
                wait,
            )
            time.sleep(wait)

    raise DocumentAIUnavailableError(
        "Document AI is temporarily unavailable (OpenAI overloaded). "
        "Please wait a moment and try uploading again."
    ) from last_exc


# Models

class VendorMaster(BaseModel):
    company_name: Optional[str] = Field(
        description="Legal / trading name of the vendor. On utility bills with only a consumer name, use that name here or leave null if you put it in contact_person."
    )
    vendor_type: Optional[str] = Field(default="Service")
    parent_vendor_id: Optional[str] = None
    is_branch: Optional[bool] = False

    gst_number: Optional[str] = None
    pan_number: Optional[str] = None

    contact_person: Optional[str] = Field(
        description="Primary contact name. For electricity/water/telecom bills, use Consumer Name / account holder name."
    )
    email: Optional[str] = None
    mobile: Optional[str] = None

    address_line1: Optional[str] = Field(
        description="Street number and road (first line of service or billing address from the document)."
    )
    address_line2: Optional[str] = Field(
        description="Second address line: flat, floor, landmark, area (if present)."
    )
    city: Optional[str] = Field(description="City or town from the billing/service address.")
    state: Optional[str] = Field(description="State or province from the billing/service address.")
    country: Optional[str] = Field(default="India")
    postal_code: Optional[str] = Field(
        description="PIN / postal / ZIP code from the address (digits only or as printed)."
    )


class BankAccount(BaseModel):
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    ifsc_code: Optional[str] = None
    branch_name: Optional[str] = None
    account_type: Optional[str] = Field(default="Savings")
    is_primary: Optional[bool] = True


class VendorItem(BaseModel):
    item_name: Optional[str] = None
    product_name: Optional[str] = Field(
        None,
        description="Display product name (prefer this for rate-contract product identity).",
    )
    item_type: Optional[str] = Field(default="Service")
    uom: Optional[str] = Field(
        None,
        description=(
            "Unit of measure (UOM) for this line, e.g. Pack, NOS, KG, EA. "
            "Do NOT put pack size here (e.g. '100 g'); use pack_size for that. "
            "Do NOT put 'Carton' here just because the rate column is labeled Unit Rate/Carton."
        ),
    )
    pack_size: Optional[str] = Field(
        None,
        description=(
            "Pack size as printed (e.g. 100 g, 12, 10x10). "
            "Not the same as UOM. Not the rate column. "
            "Will be canonicalized against OCR alias dictionary."
        ),
    )
    brand: Optional[str] = Field(None, description="Brand / trade name when printed.")
    manufacturer: Optional[str] = Field(
        None, description="Manufacturer when printed."
    )
    category: Optional[str] = Field(None, description="Product category when printed.")
    consumer_unit: Optional[str] = Field(None, description="Consumer/sell unit when printed.")
    consumer_unit_size: Optional[str] = Field(
        None, description="Consumer unit size when printed, e.g. 500ml."
    )
    purchase_unit: Optional[str] = Field(None, description="Purchase / order unit when printed.")
    description: Optional[str] = None
    rate: Optional[float] = Field(
        None, description="Unit rate or price for this line (rate contract / price list)."
    )
    currency: Optional[str] = Field(
        "INR", description="ISO currency for the rate, e.g. INR, USD."
    )
    valid_from: Optional[date] = Field(
        None, description="Start date of the validity period for this line."
    )
    valid_to: Optional[date] = Field(
        None, description="End date of the validity period for this line."
    )
    quantity_balance: Optional[str] = Field(
        None,
        description="Remaining or contract quantity / balance (number or text as on document).",
    )
    line_terms: Optional[str] = Field(
        None,
        description="Line-specific commercial terms (split, MOQ, incoterms) if not covered by contract_terms.",
    )
    erp_code: Optional[str] = Field(
        None, description="Customer/ERP item code if printed on the contract."
    )
    vendor_product_code: Optional[str] = Field(
        None, description="Supplier product or catalog code if printed."
    )
    barcode: Optional[str] = Field(
        None,
        description=(
            "Any product barcode / scan code: EAN/UPC/GTIN/ITF, Code 128/39, "
            "QR/DataMatrix payload, or alphanumeric SKU barcode. Strip spaces "
            "from numeric codes; keep alphanumeric as-is. Do NOT use FSSAI, "
            "phone, GSTIN, or batch-only values here."
        ),
    )
    expiry_date: Optional[date] = Field(
        None,
        description=(
            "Product pack Expiry / Best Before / Use By date when printed on a "
            "product label or packaging. Not the same as rate-contract valid_to."
        ),
    )


class VendorDocument(BaseModel):
    document_type: Optional[str] = Field(
        default="GST",
        description="Type of document, e.g. GST, Electricity Bill, Bank Statement.",
    )
    document_number: Optional[str] = Field(
        description="Consumer number, account number, or certificate number from the document."
    )
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    file: Optional[str] = None


class GRNHeader(BaseModel):
    document_type: Optional[str] = Field(
        default="GRN",
        description="Document type printed or inferred from the document, e.g. GRN, Goods Receipt Note.",
    )
    grn_number: Optional[str] = Field(
        None, description="GRN / Goods Receipt Note number."
    )
    version_no: Optional[int] = Field(
        1, description="GRN version/revision number if printed; otherwise 1."
    )
    vendor_id: Optional[int] = Field(
        None, description="Vendor id only if explicitly printed; do not infer internal database ids."
    )
    po_number: Optional[str] = Field(
        None, description="Purchase order number linked to the receipt."
    )
    po_id: Optional[int] = Field(
        None, description="PO internal id only if explicitly printed; do not infer."
    )
    delivery_note_no: Optional[str] = Field(
        None, description="Delivery note / challan / supplier dispatch note number."
    )
    grn_date: Optional[date] = Field(
        None, description="GRN date / goods receipt date."
    )
    received_by: Optional[int] = Field(
        None, description="Receiver user id only if explicitly printed; do not infer names into ids."
    )
    inspection_status: Optional[str] = Field(
        None, description="Inspection / QC status, e.g. Pending, Accepted, Rejected, Passed."
    )


class GRNLine(BaseModel):
    document_type: Optional[str] = Field(default="GRN")
    grn_number: Optional[str] = None
    version_no: Optional[int] = Field(1)
    line_no: Optional[int] = Field(
        None, description="GRN line number if printed or line sequence number from the table."
    )
    po_number: Optional[str] = Field(
        None, description="Purchase order number for this receipt line."
    )
    po_line_no: Optional[int] = Field(
        None, description="PO line number referenced by this receipt line."
    )
    po_id: Optional[int] = Field(
        None, description="PO internal id only if explicitly printed; do not infer."
    )
    delivery_note_no: Optional[str] = None
    delivery_line_no: Optional[int] = None
    item_id: Optional[int] = Field(
        None, description="Item id only if a numeric item id/code is printed."
    )
    description: Optional[str] = Field(
        None, description="Item/material description from the receipt line."
    )
    uom: Optional[str] = Field(
        None, description="Unit of measure, e.g. NOS, UNIT, KG, EA."
    )
    qty_received: Optional[float] = Field(
        None, description="Quantity received as printed on the GRN."
    )
    qty_accepted: Optional[float] = Field(
        None, description="Quantity accepted after inspection, if printed."
    )
    qty_rejected: Optional[float] = Field(
        None, description="Quantity rejected after inspection, if printed."
    )
    warehouse_id: Optional[int] = Field(
        None, description="Warehouse id only if explicitly printed; do not infer."
    )
    bin_location: Optional[str] = Field(
        None, description="Bin/rack/storage location printed on the document."
    )


class GRNPayload(BaseModel):
    header: Optional[GRNHeader] = None
    lines: Optional[List[GRNLine]] = None


class InvoiceHeader(BaseModel):
    document_type: Optional[str] = Field(
        default="IN",
        description="Invoice document type for insertion. Use IN unless a specific invoice document type is printed.",
    )
    invoice_no: Optional[str] = Field(
        None, description="Vendor invoice number / tax invoice number."
    )
    version_no: Optional[int] = Field(
        1, description="Invoice version/revision number if printed; otherwise 1."
    )
    vendor_id: Optional[int] = Field(
        None, description="Vendor id only if explicitly printed; do not infer internal database ids."
    )
    po_number: Optional[str] = Field(
        None, description="Purchase order number linked to this invoice."
    )
    po_id: Optional[int] = Field(
        None, description="PO internal id only if explicitly printed; do not infer."
    )
    invoice_date: Optional[date] = Field(
        None, description="Invoice date / tax invoice date."
    )
    invoice_amount: Optional[float] = Field(
        None, description="Final invoice total / grand total / amount payable."
    )
    currency_code: Optional[str] = Field(
        "INR", description="Currency code printed or implied, e.g. INR, USD."
    )
    remarks: Optional[str] = Field(
        None, description="Useful invoice notes, payment terms, or extraction remarks."
    )


class InvoiceLine(BaseModel):
    document_type: Optional[str] = Field(default="IN")
    invoice_no: Optional[str] = None
    version_no: Optional[int] = Field(1)
    line_no: Optional[int] = Field(
        None, description="Invoice line number if printed or sequence number from item table."
    )
    po_number: Optional[str] = Field(
        None, description="Purchase order number for this invoice line."
    )
    po_line_no: Optional[int] = Field(
        None, description="PO line number referenced by this invoice line."
    )
    po_id: Optional[int] = Field(
        None, description="PO internal id only if explicitly printed; do not infer."
    )
    item_id: Optional[int] = Field(
        None, description="Numeric item id/code only if printed."
    )
    description: Optional[str] = Field(
        None, description="Item/service description from the invoice line."
    )
    uom: Optional[str] = Field(None, description="Unit of measure, e.g. EA, NOS, KG.")
    qty_invoiced: Optional[float] = Field(
        None, description="Quantity billed/invoiced."
    )
    unit_price: Optional[float] = Field(
        None, description="Unit rate/price for this invoice line."
    )
    tax_amount: Optional[float] = Field(
        None, description="Line tax amount (GST/IGST/CGST/SGST) if printed."
    )
    total_amount: Optional[float] = Field(
        None, description="Line total amount including or excluding tax as printed."
    )


class InvoicePayload(BaseModel):
    header: Optional[InvoiceHeader] = None
    lines: Optional[List[InvoiceLine]] = None


class DispatchNoteHeader(BaseModel):
    document_type: Optional[str] = Field(default="DISPATCH_NOTE")
    dispatch_note_no: Optional[str] = Field(
        None, description="Dispatch note / delivery challan / delivery note number."
    )
    vendor_id: Optional[int] = Field(
        None, description="Vendor id only if explicitly printed; do not infer internal ids."
    )
    po_number: Optional[str] = Field(
        None, description="Purchase order number linked to this dispatch."
    )
    po_id: Optional[int] = Field(
        None, description="PO internal id only if explicitly printed; do not infer."
    )
    dispatch_date: Optional[date] = Field(
        None, description="Dispatch note date / delivery challan date / shipment date."
    )
    dispatch_amount: Optional[float] = Field(
        None, description="Total dispatch value if printed."
    )
    currency_code: Optional[str] = Field(
        "INR", description="Currency code printed or implied."
    )
    remarks: Optional[str] = Field(
        None, description="Useful dispatch, delivery, transporter, vehicle, or shipment notes."
    )


class DispatchNoteLine(BaseModel):
    document_type: Optional[str] = Field(default="DISPATCH_NOTE")
    dispatch_note_no: Optional[str] = None
    line_no: Optional[int] = Field(
        None, description="Dispatch line number if printed or sequence number from item table."
    )
    po_number: Optional[str] = None
    po_line_no: Optional[int] = Field(
        None, description="PO line number referenced by this dispatch line."
    )
    po_id: Optional[int] = None
    item_id: Optional[int] = Field(
        None, description="Numeric item id/code only if printed."
    )
    description: Optional[str] = Field(
        None, description="Item/material description from the dispatch line."
    )
    uom: Optional[str] = Field(None, description="Unit of measure, e.g. EA, NOS, KG.")
    qty_dispatched: Optional[float] = Field(
        None, description="Quantity dispatched / shipped / delivered."
    )
    unit_price: Optional[float] = Field(
        None, description="Unit value/rate if printed."
    )
    total_amount: Optional[float] = Field(
        None, description="Line value/amount if printed."
    )


class DispatchNotePayload(BaseModel):
    header: Optional[DispatchNoteHeader] = None
    lines: Optional[List[DispatchNoteLine]] = None


class SalesLine(BaseModel):
    transaction_date: Optional[date] = Field(
        None,
        description="Sale / transaction date (Date column).",
    )
    product_id: Optional[int] = Field(
        None,
        description="Product ID as printed (Product_ID). Do not invent internal ids.",
    )
    product_name: Optional[str] = Field(
        None,
        description="Product name (Product_Name).",
    )
    category: Optional[str] = Field(
        None,
        description="Product category (Category).",
    )
    qty_sold: Optional[float] = Field(
        None,
        description="Quantity sold (Qty_Sold).",
    )


class SalesPayload(BaseModel):
    lines: Optional[List[SalesLine]] = Field(
        None,
        description="One entry per sales transaction / sold-item line.",
    )


class StockLine(BaseModel):
    product_code: Optional[str] = Field(
        None,
        description="SKU / product code (product_code) as printed.",
    )
    product_name: Optional[str] = Field(
        None,
        description="Product name (product_name).",
    )
    barcode: Optional[str] = Field(
        None,
        description="Barcode / EAN / UPC when printed.",
    )
    category: Optional[str] = Field(
        None,
        description="Product category when printed.",
    )
    brand: Optional[str] = Field(
        None,
        description="Brand / manufacturer when printed.",
    )
    unit: Optional[str] = Field(
        None,
        description="Unit of measure (unit / UOM), e.g. Carton, NOS, KG.",
    )
    available_stock: Optional[float] = Field(
        None,
        description="On-hand / available stock quantity (available_stock).",
    )
    reorder_level: Optional[float] = Field(
        None,
        description="Reorder level / minimum stock (reorder_level).",
    )
    reorder_qty: Optional[float] = Field(
        None,
        description="Reorder quantity (reorder_qty).",
    )
    preferred_supplier: Optional[str] = Field(
        None,
        description="Preferred supplier / vendor when printed.",
    )


class StockPayload(BaseModel):
    lines: Optional[List[StockLine]] = Field(
        None,
        description="One entry per stock / inventory product line.",
    )


class VendorPayload(BaseModel):
    vendor: Optional[VendorMaster] = None
    banks: Optional[List[BankAccount]] = None
    items: Optional[List[VendorItem]] = None
    documents: Optional[List[VendorDocument]] = None
    grn: Optional[GRNPayload] = Field(
        None,
        description="Goods Receipt Note / GRN header and line details when the uploaded document is a GRN.",
    )
    invoice: Optional[InvoicePayload] = Field(
        None,
        description="Invoice header and line details when the uploaded document is a vendor invoice.",
    )
    dispatch_note: Optional[DispatchNotePayload] = Field(
        None,
        description="Dispatch note / delivery challan header and line details.",
    )
    sales: Optional[SalesPayload] = Field(
        None,
        description="Sales report / sales transaction lines when the uploaded document lists products sold.",
    )
    stock: Optional[StockPayload] = Field(
        None,
        description="Stock / inventory / current stock report lines when the document lists on-hand quantities.",
    )
    vendor_rating: Optional[str] = Field(
        None,
        description="Vendor score, grade, or rating on the contract (e.g. 4.2, 85, A, 90/100) as printed.",
    )
    contract_terms: Optional[str] = Field(
        None,
        description="Overall commercial/legal terms: payment, delivery, penalties, acceptance, governing law, etc.",
    )


DocumentKind = Literal[
    "PAN Card",
    "GST Certificate",
    "Aadhaar Card",
    "Bank Statement",
    "Cancelled Cheque",
    "Rate Contract",
    "Price List",
    "BOQ",
    "Purchase Order",
    "GRN",
    "Invoice",
    "Dispatch Note",
    "Sales Report",
    "Stock Report",
    "Product Label",
    "Utility Bill",
    "MSME Certificate",
    "Company Registration",
    "Address Proof",
    "Other",
]


class DocumentClassification(BaseModel):
    document_type: DocumentKind = Field(
        ...,
        description="Single best label for the overall document (Indian vendor onboarding context).",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model confidence in document_type (0–1).",
    )
    rationale: Optional[str] = Field(
        None,
        description="One short phrase citing visible cues (headers, form type, table of rates, etc.).",
    )


# OpenAI Client (single instance)
client = OpenAI(api_key=settings.openai_api_key)


def _heuristic_classify_document(text: str) -> dict:
    """Keyword fallback when OpenAI classification is unavailable."""
    lower = (text or "").lower()
    checks: List[tuple[str, DocumentKind]] = [
        ("rate contract", "Rate Contract"),
        ("schedule of rates", "Rate Contract"),
        ("price list", "Price List"),
        ("bill of quantities", "BOQ"),
        ("goods receipt note", "GRN"),
        ("goods receipt", "GRN"),
        ("grn", "GRN"),
        ("dispatch note", "Dispatch Note"),
        ("delivery challan", "Dispatch Note"),
        ("delivery note", "Dispatch Note"),
        ("shipment note", "Dispatch Note"),
        ("stock report", "Stock Report"),
        ("current stock", "Stock Report"),
        ("available_stock", "Stock Report"),
        ("available stock", "Stock Report"),
        ("reorder_level", "Stock Report"),
        ("reorder level", "Stock Report"),
        ("reorder_qty", "Stock Report"),
        ("inventory report", "Stock Report"),
        ("best before", "Product Label"),
        ("expiry date", "Product Label"),
        ("use by", "Product Label"),
        ("net weight", "Product Label"),
        ("nutritional information", "Product Label"),
        ("sales report", "Sales Report"),
        ("qty sold", "Sales Report"),
        ("quantity sold", "Sales Report"),
        ("qty_sold", "Sales Report"),
        ("purchase order", "Purchase Order"),
        ("tax invoice", "Invoice"),
        ("invoice no", "Invoice"),
        ("invoice number", "Invoice"),
        ("amount payable", "Invoice"),
        ("gstin", "GST Certificate"),
        ("permanent account number", "PAN Card"),
        ("aadhaar", "Aadhaar Card"),
        ("udyam", "MSME Certificate"),
        ("cancelled cheque", "Cancelled Cheque"),
        ("ifsc", "Cancelled Cheque"),
        ("bank statement", "Bank Statement"),
        ("electricity", "Utility Bill"),
        ("consumer no", "Utility Bill"),
    ]
    for needle, doc_type in checks:
        if needle in lower:
            return DocumentClassification(
                document_type=doc_type,
                confidence=0.35,
                rationale=f"Heuristic match on '{needle}' (OpenAI unavailable).",
            ).model_dump()
    return DocumentClassification(
        document_type="Other",
        confidence=0.2,
        rationale="OpenAI unavailable; no strong keyword match.",
    ).model_dump()


def classify_vendor_document(text: str) -> dict:
    """
    Classify vendor-uploaded document text using OpenAI (PAN, rate contract, bill, etc.).
    Returns a plain dict suitable for JSON APIs and storage.
    """
    stripped = (text or "").strip()
    if len(stripped) < 15:
        return DocumentClassification(
            document_type="Other",
            confidence=0.0,
            rationale="Insufficient extracted text to classify reliably.",
        ).model_dump()

    prompt = f"""You classify a single uploaded document from OCR or PDF-extracted text (Indian procurement / vendor onboarding).

Choose exactly ONE document_type from the allowed enum (JSON schema). Prefer the most specific label.

Guidelines:
- PAN Card: Income Tax PAN, permanent account number layout, typical PAN card wording.
- GST Certificate: GSTIN registration, Form GST REG-06 style, tax registration.
- Aadhaar: UIDAI, Aadhaar number pattern, demographic card layout.
- Bank Statement: transaction listing, balance summaries over periods (not just a cheque leaf).
- Cancelled Cheque: cheque image/text with MICR, account number, often "cancelled" or void markings.
- Rate Contract: agreement or schedule with **unit rates**, validity, commercial terms for multiple items/services (not only an invoice total).
- Price List: catalog-style prices without full contract legal framing.
- BOQ: Bill of Quantities / tender schedule with line items and quantities or rates.
- Purchase Order: buyer PO number, ship-to, line quantities from purchaser side.
- GRN: Goods Receipt Note / goods received note / material receipt / inward receipt, usually references a PO, delivery note/challan, receipt date, received quantities, accepted/rejected quantities, warehouse/bin, and inspection/QC status.
- Invoice: tax invoice, bill of supply, debit/credit note as billing document.
- Dispatch Note: dispatch note / delivery challan / delivery note / shipment note from supplier side, usually references a PO and lists dispatched/shipped quantities.
- Sales Report: sales register / daily sales / POS sales listing with sale date, product id/name, category, and quantity sold (not a purchase invoice or GRN).
- Stock Report: current stock / inventory on-hand listing with product_code, product_name, available_stock, and often reorder_level / reorder_qty (not a sales register — no qty_sold / sale dates required).
- Product Label: retail/food packaging or item sticker with brand, product name, net weight, MRP, barcode/EAN digits, batch, packed-on, and/or expiry/best-before (not a multi-item price catalog).
- Utility Bill: electricity, water, gas, telecom with consumer number and consumption charges.
- MSME Certificate: Udyam / MSME registration wording.
- Company Registration: COI, MCA CIN, incorporation certificate.
- Address Proof: primarily identity/address verification document without fitting above (e.g. rental agreement excerpt).
- Other: mixed bundles, illegible OCR, or none of the above fit.

If multiple doc types appear in one file, pick the **dominant** purpose.

OCR text:
---
{stripped[:120000]}
---
"""

    try:
        raw_json = _generate_openai_json(
            prompt=prompt,
            response_schema=DocumentClassification.model_json_schema(),
            temperature=0.1,
        )
        payload = DocumentClassification.model_validate_json(raw_json)
        return payload.model_dump()
    except DocumentAIUnavailableError as exc:
        logger.warning("[openai] classify fallback: %s", exc)
        return _heuristic_classify_document(stripped)


# Extraction Function
def extract_vendor_data(text: str) -> dict:
    """
    Extract structured vendor data from unstructured text using OpenAI.

    Args:
        text (str): Raw vendor-related text (OCR, email, PDF text, etc.)

    Returns:
        VendorPayload: Validated structured vendor data
    """

    prompt = f"""You extract structured vendor / onboarding data and procurement document data from OCR or PDF text.

The text may be: a supplier profile; address / utility bill; OR a **rate contract**, price agreement, schedule of rates, BOQ, commercial tender excerpt, purchase order, invoice, **dispatch note / delivery challan**, **GRN / Goods Receipt Note**, **sales report / sales register**, or **stock / inventory / current stock report**.

Rules:
1. If the text contains any postal address, you MUST populate vendor.address fields: split street into address_line1 (and address_line2 if needed), city, state, postal_code, and country (use India when implied or for Indian addresses).
2. For a single-line address like "12 MG Road, Kochi, Kerala - 682001", parse: address_line1 = street part, city = Kochi, state = Kerala, postal_code = 682001.
3. For utility bills: set contact_person to the consumer / customer / account holder name. Set company_name to that same name if there is no separate company; otherwise use the business name when present.
4. Do not return vendor as null or an all-null vendor when the text clearly includes a name or address — fill every field you can infer.
5. If the text is a bill, add a documents[] entry: document_type (e.g. Electricity Bill), document_number (consumer no / account id), issue_date / expiry_date or due date when present.
6. **Rate contracts / price schedules:** Populate items[] with one entry per priced line. For each line extract ALL of these when printed:
   - product_name / item_name (Product Name)
   - category (Category)
   - brand (Brand)
   - manufacturer (Manufacturer)
   - pack_size (Pack Size, e.g. "100 g" — NOT UOM, NOT the rate)
   - uom (UOM column only, e.g. Pack, NOS — do NOT use "Carton" from a "Unit Rate/Carton" header as UOM)
   - Do NOT invent vendor_code; the backend fills vendor_code from the logged-in vendor account
   - consumer_unit / consumer_unit_size / purchase_unit when present
   - rate (numeric Unit Rate only, e.g. 2280)
   - currency, valid_from / valid_to from contract validity or line dates
   - barcode: ANY barcode/scan code on the line (EAN/UPC/GTIN/ITF/Code128/Code39/QR/alphanumeric SKU)
   - quantity_balance if stated, line_terms for line-specific clauses
   Put overall payment/delivery/penalty clauses in contract_terms. If the document states a vendor performance score or rating, set vendor_rating as a number when possible.
6a. **Product packaging / item label extraction:** If the text is a retail pack, food label, or item sticker (brand + product name + net weight/MRP + barcode and/or expiry/best-before), populate items[] with ONE entry:
   - product_name / item_name, brand, manufacturer, pack_size, uom, rate=MRP
   - barcode: ANY printed scan code (EAN/UPC/GTIN/Code128/Code39/QR/SKU). Strip spaces from numeric codes; keep alphanumeric. NEVER leave null when a code appears.
   - expiry_date: Expiry / Best Before / Use By (NOT Packed On). Do NOT put expiry into valid_to; leave valid_from/valid_to null on packaging labels.
6b. **Purchase Order extraction:** If the document is a purchase order, buyer PO, or order confirmation from a purchaser, add a documents[] entry with document_type "Purchase Order" and document_number set to the PO / order number exactly as printed (e.g. PO-2024-001, 4500012345). Also populate items[] with line items (description, uom, quantity, rate/unit_price). Put the PO date in documents[].issue_date when present.
7. **Bank / cheque / account details:** Populate banks[] when the text shows banking info: bank_name, account_number, ifsc_code (11-character Indian IFSC when present), branch_name, account_type (Savings/Current/OD), is_primary (true for the main account when only one is shown).
8. **GRN / Goods Receipt Note extraction:** If the document is a GRN, goods receipt note, material receipt, inward receipt, or receiving report, populate grn.header and grn.lines:
   - grn.header.document_type: use "GRN" unless another document type is explicitly printed.
   - grn.header.grn_number: GRN / receipt number.
   - grn.header.version_no: printed revision/version, otherwise 1.
   - grn.header.vendor_id: only if a numeric vendor id is printed; do NOT invent internal ids.
   - grn.header.po_number: purchase order number referenced by the receipt.
   - grn.header.po_id: only if an internal PO id is explicitly printed.
   - grn.header.delivery_note_no: delivery note / challan / supplier dispatch note number.
   - grn.header.grn_date: goods receipt / GRN date.
   - grn.header.received_by: only if a numeric receiver user id is printed; do NOT convert names to ids.
   - grn.header.inspection_status: QC / inspection status such as Pending, Passed, Accepted, Rejected.
   - grn.lines: one entry per material/item line. Extract po_line_no, delivery_line_no, item_id when printed, description, uom, qty_received, qty_accepted, qty_rejected, warehouse_id when printed, and bin_location.
   - Repeat header values like grn_number, po_number, delivery_note_no, document_type, version_no into each line when they apply to all lines.
   - If accepted/rejected quantities are absent, leave qty_accepted / qty_rejected null; do not copy qty_received unless the document explicitly says accepted.
9. **Invoice extraction:** If the document is an invoice, tax invoice, bill of supply, or vendor billing document, populate invoice.header and invoice.lines:
   - invoice.header.document_type: use "IN" unless another invoice document type is explicitly printed.
   - invoice.header.invoice_no: invoice / tax invoice number.
   - invoice.header.version_no: printed revision/version, otherwise 1.
   - invoice.header.vendor_id: only if a numeric vendor id is printed; do NOT invent internal ids.
   - invoice.header.po_number: purchase order number referenced by the invoice.
   - invoice.header.po_id: only if an internal PO id is explicitly printed.
   - invoice.header.invoice_date: invoice date.
   - invoice.header.invoice_amount: grand total / invoice total / amount payable.
   - invoice.header.currency_code: currency such as INR, USD; default INR when implied by Indian GST invoice.
   - invoice.header.remarks: payment terms or concise useful invoice notes when present.
   - invoice.lines: one entry per billed item/service line. Extract po_line_no, item_id when printed, description, uom, qty_invoiced, unit_price, tax_amount, and total_amount.
   - Repeat header values like invoice_no, po_number, document_type, version_no into each line when they apply to all lines.
   - If line tax is split as CGST/SGST/IGST, set tax_amount to the sum for that line when possible.
   - If total_amount is absent but qty_invoiced and unit_price are present, calculate total_amount as quantity * unit price only when the document clearly excludes tax; otherwise leave null.
10. **Dispatch note / delivery challan extraction:** If the document is a dispatch note, delivery note, delivery challan, shipment note, or supplier dispatch document, populate dispatch_note.header and dispatch_note.lines:
   - dispatch_note.header.document_type: use "DISPATCH_NOTE".
   - dispatch_note.header.dispatch_note_no: dispatch note / delivery challan / delivery note number.
   - dispatch_note.header.vendor_id: only if a numeric vendor id is printed; do NOT invent internal ids.
   - dispatch_note.header.po_number: purchase order number referenced by the dispatch note.
   - dispatch_note.header.po_id: only if an internal PO id is explicitly printed.
   - dispatch_note.header.dispatch_date: dispatch / delivery challan / shipment date.
   - dispatch_note.header.dispatch_amount: total dispatch value if printed.
   - dispatch_note.header.currency_code: currency such as INR, USD; default INR when implied.
   - dispatch_note.header.remarks: vehicle number, transporter, LR/AWB, delivery terms, or concise useful dispatch notes when present.
   - dispatch_note.lines: one entry per dispatched item line. Extract po_line_no, item_id when printed, description, uom, qty_dispatched, unit_price, and total_amount.
   - Repeat header values like dispatch_note_no, po_number, document_type into each line when they apply to all lines.
   - If total_amount is absent but qty_dispatched and unit_price are present, calculate total_amount as quantity * unit price only when the document clearly shows pricing; otherwise leave null.
11. **Sales report / sales register extraction:** If the document is a sales report, sales register, daily sales sheet, POS sales listing, or product sales summary with quantities sold, populate sales.lines:
   - sales.lines: one entry per sold product / sales transaction line.
   - transaction_date: sale / transaction date (Date).
   - product_id: numeric product id when printed (Product_ID); do NOT invent ids.
   - product_name: product name / item name (Product_Name).
   - category: product category when printed (Category).
   - qty_sold: quantity sold (Qty_Sold).
   - Prefer columns labeled Date, Product_ID, Product_Name, Category, Qty_Sold when present.
   - Skip header/total/summary rows that are not actual product sale lines.
   - Do NOT put stock/inventory on-hand rows into sales.lines.
12. **Stock / inventory / current stock report extraction:** If the document is a stock report, inventory report, current stock listing, or on-hand quantity sheet (columns like product_code, product_name, available_stock, reorder_level, reorder_qty, unit), populate stock.lines:
   - stock.lines: one entry per product stock line.
   - product_code: SKU / product code (product_code) when printed.
   - product_name: product name (product_name).
   - available_stock: on-hand / available quantity (available_stock).
   - reorder_level: reorder / minimum level when printed.
   - reorder_qty: reorder quantity when printed.
   - unit: unit of measure (unit / UOM) when printed.
   - barcode, category, brand, preferred_supplier when printed.
   - Prefer columns labeled product_code, product_name, available_stock, reorder_level, reorder_qty, unit when present.
   - Skip header/total/summary rows that are not actual product stock lines.
   - Do NOT put stock rows into sales.lines, and do NOT put sales qty_sold rows into stock.lines.
13. Use null only for fields that are truly absent from the text. Do not hallucinate internal ids such as vendor_id, po_id, warehouse_id, product_id, or received_by.

Document text:
---
{text}
---
"""

    raw_json = _generate_openai_json(
        prompt=prompt,
        response_schema=VendorPayload.model_json_schema(),
        temperature=0.2,
    )
    payload = VendorPayload.model_validate_json(raw_json)
    return payload.model_dump()