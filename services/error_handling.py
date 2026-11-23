"""Central error handling for the API.

Every error leaves the API in the same JSON shape so the frontend can show it in a toast::

    {
        "detail":  <str | list>,   # kept for backward compatibility with existing clients
        "message": <str>,          # user-friendly text, always safe to show in a toast
        "code":    <str>,          # machine readable error code
        "error_id": <str>,         # only for unexpected server errors (matches the server log)
    }

Raw database / driver / traceback text is never sent to the client.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import (
    DataError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

GENERIC_SERVER_MESSAGE = "Something went wrong on our side. Please try again in a moment."
DB_UNAVAILABLE_MESSAGE = (
    "The database is temporarily unavailable. Please try again in a moment."
)
UPSTREAM_UNAVAILABLE_MESSAGE = (
    "A connected service is not responding right now. Please try again in a moment."
)

_STATUS_MESSAGES = {
    400: "The request could not be processed. Please check your input and try again.",
    401: "Your session has expired. Please log in again.",
    403: "You do not have permission to perform this action.",
    404: "The requested record could not be found.",
    405: "This action is not supported.",
    408: "The request timed out. Please try again.",
    409: "This change conflicts with existing data. Please refresh and try again.",
    413: "The file or data you sent is too large.",
    415: "This file type is not supported.",
    422: "Some of the information provided is invalid. Please review and try again.",
    429: "Too many requests. Please wait a moment and try again.",
    500: GENERIC_SERVER_MESSAGE,
    502: UPSTREAM_UNAVAILABLE_MESSAGE,
    503: "The service is temporarily unavailable. Please try again in a moment.",
    504: "The server took too long to respond. Please try again.",
}

# Text that must never reach an end user (SQL, driver errors, stack traces, file paths).
_TECHNICAL_PATTERNS = re.compile(
    r"(traceback|sqlalchemy|psycopg|asyncpg|\[sql|\[parameters|\bDETAIL:|"
    r"\b(select|insert\s+into|update|delete\s+from)\b.+\b(from|set|values|where)\b|"
    r"violates .* constraint|duplicate key|null value in column|relation \".*\" does not exist|"
    r"column \".*\" (of relation|does not exist)|connection (refused|reset|to server)|"
    r"File \"|\.py\"?, line \d+|object has no attribute|NoneType|keyerror|"
    r"errno \d+|max retries exceeded|httpconnectionpool|\bat 0x[0-9a-f]+)",
    re.IGNORECASE | re.DOTALL,
)

_FIELD_FROM_CONSTRAINT = re.compile(r"Key \(([^)]+)\)=", re.IGNORECASE)
_NULL_COLUMN = re.compile(r'null value in column "([^"]+)"', re.IGNORECASE)


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def looks_technical(text: Any) -> bool:
    return isinstance(text, str) and bool(_TECHNICAL_PATTERNS.search(text))


def status_message(status_code: int) -> str:
    if status_code in _STATUS_MESSAGES:
        return _STATUS_MESSAGES[status_code]
    if status_code >= 500:
        return GENERIC_SERVER_MESSAGE
    if status_code >= 400:
        return _STATUS_MESSAGES[400]
    return GENERIC_SERVER_MESSAGE


def friendly_db_message(exc: BaseException) -> tuple[int, str, str]:
    """Return (http_status, message, code) for a SQLAlchemy / DB-API exception."""
    raw = str(getattr(exc, "orig", None) or exc)
    lowered = raw.lower()

    if isinstance(exc, IntegrityError):
        field_match = _FIELD_FROM_CONSTRAINT.search(raw)
        field = (
            f" ({', '.join(_humanize(p) for p in field_match.group(1).split(','))})"
            if field_match
            else ""
        )
        if "duplicate key" in lowered or "unique constraint" in lowered or "unique" in lowered:
            return 409, f"A record with the same details already exists{field}.", "duplicate_record"
        if "foreign key" in lowered:
            if "still referenced" in lowered or "is referenced" in lowered or "delete" in lowered:
                return (
                    409,
                    "This record cannot be removed because other records depend on it.",
                    "record_in_use",
                )
            return (
                400,
                "A linked record (for example vendor, item or purchase order) does not exist.",
                "invalid_reference",
            )
        null_match = _NULL_COLUMN.search(raw)
        if null_match or "not-null" in lowered or "not null" in lowered:
            col = f": {_humanize(null_match.group(1))}" if null_match else ""
            return 400, f"A required field is missing{col}.", "missing_required_field"
        if "check constraint" in lowered:
            return 400, "One of the values entered is not allowed.", "invalid_value"
        return 409, "This change conflicts with existing data.", "data_conflict"

    if isinstance(exc, DataError):
        if "too long" in lowered:
            return 400, "One of the values entered is too long.", "value_too_long"
        if "out of range" in lowered:
            return 400, "One of the numbers entered is out of the allowed range.", "value_out_of_range"
        return 400, "One of the values entered has an invalid format.", "invalid_value"

    if isinstance(exc, (OperationalError, InterfaceError, SQLAlchemyTimeoutError)):
        return 503, DB_UNAVAILABLE_MESSAGE, "database_unavailable"

    return 500, GENERIC_SERVER_MESSAGE, "database_error"


def _validation_message(errors: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for err in errors:
        loc = [str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path", "header", "cookie")]
        field = " > ".join(_humanize(p) for p in loc if not p.isdigit())
        kind = str(err.get("type", ""))
        msg = str(err.get("msg", "is invalid"))
        if "missing" in kind or kind == "value_error.missing":
            text = f"{field or 'A required field'} is required"
        elif field:
            text = f"{field}: {msg[:1].lower() + msg[1:] if msg else 'is invalid'}"
        else:
            text = msg
        if text not in parts:
            parts.append(text)
    if not parts:
        return _STATUS_MESSAGES[422]
    shown = parts[:3]
    extra = len(parts) - len(shown)
    suffix = f" (and {extra} more)" if extra > 0 else ""
    return "; ".join(shown) + suffix + "."


def _error_response(
    status_code: int,
    message: str,
    *,
    code: str,
    detail: Any = None,
    error_id: Optional[str] = None,
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    content: dict[str, Any] = {
        "detail": message if detail is None else detail,
        "message": message,
        "code": code,
    }
    if error_id:
        content["error_id"] = error_id
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _friendly_from_text(status_code: int, text: str) -> str:
    """Turn developer / driver text into something safe to show a user."""
    if re.search(r"duplicate key|unique constraint", text, re.IGNORECASE):
        return "A record with the same details already exists."
    if re.search(r"foreign key", text, re.IGNORECASE):
        return "A linked record does not exist, or other records depend on this one."
    if re.search(r"null value in column|not-null", text, re.IGNORECASE):
        return "A required field is missing."
    # "Error updating RFQ header: <raw exception>" -> keep the readable prefix only.
    prefix = text.split(":", 1)[0].strip() if ":" in text else ""
    if prefix and len(prefix) <= 80 and not looks_technical(prefix):
        return f"{prefix.rstrip('.')}. Please try again."
    return status_message(status_code)


def _sanitize_http_detail(status_code: int, detail: Any) -> tuple[Any, str]:
    """Return (detail_for_client, user_message) for an HTTPException."""
    if isinstance(detail, str) and detail.strip():
        text = detail.strip()
        if looks_technical(text) or (status_code >= 500 and ":" in text):
            logger.error("HTTP %s detail hidden from client: %s", status_code, text)
            message = _friendly_from_text(status_code, text)
            return message, message
        return detail, text

    if isinstance(detail, dict):
        inner = detail.get("message") or detail.get("detail") or detail.get("error")
        if isinstance(inner, str) and inner.strip() and not looks_technical(inner):
            return detail, inner

    message = status_message(status_code)
    return message, message


def _new_error_id() -> str:
    return uuid.uuid4().hex[:8]


def _log_unhandled(request: Request, exc: BaseException, error_id: str) -> None:
    logger.error(
        "[UNHANDLED %s] %s %s",
        error_id,
        request.method,
        request.url.path,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def _is_upstream_error(exc: BaseException) -> bool:
    module = type(exc).__module__ or ""
    return module.startswith(("requests", "httpx", "urllib3", "httpcore", "redis", "aiohttp")) or isinstance(
        exc, (ConnectionError, TimeoutError)
    )


def exception_to_response(request: Request, exc: BaseException) -> JSONResponse:
    """Map any exception raised by an endpoint to a friendly JSON response."""
    if isinstance(exc, StarletteHTTPException):
        detail, message = _sanitize_http_detail(exc.status_code, exc.detail)
        if exc.status_code >= 500:
            logger.error("[HTTP %s] %s %s -> %s", exc.status_code, request.method, request.url.path, exc.detail)
        return _error_response(
            exc.status_code,
            message,
            code=f"http_{exc.status_code}",
            detail=detail,
            headers=getattr(exc, "headers", None),
        )

    error_id = _new_error_id()

    if isinstance(exc, SQLAlchemyError):
        status_code, message, code = friendly_db_message(exc)
        _log_unhandled(request, exc, error_id)
        return _error_response(status_code, message, code=code, error_id=error_id)

    if isinstance(exc, (ConnectionError, TimeoutError)) or _is_upstream_error(exc):
        _log_unhandled(request, exc, error_id)
        return _error_response(
            status.HTTP_502_BAD_GATEWAY,
            UPSTREAM_UNAVAILABLE_MESSAGE,
            code="upstream_unavailable",
            error_id=error_id,
        )

    if isinstance(exc, PermissionError):
        return _error_response(403, _STATUS_MESSAGES[403], code="forbidden", error_id=error_id)

    if isinstance(exc, FileNotFoundError):
        _log_unhandled(request, exc, error_id)
        return _error_response(404, "The requested file could not be found.", code="file_not_found", error_id=error_id)

    _log_unhandled(request, exc, error_id)
    return _error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        GENERIC_SERVER_MESSAGE,
        code="internal_error",
        error_id=error_id,
    )


def install_error_handling(app: FastAPI) -> None:
    """Register handlers. Call this BEFORE adding CORSMiddleware.

    The catch-all middleware must sit *inside* CORS so that even 500 responses carry CORS
    headers; otherwise browsers hide the real error behind a generic "Network Error".
    """

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # Never echo the submitted `input` back (it may hold passwords / personal data).
        raw_errors = [{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]
        try:
            errors = jsonable_encoder(raw_errors, custom_encoder={BaseException: str})
        except Exception:  # pragma: no cover - defensive; ctx may hold exotic objects
            errors = [{"msg": str(e.get("msg", "Invalid value")), "loc": e.get("loc", [])} for e in raw_errors]
        logger.warning(
            "[VALIDATION ERROR] %s %s query=%s errors=%s",
            request.method,
            request.url.path,
            dict(request.query_params),
            errors,
        )
        message = _validation_message(errors)
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            message,
            code="validation_error",
            detail=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException):
        return exception_to_response(request, exc)

    @app.exception_handler(SQLAlchemyError)
    async def _db_handler(request: Request, exc: SQLAlchemyError):
        return exception_to_response(request, exc)

    @app.middleware("http")
    async def _catch_all_middleware(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - last line of defence
            return exception_to_response(request, exc)
