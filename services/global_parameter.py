"""Client for Foundation ``global_parameter`` (UOM master codes)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from config import settings

logger = logging.getLogger(__name__)

_TIMEOUT_S = 10

# CHAR(3) type for unit-of-measure rows in public.global_parameter.
UOM_PARAMETER_TYPE = "UOM"


@dataclass(frozen=True)
class UomResolveResult:
    """Outcome of matching an OCR UOM against Foundation global_parameter."""

    ocr_value: str
    canonical_code: Optional[str]
    description: Optional[str]
    matched: bool
    parameter_id: Optional[int] = None

    @property
    def needs_confirm(self) -> bool:
        """True when OCR has a UOM that is not yet in global_parameter."""
        return bool(self.ocr_value) and not self.matched


def _nestjs_base() -> Optional[str]:
    base = (settings.nestjs_api_url or "").strip().rstrip("/")
    return base or None


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _clip_code(value: Any, max_len: int = 3) -> Optional[str]:
    text = _clean(value)
    if not text:
        return None
    return text[:max_len].upper()


def _norm_key(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    return " ".join(text.lower().split())


def list_global_parameters(*, param_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List Foundation global_parameter rows:

      GET {NESTJS_API_URL}/global-parameter?type=UOM
    """
    base = _nestjs_base()
    if not base:
        logger.warning("NESTJS_API_URL not configured; cannot list global_parameter")
        return []

    params: Dict[str, Any] = {}
    if param_type and str(param_type).strip():
        params["type"] = str(param_type).strip()[:3]

    url = f"{base}/global-parameter"
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning("global_parameter list failed: %s", exc)
        return []

    if resp.status_code >= 400:
        logger.warning(
            "global_parameter list HTTP %s: %s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        return []

    try:
        body = resp.json()
    except ValueError:
        logger.warning("global_parameter list response not JSON")
        return []

    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    return []


def list_uom_parameters() -> List[Dict[str, Any]]:
    return list_global_parameters(param_type=UOM_PARAMETER_TYPE)


def resolve_uom_from_global_parameter(uom: Any) -> UomResolveResult:
    """
    Match an OCR UOM string to Foundation ``global_parameter`` (type=UOM).

    Match order:
      1. Exact code (CHAR(3), case-insensitive)
      2. Exact description (case-insensitive)
      3. Description contains OCR text / OCR contains description

    When unmatched, ``canonical_code`` is a best-effort 3-char clip of the OCR
    value (for suggested create) and ``matched`` is False.
    """
    raw = _clean(uom)
    if not raw:
        return UomResolveResult(
            ocr_value="",
            canonical_code=None,
            description=None,
            matched=False,
        )

    suggested = _clip_code(raw)
    rows = list_uom_parameters()
    if not rows:
        return UomResolveResult(
            ocr_value=raw,
            canonical_code=suggested,
            description=raw,
            matched=False,
        )

    raw_key = _norm_key(raw)
    code_key = _norm_key(suggested)

    # Pass 1: exact code
    for row in rows:
        code = _clip_code(row.get("code"))
        if code and _norm_key(code) == code_key:
            return UomResolveResult(
                ocr_value=raw,
                canonical_code=code,
                description=_clean(row.get("description")) or code,
                matched=True,
                parameter_id=_as_int(row.get("id")),
            )

    # Pass 2: exact description
    for row in rows:
        desc = _clean(row.get("description"))
        if desc and _norm_key(desc) == raw_key:
            code = _clip_code(row.get("code")) or suggested
            return UomResolveResult(
                ocr_value=raw,
                canonical_code=code,
                description=desc,
                matched=True,
                parameter_id=_as_int(row.get("id")),
            )

    # Pass 3: fuzzy containment (short masters only)
    for row in rows:
        desc = _clean(row.get("description"))
        code = _clip_code(row.get("code"))
        desc_key = _norm_key(desc)
        if not desc_key and not code:
            continue
        if desc_key and (desc_key in raw_key or raw_key in desc_key):
            return UomResolveResult(
                ocr_value=raw,
                canonical_code=code or suggested,
                description=desc or code,
                matched=True,
                parameter_id=_as_int(row.get("id")),
            )
        if code and (code_key == _norm_key(code) or code_key.startswith(_norm_key(code))):
            return UomResolveResult(
                ocr_value=raw,
                canonical_code=code,
                description=desc or code,
                matched=True,
                parameter_id=_as_int(row.get("id")),
            )

    return UomResolveResult(
        ocr_value=raw,
        canonical_code=suggested,
        description=raw,
        matched=False,
    )


def create_uom_global_parameter(
    *,
    code: str,
    description: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Create a UOM row in Foundation global_parameter.

      POST {NESTJS_API_URL}/global-parameter
      { "type": "UOM", "code": "KG", "description": "Kilogram" }

    Skips create when the code already exists (returns skipped payload).
    """
    clipped = _clip_code(code)
    if not clipped:
        return None

    desc = _clean(description) or clipped

    existing = resolve_uom_from_global_parameter(clipped)
    if existing.matched and _norm_key(existing.canonical_code) == _norm_key(clipped):
        logger.info(
            "Skip create UOM global_parameter — code %r already exists id=%s",
            clipped,
            existing.parameter_id,
        )
        return {
            "skipped": True,
            "reason": "already_exists",
            "type": UOM_PARAMETER_TYPE,
            "code": clipped,
            "description": existing.description or desc,
            "id": existing.parameter_id,
        }

    # Also skip when OCR description already maps to another code.
    if description:
        by_desc = resolve_uom_from_global_parameter(description)
        if by_desc.matched:
            return {
                "skipped": True,
                "reason": "already_exists",
                "type": UOM_PARAMETER_TYPE,
                "code": by_desc.canonical_code,
                "description": by_desc.description,
                "id": by_desc.parameter_id,
            }

    base = _nestjs_base()
    if not base:
        logger.warning("NESTJS_API_URL not configured; cannot create UOM parameter")
        return None

    body = {
        "type": UOM_PARAMETER_TYPE,
        "code": clipped,
        "description": desc[:255],
    }
    url = f"{base}/global-parameter"
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        logger.warning("Create UOM global_parameter failed for %r: %s", clipped, exc)
        return None

    if resp.status_code >= 400:
        logger.warning(
            "Create UOM global_parameter HTTP %s for %r: %s",
            resp.status_code,
            clipped,
            (resp.text or "")[:200],
        )
        return None

    try:
        return resp.json()
    except ValueError:
        return {"type": UOM_PARAMETER_TYPE, "code": clipped, "description": desc}


def ensure_uom_global_parameter(
    ocr_uom: Any,
    *,
    confirmed_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    After user confirms a rate-contract line, ensure OCR UOM exists in
    global_parameter (same role as PRODUCT_NAME alias create).
    """
    raw = _clean(ocr_uom)
    if not raw:
        return None

    resolved = resolve_uom_from_global_parameter(raw)
    if resolved.matched:
        return {
            "skipped": True,
            "reason": "already_exists",
            "type": UOM_PARAMETER_TYPE,
            "code": resolved.canonical_code,
            "description": resolved.description,
            "id": resolved.parameter_id,
        }

    code = _clip_code(confirmed_code) or resolved.canonical_code or _clip_code(raw)
    return create_uom_global_parameter(code=code or raw[:3], description=raw)


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
