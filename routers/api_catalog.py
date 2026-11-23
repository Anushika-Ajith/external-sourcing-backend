"""Aggregate OpenAPI docs from sibling services into a flat API catalog."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urljoin

import requests
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from config import settings
from models import User
from services.auth_utils import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api-catalog", tags=["api-catalog"])

HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "options", "head"}
)


class ApiCatalogEntry(BaseModel):
    service: str
    api_name: str
    api_endpoint: str
    http_method: str
    path: str
    summary: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class ApiCatalogServiceStatus(BaseModel):
    service: str
    base_url: str
    openapi_url: Optional[str] = None
    status: Literal["ok", "error", "skipped"]
    error: Optional[str] = None
    count: int = 0


class ApiCatalogResponse(BaseModel):
    count: int
    entries: List[ApiCatalogEntry]
    services: List[ApiCatalogServiceStatus]


def _trim_slash(url: str) -> str:
    return (url or "").rstrip("/")


def _service_targets() -> List[Dict[str, Any]]:
    """Remote OpenAPI sources (self is handled separately via app.openapi())."""
    targets: List[Dict[str, Any]] = []

    foundation = _trim_slash(settings.nestjs_api_url or "http://localhost:3000")
    targets.append(
        {
            "service": "foundation",
            "base_url": foundation,
            "openapi_url": f"{foundation}/api-json",
        }
    )

    erp = _trim_slash(settings.internal_api_url or "http://localhost:3003")
    targets.append(
        {
            "service": "erp",
            "base_url": erp,
            "openapi_url": f"{erp}/api/docs-json",
        }
    )

    internal = _trim_slash(settings.internal_sourcing_api_url or "http://localhost:8001")
    targets.append(
        {
            "service": "internal-sourcing",
            "base_url": internal,
            "openapi_url": f"{internal}/openapi.json",
        }
    )

    ocr = _trim_slash(settings.ocr_service_url or "http://localhost:8010")
    targets.append(
        {
            "service": "ocr",
            "base_url": ocr,
            "openapi_url": f"{ocr}/openapi.json",
        }
    )

    ai_agent = _trim_slash(settings.ai_agent_api_url or "http://localhost:8000")
    targets.append(
        {
            "service": "ai-agent",
            "base_url": ai_agent,
            "openapi_url": f"{ai_agent}/openapi.json",
        }
    )

    return targets


def _fetch_openapi(url: str, timeout: float = 8.0) -> Dict[str, Any]:
    res = requests.get(url, timeout=timeout)
    res.raise_for_status()
    data = res.json()
    if not isinstance(data, dict):
        raise ValueError("OpenAPI document must be a JSON object")
    return data


def _parse_openapi(
    *,
    service: str,
    base_url: str,
    doc: Dict[str, Any],
) -> List[ApiCatalogEntry]:
    entries: List[ApiCatalogEntry] = []
    paths = doc.get("paths") or {}
    if not isinstance(paths, dict):
        return entries

    servers = doc.get("servers") or []
    server_base = ""
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            server_base = str(first.get("url") or "").rstrip("/")

    # Prefer absolute HTTP(S) server URL; otherwise use configured service base.
    if server_base.startswith("http://") or server_base.startswith("https://"):
        endpoint_base = server_base
    else:
        endpoint_base = _trim_slash(base_url)
        if server_base.startswith("/"):
            endpoint_base = f"{endpoint_base}{server_base}"

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            method_l = str(method).lower()
            if method_l not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                operation = {}

            op_id = operation.get("operationId")
            summary = operation.get("summary")
            tags = operation.get("tags") or []
            if not isinstance(tags, list):
                tags = []

            api_name = (
                str(op_id).strip()
                if op_id
                else (
                    str(summary).strip()
                    if summary
                    else f"{method_l.upper()} {path}"
                )
            )

            # Build absolute endpoint when possible.
            if str(path).startswith("http"):
                api_endpoint = str(path)
            else:
                api_endpoint = urljoin(endpoint_base + "/", str(path).lstrip("/"))
                # urljoin can drop path segments oddly; prefer simple concat for absolute base.
                if endpoint_base.startswith("http"):
                    api_endpoint = f"{endpoint_base}{path if str(path).startswith('/') else '/' + str(path)}"

            entries.append(
                ApiCatalogEntry(
                    service=service,
                    api_name=api_name,
                    api_endpoint=api_endpoint,
                    http_method=method_l.upper(),
                    path=str(path),
                    summary=str(summary) if summary else None,
                    tags=[str(t) for t in tags],
                )
            )

    entries.sort(key=lambda e: (e.service, e.path, e.http_method, e.api_name))
    return entries


def _load_remote(target: Dict[str, Any]) -> tuple[List[ApiCatalogEntry], ApiCatalogServiceStatus]:
    service = target["service"]
    base_url = target["base_url"]
    openapi_url = target["openapi_url"]
    try:
        doc = _fetch_openapi(openapi_url)
        entries = _parse_openapi(service=service, base_url=base_url, doc=doc)
        return entries, ApiCatalogServiceStatus(
            service=service,
            base_url=base_url,
            openapi_url=openapi_url,
            status="ok",
            count=len(entries),
        )
    except Exception as exc:
        logger.warning("api-catalog: failed to load %s (%s): %s", service, openapi_url, exc)
        return [], ApiCatalogServiceStatus(
            service=service,
            base_url=base_url,
            openapi_url=openapi_url,
            status="error",
            error=str(exc),
            count=0,
        )


@router.get("", response_model=ApiCatalogResponse)
async def list_api_catalog(
    request: Request,
    service: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """
    List API endpoints across project services by aggregating OpenAPI documents.

    Returns api_name (operationId/summary), http_method, path, and absolute api_endpoint.
    """
    _ = current_user

    all_entries: List[ApiCatalogEntry] = []
    statuses: List[ApiCatalogServiceStatus] = []

    # Self (external-sourcing) — use live FastAPI schema, no HTTP hop.
    try:
        self_base = str(request.base_url).rstrip("/")
        self_doc = request.app.openapi()
        self_entries = _parse_openapi(
            service="external-sourcing",
            base_url=self_base,
            doc=self_doc,
        )
        all_entries.extend(self_entries)
        statuses.append(
            ApiCatalogServiceStatus(
                service="external-sourcing",
                base_url=self_base,
                openapi_url=f"{self_base}/openapi.json",
                status="ok",
                count=len(self_entries),
            )
        )
    except Exception as exc:
        logger.warning("api-catalog: failed to load self OpenAPI: %s", exc)
        statuses.append(
            ApiCatalogServiceStatus(
                service="external-sourcing",
                base_url=str(request.base_url).rstrip("/"),
                status="error",
                error=str(exc),
                count=0,
            )
        )

    targets = _service_targets()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(targets)))) as pool:
        futures = [pool.submit(_load_remote, t) for t in targets]
        for fut in as_completed(futures):
            entries, status = fut.result()
            all_entries.extend(entries)
            statuses.append(status)

    statuses.sort(key=lambda s: s.service)
    all_entries.sort(key=lambda e: (e.service, e.path, e.http_method, e.api_name))

    if service:
        wanted = service.strip().lower()
        all_entries = [e for e in all_entries if e.service.lower() == wanted]
        statuses = [s for s in statuses if s.service.lower() == wanted]

    return ApiCatalogResponse(
        count=len(all_entries),
        entries=all_entries,
        services=statuses,
    )
