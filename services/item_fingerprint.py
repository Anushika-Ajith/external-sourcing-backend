"""Build structured item fingerprints and embedding text from PO line data."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from openai import OpenAI
from pydantic import BaseModel, Field

from config import settings
from models import PurchaseOrderLine, VendorItem

logger = logging.getLogger(__name__)

FINGERPRINT_FIELDS = (
    "normalized_name",
    "brand",
    "strength",
    "dosage_form",
    "pack_size",
    "manufacturer",
    "uom",
    "generic",
    "category",
    "product_subcategory",
    "variant_flavour",
    "product_type",
    "pack_type",
    "net_quantity",
    "net_weight",
    "pack_configuration",
    "vendor_code",
)

PRODUCT_FINGERPRINT_CORE_INSTRUCTIONS = """Create the Product Fingerprint for the product.

Do not assume or invent any brand, manufacturer, product name, flavour, pack size, barcode, ingredients, price, or other product information.

Use ONLY the information provided in the OCR text, product image, barcode data, supplier product master, or other supplied source data.

Extract and normalize the following attributes where available:
- Product Category
- Product Subcategory
- Brand
- Product Name
- Variant / Flavour
- Product Type
- Pack Type
- Net Quantity
- Net Weight
- UOM
- Pack Configuration
- Manufacturer

If a field cannot be determined from the supplied data, omit it or set it to null. Do not guess."""

PRODUCT_FINGERPRINT_JSON_FIELD_GUIDE = """Map extracted attributes to JSON fields:
- Product Category -> category
- Product Subcategory -> product_subcategory
- Brand -> brand
- Product Name -> normalized_name (uppercase canonical name without inventing missing words)
- Variant / Flavour -> variant_flavour
- Product Type -> product_type
- Pack Type -> pack_type
- Net Quantity -> net_quantity
- Net Weight -> net_weight
- UOM -> uom
- Pack Configuration -> pack_configuration
- Manufacturer -> manufacturer
- Printed pack size (when explicitly present) -> pack_size
- Molecule / generic name (when explicitly present) -> generic
- Strength / dosage (when explicitly present) -> strength, dosage_form
- Do NOT invent vendor_code (backend fills it when applicable)."""

# Required on product_fingerprint for rate-contract uploads.
RATE_CONTRACT_MANDATORY_FINGERPRINT_FIELDS = (
    "category",
    "manufacturer",
    "brand",
    "uom",
    "pack_size",
    "vendor_code",
)

_openai_client: Optional[OpenAI] = None


class ItemFingerprintExtract(BaseModel):
    normalized_name: Optional[str] = Field(
        None,
        description="Product Name — canonical product name from source data, uppercased.",
    )
    brand: Optional[str] = Field(None, description="Brand or trade name.")
    strength: Optional[str] = Field(
        None, description="Strength/concentration, e.g. 650 MG."
    )
    dosage_form: Optional[str] = Field(
        None, description="Dosage form, e.g. TABLET, CAPSULE, SYRUP."
    )
    pack_size: Optional[str] = Field(
        None, description="Pack size or count when explicitly printed, e.g. 15, 10X10."
    )
    manufacturer: Optional[str] = Field(None, description="Manufacturer name.")
    generic: Optional[str] = Field(
        None, description="Generic/molecule name, e.g. PARACETAMOL."
    )
    uom: Optional[str] = Field(None, description="Unit of measure, e.g. STRIP, NOS, BOX.")
    category: Optional[str] = Field(None, description="Product Category.")
    product_subcategory: Optional[str] = Field(None, description="Product Subcategory.")
    variant_flavour: Optional[str] = Field(None, description="Variant or flavour.")
    product_type: Optional[str] = Field(None, description="Product Type.")
    pack_type: Optional[str] = Field(None, description="Pack Type, e.g. BOX, CARTON.")
    net_quantity: Optional[str] = Field(None, description="Net Quantity when printed.")
    net_weight: Optional[str] = Field(None, description="Net Weight when printed.")
    pack_configuration: Optional[str] = Field(
        None, description="Pack Configuration when printed."
    )
    vendor_code: Optional[str] = Field(
        None, description="Owning vendor business code (omit — backend fills)."
    )


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = settings.openai_api_key
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for item fingerprint parsing")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def _clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _upper(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return value.upper()


def normalize_fingerprint(
    raw: Dict[str, Any],
    *,
    uom_fallback: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize fingerprint keys and casing for storage and embedding text."""
    fingerprint: Dict[str, Any] = {}
    for key in FINGERPRINT_FIELDS:
        val = _clean_str(raw.get(key))
        if key in (
            "normalized_name",
            "brand",
            "strength",
            "dosage_form",
            "manufacturer",
            "generic",
            "uom",
            "category",
            "product_subcategory",
            "variant_flavour",
            "product_type",
            "pack_type",
            "net_quantity",
            "net_weight",
            "pack_configuration",
            "vendor_code",
        ):
            val = _upper(val)
        fingerprint[key] = val

    if not fingerprint.get("uom") and uom_fallback:
        fingerprint["uom"] = _upper(uom_fallback)

    if fingerprint.get("normalized_name") and not fingerprint.get("generic"):
        fingerprint["generic"] = extract_generic_name(fingerprint)

    return {k: v for k, v in fingerprint.items() if v is not None}


def extract_generic_name(fingerprint: Dict[str, Any]) -> Optional[str]:
    explicit = _upper(_clean_str(fingerprint.get("generic")))
    if explicit:
        return explicit

    normalized = _upper(_clean_str(fingerprint.get("normalized_name")))
    if not normalized:
        return None

    tokens = normalized.split()
    if not tokens:
        return None

    strength_pattern = re.compile(r"^\d")
    generic_tokens = []
    for token in tokens:
        if strength_pattern.match(token):
            break
        if token in {"MG", "ML", "GM", "MCG", "%"}:
            break
        generic_tokens.append(token)

    if generic_tokens:
        return " ".join(generic_tokens)

    return tokens[0]


def build_product_line(fingerprint: Dict[str, Any]) -> Optional[str]:
    brand = _clean_str(fingerprint.get("brand"))
    normalized = _clean_str(fingerprint.get("normalized_name"))
    if brand and normalized:
        return f"{brand.upper()} {normalized.upper()}"
    return _upper(brand or normalized)


def build_embedding_text(fingerprint: Dict[str, Any]) -> str:
    """
    Turn a structured fingerprint into embedding input text.

    Example:
      Product: DOLO PARACETAMOL 650 MG TABLET
      Brand: DOLO
      Generic: PARACETAMOL
      ...
    """
    normalized = normalize_fingerprint(fingerprint)
    lines = []

    product_line = build_product_line(normalized)
    if product_line:
        lines.append(f"Product: {product_line}")

    field_labels = {
        "brand": "Brand",
        "generic": "Generic",
        "strength": "Strength",
        "dosage_form": "Dosage Form",
        "pack_size": "Pack Size",
        "manufacturer": "Manufacturer",
        "uom": "Unit",
        "category": "Category",
        "product_subcategory": "Subcategory",
        "variant_flavour": "Variant / Flavour",
        "product_type": "Product Type",
        "pack_type": "Pack Type",
        "net_quantity": "Net Quantity",
        "net_weight": "Net Weight",
        "pack_configuration": "Pack Configuration",
        "vendor_code": "Vendor Code",
    }
    for key, label in field_labels.items():
        value = normalized.get(key)
        if value:
            lines.append(f"{label}: {value}")

    if not lines:
        fallback = _clean_str(
            fingerprint.get("normalized_name")
            or fingerprint.get("description")
            or fingerprint.get("item_name")
        )
        return fallback or ""

    return "\n".join(lines)


def parse_fingerprint_with_openai(
    description: str,
    *,
    uom: Optional[str] = None,
    item_no: Optional[str] = None,
) -> Dict[str, Any]:
    prompt = f"""{PRODUCT_FINGERPRINT_CORE_INSTRUCTIONS}

{PRODUCT_FINGERPRINT_JSON_FIELD_GUIDE}

Line item number: {item_no or "N/A"}
Unit of measure hint (use only if present in source or hint): {uom or "N/A"}

Description:
---
{description.strip()}
---

Return ONLY valid JSON matching this schema:
{json.dumps(ItemFingerprintExtract.model_json_schema())}
"""
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=settings.openai_model or "gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You create product fingerprints from supplied source data only. "
                    "Respond with JSON only. Do not invent or assume missing product attributes."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    text = (response.choices[0].message.content if response.choices else None) or ""
    if not text.strip():
        raise ValueError("OpenAI returned empty fingerprint JSON")

    payload = ItemFingerprintExtract.model_validate_json(text)
    return normalize_fingerprint(payload.model_dump(), uom_fallback=uom)


def parse_rate_contract_fingerprint_with_openai(
    description: str,
    *,
    uom: Optional[str] = None,
    item_no: Optional[str] = None,
    pack_size_hint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Rate-contract fingerprint extraction.

    Uses supplied OCR / line text and hints only; omits fields not evidenced
    in source data. Backend may apply deterministic fallbacks after parsing.
    """
    text = _clean_str(description)
    if not text:
        return normalize_fingerprint({}, uom_fallback=uom)

    prompt = f"""{PRODUCT_FINGERPRINT_CORE_INSTRUCTIONS}

{PRODUCT_FINGERPRINT_JSON_FIELD_GUIDE}

Use the rate-contract line below as the only source for product attributes.
Hints below are supplementary supplied data — use them only when consistent with the line text.

Line item number: {item_no or "N/A"}
Unit of measure hint: {uom or "N/A"}
Pack size hint: {pack_size_hint or "N/A"}

Rate contract line:
---
{text}
---

Return ONLY valid JSON matching this schema:
{json.dumps(ItemFingerprintExtract.model_json_schema())}
"""
    client = _get_openai_client()
    response = client.chat.completions.create(
        model=settings.openai_model or "gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You create rate-contract product fingerprints from supplied source data only. "
                    "Respond with JSON only. Do not invent brand, manufacturer, category, "
                    "pack size, or other attributes not evidenced in the source text or hints."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    content = (response.choices[0].message.content if response.choices else None) or ""
    if not content.strip():
        raise ValueError("OpenAI returned empty rate-contract fingerprint JSON")

    payload = ItemFingerprintExtract.model_validate_json(content)
    return normalize_fingerprint(
        payload.model_dump(),
        uom_fallback=uom,
    )


def build_invoice_line_fingerprint_with_openai(
    description: str,
    *,
    uom: Optional[str] = None,
    item_no: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build a fingerprint for an unmatched invoice line.

    Focuses on category, manufacturer, brand, uom, and pack_size.
    """
    text = _clean_str(description)
    if not text:
        return normalize_fingerprint({}, uom_fallback=uom)

    prompt = f"""{PRODUCT_FINGERPRINT_CORE_INSTRUCTIONS}

{PRODUCT_FINGERPRINT_JSON_FIELD_GUIDE}

Line item number: {item_no or "N/A"}
Unit of measure hint (use only if present in source or hint): {uom or "N/A"}

Invoice line description:
---
{text}
---

Return ONLY valid JSON matching this schema:
{json.dumps(ItemFingerprintExtract.model_json_schema())}
"""
    try:
        client = _get_openai_client()
        response = client.chat.completions.create(
            model=settings.openai_model or "gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You create invoice line product fingerprints from supplied source data only. "
                        "Respond with JSON only. Do not invent or assume missing product attributes."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = (
            response.choices[0].message.content if response.choices else None
        ) or ""
        if not content.strip():
            raise ValueError("OpenAI returned empty fingerprint JSON")
        payload = ItemFingerprintExtract.model_validate_json(content)
        fingerprint = normalize_fingerprint(
            payload.model_dump(), uom_fallback=uom
        )
        if not fingerprint.get("normalized_name"):
            fingerprint["normalized_name"] = text.upper()
        return fingerprint
    except Exception as exc:
        logger.warning(
            "OpenAI invoice fingerprint failed for %r: %s — using fallback",
            text[:120],
            exc,
        )
        return normalize_fingerprint(
            {"normalized_name": text.upper(), "uom": uom},
            uom_fallback=uom,
        )


# Backward-compatible alias
parse_fingerprint_with_gemini = parse_fingerprint_with_openai


def build_fingerprint_from_line_data(
    *,
    description: Optional[str] = None,
    uom: Optional[str] = None,
    item_no: Optional[str] = None,
    vendor_item: Optional[VendorItem] = None,
    use_gemini: bool = True,
) -> Dict[str, Any]:
    uom_value = _clean_str(uom) or (
        _clean_str(vendor_item.uom) if vendor_item else None
    )

    if vendor_item and vendor_item.product_fingerprint:
        if isinstance(vendor_item.product_fingerprint, dict):
            return normalize_fingerprint(
                vendor_item.product_fingerprint,
                uom_fallback=uom_value,
            )

    text = _clean_str(description) or (
        _clean_str(vendor_item.item_name) if vendor_item else None
    )
    if not text:
        return normalize_fingerprint({"uom": uom_value}, uom_fallback=uom_value)

    if use_gemini:
        try:
            return parse_fingerprint_with_openai(
                text,
                uom=uom_value,
                item_no=item_no,
            )
        except Exception as exc:
            logger.warning("OpenAI fingerprint parse failed for line: %s", exc)

    return normalize_fingerprint(
        {"normalized_name": text.upper(), "uom": uom_value},
        uom_fallback=uom_value,
    )


def build_fingerprint_from_po_line(
    line: PurchaseOrderLine,
    vendor_item: Optional[VendorItem] = None,
    *,
    use_gemini: bool = True,
) -> Dict[str, Any]:
    return build_fingerprint_from_line_data(
        description=_clean_str(line.description),
        uom=_clean_str(line.uom),
        item_no=_clean_str(line.item_no),
        vendor_item=vendor_item,
        use_gemini=use_gemini,
    )


def enrich_fingerprint_from_vendor_item(
    fingerprint: Optional[Dict[str, Any]],
    vendor_item: VendorItem,
    *,
    pack_size_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Merge OCR/LLM fingerprint with vendor_item identity fields.

    Ensures rate-contract mandatory keys (category, manufacturer, brand, uom,
    pack_size, vendor_code) are populated from the vendor item when missing.
    """
    raw: Dict[str, Any] = dict(fingerprint) if isinstance(fingerprint, dict) else {}

    fallbacks = {
        "brand": getattr(vendor_item, "brand", None),
        "manufacturer": getattr(vendor_item, "manufacturer", None),
        "category": getattr(vendor_item, "category", None),
        "uom": getattr(vendor_item, "uom", None),
        "vendor_code": getattr(vendor_item, "vendor_code", None),
        "normalized_name": (
            getattr(vendor_item, "product_name", None)
            or getattr(vendor_item, "item_name", None)
        ),
    }
    for key, value in fallbacks.items():
        if not _clean_str(raw.get(key)) and _clean_str(value):
            raw[key] = value

    if not _clean_str(raw.get("pack_size")):
        pack = _clean_str(pack_size_override)
        if not pack and getattr(vendor_item, "pack_size", None) is not None:
            pack = _clean_str(vendor_item.pack_size)
        if not pack:
            pack = _clean_str(getattr(vendor_item, "consumer_unit_size", None)) or _clean_str(
                getattr(vendor_item, "consumer_unit", None)
            )
        if pack:
            raw["pack_size"] = pack

    return normalize_fingerprint(raw, uom_fallback=_clean_str(vendor_item.uom))


def missing_rate_contract_fingerprint_fields(
    fingerprint: Dict[str, Any],
) -> List[str]:
    """Return mandatory rate-contract fingerprint keys that are empty."""
    missing: List[str] = []
    for key in RATE_CONTRACT_MANDATORY_FINGERPRINT_FIELDS:
        if not _clean_str(fingerprint.get(key)):
            missing.append(key)
    return missing
