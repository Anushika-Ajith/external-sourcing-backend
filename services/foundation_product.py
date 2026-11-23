"""Clients for ERP item and Foundation product APIs used during rate-contract insertion."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException, status

from config import settings

logger = logging.getLogger(__name__)

_ITEM_TIMEOUT_S = 20
_DEFAULT_HSN_CODE = "3004"


def _erp_base() -> str:
    base = (settings.internal_api_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_API_URL is not configured; cannot create ERP item",
        )
    return base


def _foundation_base() -> str:
    base = (settings.nestjs_api_url or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NESTJS_API_URL is not configured; cannot create Foundation product",
        )
    return base


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _parse_json_body(resp: requests.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return (resp.text or "")[:500]


def _extract_error_detail(body: Any, *, fallback: str) -> str:
    if isinstance(body, dict):
        msg = body.get("message") or body.get("detail") or body.get("error")
        if isinstance(msg, list):
            return "; ".join(str(m) for m in msg)
        if msg:
            return str(msg)
    if isinstance(body, str) and body.strip():
        return body.strip()
    return fallback


def _to_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value).strip().replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def _get_next_item_id(*, tenant_id: int, company_id: int) -> int:
    base = _erp_base()
    url = f"{base}/items"
    page = 1
    limit = 500
    max_id = 0
    while True:
        try:
            resp = requests.get(
                url,
                params={
                    "page": page,
                    "limit": limit,
                    "tenantId": tenant_id,
                    "companyId": company_id,
                },
                headers={"Accept": "application/json"},
                timeout=_ITEM_TIMEOUT_S,
            )
        except requests.Timeout as exc:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="ERP item lookup timed out",
            ) from exc
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"ERP item lookup failed: {exc}",
            ) from exc

        body = _parse_json_body(resp)
        if resp.status_code != 200:
            detail = _extract_error_detail(body, fallback=resp.text or "ERP item lookup failed")
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)

        items = []
        if isinstance(body, list):
            items = body
        elif isinstance(body, dict):
            raw_items = body.get("items")
            if isinstance(raw_items, list):
                items = raw_items

        if not items:
            break

        for item in items:
            if isinstance(item, dict):
                try:
                    max_id = max(max_id, int(item.get("id") or 0))
                except (TypeError, ValueError):
                    continue

        if len(items) < limit:
            break
        page += 1

    return max_id + 1


def create_item_in_erp(
    *,
    tenant_id: int,
    company_id: int,
    name: str,
    description: Optional[str] = None,
    category: Optional[str] = None,
    hsn_code: Optional[str] = None,
    hsn_description: Optional[str] = None,
    brand: Optional[str] = None,
    product_name: Optional[str] = None,
    consumer_unit: Optional[str] = None,
    consumer_unit_size: Optional[str] = None,
    purchase_unit: Optional[str] = None,
    pack_size: Any = None,
    conversion_factor: Any = None,
    status_value: Optional[str] = None,
    is_active: bool = True,
    reorder_level: int = 20,
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create an ERP item via:
    POST {INTERNAL_API_URL}/items
    """
    base = _erp_base()
    url = f"{base}/items"
    next_id = _get_next_item_id(tenant_id=tenant_id, company_id=company_id)
    payload: Dict[str, Any] = {
        "id": next_id,
        "tenantId": int(tenant_id),
        "companyId": int(company_id),
        "name": _clean(name),
        "category": _clean(category) or "GENERAL",
        "classification": _clean(category) or "GENERAL",
        "chemical": _clean(description),
        "strength": _clean(consumer_unit_size) or _clean(description),
        "packing": int(_to_number(conversion_factor) or 1),
        "box": _to_number(pack_size) or 1,
        "brand": _clean(brand),
        "productName": _clean(product_name) or _clean(name),
        "uom": _clean(consumer_unit) or _clean(purchase_unit),
        "manufacturer": _clean(brand),
        "marketedBy": _clean(brand),
        "price": 0,
        "consumerUnit": _clean(consumer_unit) or _clean(purchase_unit),
        "consumerUnitSize": _clean(consumer_unit_size),
        "purchaseUnit": _clean(purchase_unit) or _clean(consumer_unit),
        "packSize": _to_number(pack_size),
        "conversionFactor": _to_number(conversion_factor) or 1,
        "status": _clean(status_value) or "Active",
        "itemBreakable": False,
        "allowConsumption": True,
        "isActive": bool(is_active),
        "itemType": "Medicine",
    }
    if created_by is not None:
        payload["createdBy"] = int(created_by)

    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=_ITEM_TIMEOUT_S,
        )
    except requests.Timeout as exc:
        logger.error("[items] ERP /items timeout: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="ERP item creation timed out",
        ) from exc
    except requests.RequestException as exc:
        logger.error("[items] ERP /items request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"ERP item creation failed: {exc}",
        ) from exc

    body = _parse_json_body(resp)
    if resp.status_code in (200, 201) and isinstance(body, dict):
        logger.info(
            "[items] ERP item created id=%s itemCode=%s name=%s",
            body.get("id"),
            body.get("itemCode"),
            body.get("name") or name,
        )
        return body

    detail = _extract_error_detail(body, fallback=resp.text or "ERP item creation failed")
    logger.error(
        "[items] ERP /items status=%s detail=%s payload=%s",
        resp.status_code,
        detail,
        payload,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=detail,
    )


def create_product_in_foundation(
    *,
    tenant_id: int = 1,
    company_id: int = 3,
    department_id: int = 1,
    name: str,
    description: Optional[str] = None,
    category: Optional[str] = None,
    hsn_code: Optional[str] = None,
    hsn_description: Optional[str] = None,
    brand: Optional[str] = None,
    product_name: Optional[str] = None,
    consumer_unit: Optional[str] = None,
    consumer_unit_size: Optional[str] = None,
    purchase_unit: Optional[str] = None,
    pack_size: Any = None,
    conversion_factor: Any = None,
    status_value: Optional[str] = None,
    is_active: bool = True,
    reorder_level: int = 20,
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Create a Foundation product via:
    POST {NESTJS_API_URL}/products

    ``productCode`` is allocated by Foundation next-numbering (ITEM_MASTER)
    using tenant/company/department from this request.
    """
    base = _foundation_base()
    url = f"{base}/products"
    payload: Dict[str, Any] = {
        "tenantId": int(tenant_id),
        "companyId": int(company_id),
        "departmentId": int(department_id),
        "name": _clean(name),
        "description": _clean(description),
        "category": _clean(category) or "GENERAL",
        "hsnCode": _clean(hsn_code) or _DEFAULT_HSN_CODE,
        "hsnDescription": _clean(hsn_description),
        "brand": _clean(brand),
        "productName": _clean(product_name) or _clean(name),
        "consumerUnit": _clean(consumer_unit) or _clean(purchase_unit),
        "consumerUnitSize": _clean(consumer_unit_size),
        "purchaseUnit": _clean(purchase_unit) or _clean(consumer_unit),
        "packSize": _to_number(pack_size),
        "conversionFactor": _to_number(conversion_factor) or 1,
        "status": _clean(status_value) or "ACTIVE",
        "isActive": bool(is_active),
        "reorderLevel": int(reorder_level),
    }
    if created_by is not None:
        payload["createdBy"] = int(created_by)

    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=_ITEM_TIMEOUT_S,
        )
    except requests.Timeout as exc:
        logger.error("[products] Foundation /products timeout: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Foundation product creation timed out",
        ) from exc
    except requests.RequestException as exc:
        logger.error("[products] Foundation /products request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Foundation product creation failed: {exc}",
        ) from exc

    body = _parse_json_body(resp)
    if resp.status_code in (200, 201) and isinstance(body, dict):
        logger.info(
            "[products] Foundation product created productId=%s productCode=%s name=%s",
            body.get("productId"),
            body.get("productCode"),
            body.get("name") or name,
        )
        return body

    detail = _extract_error_detail(
        body,
        fallback=resp.text or "Foundation product creation failed",
    )
    logger.error(
        "[products] Foundation /products status=%s detail=%s payload=%s",
        resp.status_code,
        detail,
        payload,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=detail,
    )


def list_foundation_products(*, timeout_s: int = _ITEM_TIMEOUT_S) -> List[Dict[str, Any]]:
    """
    Fetch all products from Foundation ``GET /products``.

    Returns an empty list when Foundation is unavailable so callers can fall back
    to the local catalog.
    """
    try:
        base = _foundation_base()
    except HTTPException as exc:
        logger.warning("Foundation products list skipped: %s", exc.detail)
        return []

    url = f"{base}/products"
    try:
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=timeout_s)
    except requests.RequestException as exc:
        logger.warning("Foundation /products list request failed: %s", exc)
        return []

    body = _parse_json_body(resp)
    if resp.status_code != 200:
        detail = _extract_error_detail(body, fallback=resp.text or "Foundation product list failed")
        logger.warning("Foundation /products list status=%s detail=%s", resp.status_code, detail)
        return []

    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    logger.warning("Foundation /products list returned unexpected payload type: %s", type(body).__name__)
    return []
