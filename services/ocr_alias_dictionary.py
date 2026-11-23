"""Client for Foundation OCR alias dictionary (pack-size + product aliases)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Set

import requests

from config import settings

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10

# Alias types treated as product-name synonyms for invoice matching.
PRODUCT_ALIAS_TYPES = (
    "PRODUCT_NAME",
    "ITEM_NAME",
    "PRODUCT",
    "ITEM",
)


def _nestjs_base() -> Optional[str]:
    base = (settings.nestjs_api_url or "").strip().rstrip("/")
    return base or None


def resolve_pack_size_canonical(
    pack_size: Any,
    *,
    language_code: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve a pack-size alias to its canonical value via Foundation:

      GET {NESTJS_API_URL}/ocr-alias-dictionary/pack-size/canonical?packSize=GM

    Returns the canonical value when found; otherwise returns the cleaned input
    (so callers can still store/match something useful if the API is down or
    the alias is unknown).
    """
    raw = None if pack_size is None else str(pack_size).strip()
    if not raw:
        return None

    base = _nestjs_base()
    if not base:
        logger.warning(
            "NESTJS_API_URL not configured; using raw pack_size=%s",
            raw,
        )
        return raw

    params = {"packSize": raw}
    if language_code and language_code.strip():
        params["languageCode"] = language_code.strip()

    url = f"{base}/ocr-alias-dictionary/pack-size/canonical"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning(
            "Pack-size canonical lookup failed for %r: %s — using raw value",
            raw,
            exc,
        )
        return raw

    if resp.status_code == 404:
        logger.info("No pack-size alias for %r — using raw value", raw)
        return raw

    if resp.status_code >= 400:
        logger.warning(
            "Pack-size canonical lookup HTTP %s for %r: %s — using raw value",
            resp.status_code,
            raw,
            (resp.text or "")[:200],
        )
        return raw

    try:
        body = resp.json()
    except ValueError:
        logger.warning("Pack-size canonical response not JSON for %r", raw)
        return raw

    if isinstance(body, dict):
        canonical = body.get("canonicalValue") or body.get("canonical_value")
        if canonical is not None and str(canonical).strip():
            return str(canonical).strip()

    return raw


def list_ocr_alias_entries(
    *,
    alias_type: Optional[str] = None,
    language_code: Optional[str] = None,
    active: Optional[bool] = True,
    item_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    List OCR alias dictionary rows from Foundation:

      GET {NESTJS_API_URL}/ocr-alias-dictionary?aliasType=PRODUCT_NAME&active=true
    """
    base = _nestjs_base()
    if not base:
        logger.warning("NESTJS_API_URL not configured; cannot list OCR aliases")
        return []

    params: Dict[str, Any] = {}
    if alias_type:
        params["aliasType"] = alias_type
    if language_code and language_code.strip():
        params["languageCode"] = language_code.strip()
    if active is not None:
        params["active"] = "true" if active else "false"
    if item_id is not None:
        params["itemId"] = int(item_id)

    url = f"{base}/ocr-alias-dictionary"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning("OCR alias list failed: %s", exc)
        return []

    if resp.status_code >= 400:
        logger.warning(
            "OCR alias list HTTP %s: %s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        return []

    try:
        body = resp.json()
    except ValueError:
        logger.warning("OCR alias list response not JSON")
        return []

    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    return []


def list_product_aliases_for_item_ids(
    item_ids: Sequence[int],
    *,
    alias_types: Sequence[str] = PRODUCT_ALIAS_TYPES,
) -> List[Dict[str, Any]]:
    """
    Load product-name aliases whose itemId is in the given PO product set.

    Fetches each configured alias type once, then filters to ``item_ids``.
    """
    wanted: Set[int] = {int(i) for i in item_ids if i is not None}
    if not wanted:
        return []

    collected: List[Dict[str, Any]] = []
    seen_keys: Set[tuple] = set()

    for alias_type in alias_types:
        rows = list_ocr_alias_entries(alias_type=alias_type, active=True)
        for row in rows:
            raw_item_id = row.get("itemId", row.get("item_id"))
            try:
                item_id = int(raw_item_id) if raw_item_id is not None else None
            except (TypeError, ValueError):
                item_id = None
            if item_id is None or item_id not in wanted:
                continue
            key = (
                str(row.get("aliasType") or row.get("alias_type") or "").upper(),
                str(row.get("aliasValue") or row.get("alias_value") or "").upper(),
                item_id,
                str(row.get("languageCode") or row.get("language_code") or ""),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            collected.append(row)

    return collected


def check_alias_value_exists(
    alias_value: str,
    *,
    alias_type: Optional[str] = None,
    language_code: Optional[str] = None,
    active: Optional[bool] = True,
) -> Optional[Dict[str, Any]]:
    """
    Check whether an alias_value already exists in Foundation:

      GET {NESTJS_API_URL}/ocr-alias-dictionary/exists?aliasValue=...

    Returns the API body (``exists``, ``count``, ``entries``, …) or None if
    the check could not be performed.
    """
    alias = (alias_value or "").strip()
    if not alias:
        return None

    base = _nestjs_base()
    if not base:
        logger.warning(
            "NESTJS_API_URL not configured; cannot check alias existence for %r",
            alias,
        )
        return None

    params: Dict[str, Any] = {"aliasValue": alias}
    if alias_type and alias_type.strip():
        params["aliasType"] = alias_type.strip()
    if language_code and language_code.strip():
        params["languageCode"] = language_code.strip()
    if active is not None:
        params["active"] = "true" if active else "false"

    url = f"{base}/ocr-alias-dictionary/exists"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning(
            "Alias exists check failed for %r: %s — will attempt create",
            alias,
            exc,
        )
        return None

    if resp.status_code >= 400:
        logger.warning(
            "Alias exists check HTTP %s for %r: %s — will attempt create",
            resp.status_code,
            alias,
            (resp.text or "")[:200],
        )
        return None

    try:
        body = resp.json()
    except ValueError:
        logger.warning("Alias exists check response not JSON for %r", alias)
        return None

    return body if isinstance(body, dict) else None


def create_product_name_alias(
    *,
    alias_value: str,
    canonical_value: str,
    item_id: Optional[int] = None,
    language_code: Optional[str] = None,
    remarks: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Create a PRODUCT_NAME alias in Foundation ocr_alias_dictionary.

    Used when a user manually confirms/corrects an invoice line product mapping.
    """
    alias = (alias_value or "").strip()
    canonical = (canonical_value or "").strip()
    if not alias or not canonical:
        return None

    base = _nestjs_base()
    if not base:
        logger.warning("NESTJS_API_URL not configured; cannot create product alias")
        return None

    exists_result = check_alias_value_exists(
        alias,
        alias_type="PRODUCT_NAME",
        language_code=language_code,
        active=True,
    )
    if exists_result and exists_result.get("exists"):
        logger.info(
            "Skip create product alias — already exists for %r (count=%s)",
            alias,
            exists_result.get("count"),
        )
        return {
            "skipped": True,
            "reason": "already_exists",
            "aliasValue": alias,
            "canonicalValue": canonical,
            "itemId": item_id,
            **exists_result,
        }

    body: Dict[str, Any] = {
        "aliasType": "PRODUCT_NAME",
        "aliasValue": alias,
        "canonicalValue": canonical,
        "active": True,
    }
    if item_id is not None:
        body["itemId"] = int(item_id)
    if language_code and language_code.strip():
        body["languageCode"] = language_code.strip()
    if remarks and remarks.strip():
        body["remarks"] = remarks.strip()[:500]

    url = f"{base}/ocr-alias-dictionary"
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning("Create product alias failed for %r: %s", alias, exc)
        return None

    if resp.status_code >= 400:
        logger.warning(
            "Create product alias HTTP %s for %r: %s",
            resp.status_code,
            alias,
            (resp.text or "")[:200],
        )
        return None

    try:
        return resp.json()
    except ValueError:
        return {"aliasValue": alias, "canonicalValue": canonical, "itemId": item_id}
