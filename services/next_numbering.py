"""Client for NestJS next-numbering service (RFQ document numbers)."""

from typing import Any, Optional

import requests

from config import settings

DOC_TYPE_RFQ = "RFQ"
DOC_TYPE_VENDOR_REG = "VENDOR_REG"
DOC_TYPE_PO = "PURCHASE_ORDER"
DOC_TYPE_VENDOR_PRODUCT = "VENDOR_PRODUCT"


def _parse_next_number(payload: Any) -> str:
    if isinstance(payload, str):
        s = payload.strip()
        if s:
            return s
    if isinstance(payload, (int, float)):
        return str(int(payload))
    if isinstance(payload, dict):
        for key in (
            "nextNumber",
            "next_number",
            "number",
            "next",
            "documentNumber",
            "document_number",
            "value",
            "data",
        ):
            if key not in payload or payload[key] is None:
                continue
            v = payload[key]
            if isinstance(v, dict):
                inner = v.get("number") or v.get("nextNumber") or v.get("value")
                if inner is not None:
                    return str(inner).strip()
            else:
                return str(v).strip()
    raise ValueError(f"Could not parse next RFQ number from numbering service response: {payload!r}")


def get_next_rfq_number_from_nest(
    tenant_id: int,
    company_id: int,
    department_id: Optional[int],
) -> str:
    """
    POST {NESTJS_API_URL}/next-numbering/{tenant}/{company}/{dept}/RFQ/next
    """
    base = (settings.nestjs_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("NESTJS_API_URL is not configured")

    dept = 0 if department_id is None else int(department_id)
    url = f"{base}/next-numbering/{tenant_id}/{company_id}/{dept}/{DOC_TYPE_RFQ}/next"

    try:
        resp = requests.post(url, timeout=60)
        resp.raise_for_status()
    except requests.HTTPError as e:
        detail = ""
        if e.response is not None:
            try:
                body = e.response.json()
                if isinstance(body, dict):
                    detail = str(body.get("message") or body.get("detail") or body)
                else:
                    detail = str(body)
            except ValueError:
                detail = (e.response.text or "").strip()
        msg = f"Numbering service request failed: {e}"
        if detail:
            msg = f"{msg} — {detail}"
        raise RuntimeError(msg) from e
    except requests.RequestException as e:
        raise RuntimeError(f"Numbering service request failed: {e}") from e

    text = (resp.text or "").strip()
    if not text:
        raise ValueError("Empty response from numbering service")

    try:
        payload = resp.json()
    except ValueError:
        return text.strip().strip('"')

    return _parse_next_number(payload)


def get_next_document_number_from_nest(
    tenant_id: int,
    company_id: int,
    department_id: Optional[int],
    document_type: str,
) -> str:
    """
    POST {NESTJS_API_URL}/next-numbering/{tenant}/{company}/{dept}/{document_type}/next
    """
    base = (settings.nestjs_api_url or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("NESTJS_API_URL is not configured")

    dept = 0 if department_id is None else int(department_id)
    doc_type = str(document_type).strip()
    url = f"{base}/next-numbering/{tenant_id}/{company_id}/{dept}/{doc_type}/next"

    try:
        resp = requests.post(url, timeout=60)
        resp.raise_for_status()
    except requests.HTTPError as e:
        detail = ""
        if e.response is not None:
            try:
                body = e.response.json()
                if isinstance(body, dict):
                    detail = str(body.get("message") or body.get("detail") or body)
                else:
                    detail = str(body)
            except ValueError:
                detail = (e.response.text or "").strip()
        msg = f"Numbering service request failed: {e}"
        if detail:
            msg = f"{msg} — {detail}"
        raise RuntimeError(msg) from e
    except requests.RequestException as e:
        raise RuntimeError(f"Numbering service request failed: {e}") from e

    text = (resp.text or "").strip()
    if not text:
        raise ValueError("Empty response from numbering service")

    try:
        payload = resp.json()
    except ValueError:
        return text.strip().strip('"')

    return _parse_next_number(payload)


def get_next_vendor_registration_number_from_nest(
    tenant_id: int = 1,
    company_id: int = 3,
    department_id: int = 1,
) -> str:
    return get_next_document_number_from_nest(
        tenant_id=tenant_id,
        company_id=company_id,
        department_id=department_id,
        document_type=DOC_TYPE_VENDOR_REG,
    )


def get_next_po_number_from_nest(
    tenant_id: int,
    company_id: int,
    department_id: Optional[int],
) -> str:
    """
    POST {NESTJS_API_URL}/next-numbering/{tenant}/{company}/{dept}/PURCHASE_ORDER/next
    """
    return get_next_document_number_from_nest(
        tenant_id=tenant_id,
        company_id=company_id,
        department_id=department_id,
        document_type=DOC_TYPE_PO,
    )


def get_next_vendor_product_code_from_nest(
    tenant_id: int = 1,
    company_id: int = 1,
    department_id: Optional[int] = 1,
) -> str:
    """
    POST {NESTJS_API_URL}/next-numbering/{tenant}/{company}/{dept}/VENDOR_PRODUCT/next
    """
    return get_next_document_number_from_nest(
        tenant_id=tenant_id,
        company_id=company_id,
        department_id=department_id,
        document_type=DOC_TYPE_VENDOR_PRODUCT,
    )
