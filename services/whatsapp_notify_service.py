"""
Central WhatsApp notification client for external-sourcing-backend.

Primary path: POST {WHATSAPP_BACKEND_URL}/<endpoint> (Nest whatsapp-backend).
Fallback: Meta WhatsApp Cloud API when WHATSAPP_TOKEN + WHATSAPP_PHONE_ID are set.

All notify helpers are best-effort and never raise — callers must not depend on delivery.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

import requests

from config import settings

logger = logging.getLogger(__name__)

_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def to_whatsapp_http_url(url: str) -> str:
    """Use HTTPS on TLS domains; downgrade only bare EC2 :port URLs."""
    u = (url or "").strip()
    lower = u.lower()
    if not lower.startswith("https://"):
        return u
    if (
        "amazonaws.com" in lower
        or ":3002" in lower
        or ":8002" in lower
        or ":3004" in lower
    ):
        return "http://" + u[8:]
    return u


def extract_http_urls(text: str) -> List[str]:
    """Return unique http(s) URLs found in notification text."""
    seen: set[str] = set()
    urls: List[str] = []
    for match in _URL_PATTERN.findall(text or ""):
        url = match.strip().rstrip(".,;:!?)")
        key = url.lower()
        if not url or key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def message_contains_url(text: str, page_url: str) -> bool:
    target = (page_url or "").strip().rstrip("/").lower()
    if not target:
        return True
    return any(
        url.rstrip("/").lower() == target for url in extract_http_urls(text)
    )


def append_page_link_to_message(
    message: str,
    page_url: Optional[str],
    *,
    label: str = "Open page",
) -> str:
    """Ensure the vendor portal page URL is present in the WhatsApp body."""
    base = (message or "").strip()
    url = to_whatsapp_http_url(page_url or "")
    if not url:
        return base
    if message_contains_url(base, url):
        return base
    if not base:
        return f"{label}:\n{url}"
    return f"{base}\n\n{label}:\n{url}"


def _build_template_body_params(
    text: str,
    param_count: Optional[int] = None,
    *,
    po_number: Optional[str] = None,
) -> List[str]:
    count = param_count
    if count is None:
        raw = str(
            getattr(settings, "whatsapp_notify_template_body_param_count", None) or "2"
        ).strip()
        try:
            count = max(1, int(raw))
        except ValueError:
            count = 2

    normalized = (text or "").strip()
    if count <= 1:
        po = (po_number or "").strip()
        if po:
            return [_sanitize_template_param(po, 1024)]
        if normalized:
            import re

            if po_match := re.search(r"PO Number:\s*(\S+)", normalized, re.I):
                return [_sanitize_template_param(po_match.group(1), 1024)]
            if grn_match := re.search(r"GRN Number:\s*(\S+)", normalized, re.I):
                return [_sanitize_template_param(grn_match.group(1), 1024)]
            if inv_match := re.search(r"Invoice:\s*(\S+)", normalized, re.I):
                return [_sanitize_template_param(inv_match.group(1).strip(), 1024)]
            return [_sanitize_template_param(normalized, 1024)]
        return [""]

    if not normalized:
        return [""] * count

    import re

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    short_max = int(
        getattr(settings, "whatsapp_notify_template_short_param_max_length", 30) or 30
    )

    po_number = None
    grn_number = None
    invoice_number = None
    vendor_name = None
    if vendor_match := re.search(r"Vendor:\s*([^\n]+)", normalized, re.I):
        vendor_name = vendor_match.group(1).strip()
    if po_match := re.search(r"PO Number:\s*(\S+)", normalized, re.I):
        po_number = po_match.group(1)
    if grn_match := re.search(r"GRN Number:\s*(\S+)", normalized, re.I):
        grn_number = grn_match.group(1)
    if inv_match := re.search(r"Invoice:\s*(\S+)", normalized, re.I):
        invoice_number = inv_match.group(1).strip()

    handoff_role = None
    if re.search(r"submitted to vendor", normalized, re.I):
        handoff_role = "Vendor"
    elif re.search(r"submitted to buyer", normalized, re.I):
        handoff_role = "Buyer"

    headline = re.sub(r"^[^\w]+", "", lines[0]).strip() if lines else "Notification"

    if vendor_name and po_number:
        param1, param2 = vendor_name, po_number
    elif handoff_role and po_number:
        param1, param2 = vendor_name or handoff_role, po_number
    elif grn_number and po_number:
        param1, param2 = grn_number, po_number
    elif invoice_number and po_number:
        param1, param2 = invoice_number, po_number
    elif grn_number:
        param1, param2 = "GRN created", grn_number
    elif po_number:
        param1, param2 = "Purchase Order", po_number
    else:
        param1, param2 = headline or "Notification", headline or "Update"

    if count == 2:
        return [
            _sanitize_template_param(param1, short_max),
            _sanitize_template_param(param2, short_max),
        ]

    params = [
        _sanitize_template_param(param1, short_max),
        _sanitize_template_param(param2, short_max),
    ]
    while len(params) < count:
        params.append("")
    return params[:count]


def _sanitize_template_param(text: str, max_length: int = 1024) -> str:
    """Meta template vars cannot contain newlines/tabs or 5+ consecutive spaces."""
    import re

    cleaned = re.sub(r"[\r\n\t]+", " ", str(text or ""))
    cleaned = re.sub(r" {5,}", "    ", cleaned)
    cleaned = re.sub(r" {2,4}", " ", cleaned)
    return cleaned.strip()[:max_length]


class WhatsAppNotifyService:
    """HTTP client for whatsapp-backend notify endpoints + Meta direct fallback."""

    def __init__(self, *, log_prefix: str = "WHATSAPP") -> None:
        self.log_prefix = log_prefix

    def _log(self, message: str) -> None:
        print(message)
        logger.info(message)

    def backend_base_url(self) -> Optional[str]:
        base = (getattr(settings, "whatsapp_backend_url", None) or "").strip().rstrip("/")
        return base or None

    def post_backend(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        *,
        timeout: int = 15,
    ) -> Optional[Dict[str, Any]]:
        """POST JSON to whatsapp-backend (e.g. ``/po-notify``). Returns result dict or None."""
        base = self.backend_base_url()
        if not base:
            self._log(
                f"[{self.log_prefix}] WHATSAPP_BACKEND_URL unset — skip whatsapp-backend path"
            )
            return None

        path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{base}{path}"
        self._log(f"[{self.log_prefix}][1/3] POST {url} payload={payload}")
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            body: Any = None
            try:
                body = resp.json() if resp.content else None
            except Exception:
                body = resp.text
            self._log(
                f"[{self.log_prefix}][2/3] whatsapp-backend status={resp.status_code} body={body}"
            )
            if resp.ok and isinstance(body, dict) and body.get("sent") is True:
                self._log(f"[{self.log_prefix}][3/3] ✅ Sent via whatsapp-backend")
                return {"ok": True, "via": "whatsapp-backend", **body}
            self._log(
                f"[{self.log_prefix}][3/3] ⚠️ whatsapp-backend did not confirm send — trying Meta direct"
            )
            return {
                "ok": False,
                "via": "whatsapp-backend",
                "status": resp.status_code,
                "body": body,
            }
        except Exception as exc:
            self._log(
                f"[{self.log_prefix}][3/3] ❌ whatsapp-backend request failed: {exc} — trying Meta direct"
            )
            return None

    def send_text_meta(self, *, to: str, text: str, timeout: int = 20) -> Dict[str, Any]:
        """Send a plain-text WhatsApp message via Meta Graph API."""
        return self._send_meta_message(
            to=to,
            payload={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
            timeout=timeout,
            kind="text",
        )

    def send_template_meta(
        self,
        *,
        to: str,
        text: str,
        template_name: Optional[str] = None,
        template_lang: Optional[str] = None,
        template_body_params: Optional[List[str]] = None,
        template_url_button_param: Optional[str] = None,
        po_number: Optional[str] = None,
        po_id: Optional[int] = None,
        timeout: int = 20,
    ) -> Dict[str, Any]:
        """Send an approved Meta template (works outside the 24h session window)."""
        name = (template_name or getattr(settings, "whatsapp_notify_template", None) or "").strip()
        lang = (
            template_lang
            or getattr(settings, "whatsapp_notify_template_lang", None)
            or "en_US"
        ).strip()
        if not name:
            return {"ok": False, "sent": False, "reason": "template-not-configured"}

        if template_body_params is not None:
            body_params = [_sanitize_template_param(param) for param in template_body_params]
        else:
            body_params = [
                _sanitize_template_param(param)
                for param in _build_template_body_params(text, po_number=po_number)
            ]
        url_button = (
            (template_url_button_param or "").strip()
            or build_po_template_url_button_param(po_id)
            or ""
        )
        components = _build_meta_template_components(
            body_params,
            url_button_param=url_button or None,
        )
        return self._send_meta_message(
            to=to,
            payload={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "template",
                "template": {
                    "name": name,
                    "language": {"code": lang},
                    "components": components,
                },
            },
            timeout=timeout,
            kind=f"template:{name}",
        )

    def _meta_error_code(self, body: Any) -> Optional[int]:
        if not isinstance(body, dict):
            return None
        error = body.get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("code")
        return int(code) if isinstance(code, int) else None

    def _is_outside_window_error(self, body: Any) -> bool:
        code = self._meta_error_code(body)
        return code in (131047, 470)

    def _send_meta_message(
        self,
        *,
        to: str,
        payload: Dict[str, Any],
        timeout: int,
        kind: str,
    ) -> Dict[str, Any]:
        token = (getattr(settings, "whatsapp_token", None) or "").strip()
        phone_id = (getattr(settings, "whatsapp_phone_id", None) or "").strip()
        if not token or not phone_id:
            self._log(
                f"[{self.log_prefix}] Meta direct skipped: "
                "WHATSAPP_TOKEN / WHATSAPP_PHONE_ID not set"
            )
            return {"ok": False, "sent": False, "reason": "meta-not-configured"}

        graph_base = (
            getattr(settings, "whatsapp_graph_api_base", None) or ""
        ).strip().rstrip("/")
        if not graph_base:
            self._log(
                f"[{self.log_prefix}] Meta direct skipped: WHATSAPP_GRAPH_API_BASE not set"
            )
            return {"ok": False, "sent": False, "reason": "meta-not-configured"}

        url = f"{graph_base}/{phone_id}/messages"
        self._log(
            f"[{self.log_prefix}][1/3] Meta Graph API ({kind}) → to={to} phone_id={phone_id}"
        )
        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
            body: Any = None
            try:
                body = resp.json() if resp.content else None
            except Exception:
                body = resp.text
            if resp.ok:
                msg_id = None
                if isinstance(body, dict):
                    messages = body.get("messages") or []
                    if messages and isinstance(messages[0], dict):
                        msg_id = messages[0].get("id")
                self._log(
                    f"[{self.log_prefix}][3/3] ✅ Meta accepted {kind} message"
                    + (f" id={msg_id}" if msg_id else "")
                )
                return {"ok": True, "sent": True, "via": "meta-direct", "result": body}
            self._log(
                f"[{self.log_prefix}][3/3] ❌ Meta API error status={resp.status_code} body={body}"
            )
            return {
                "ok": False,
                "sent": False,
                "via": "meta-direct",
                "status": resp.status_code,
                "body": body,
            }
        except Exception as exc:
            self._log(f"[{self.log_prefix}][3/3] ❌ Meta API request failed: {exc}")
            return {"ok": False, "sent": False, "via": "meta-direct", "error": str(exc)}

    def send_meta_with_window_fallback(
        self,
        *,
        to: str,
        text: str,
        template_name: Optional[str] = None,
        template_lang: Optional[str] = None,
        template_body_params: Optional[List[str]] = None,
        template_url_button_param: Optional[str] = None,
        po_number: Optional[str] = None,
        po_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Meta direct fallback: try free-form first; if blocked outside 24h window,
        retry with an approved template when configured.
        """
        text_result = self.send_text_meta(to=to, text=text)
        if text_result.get("ok") and text_result.get("sent"):
            return text_result

        body = text_result.get("body")
        if self._is_outside_window_error(body):
            self._log(
                f"[{self.log_prefix}] Free-form blocked outside 24h window → trying template"
            )
            template_result = self.send_template_meta(
                to=to,
                text=text,
                template_name=template_name,
                template_lang=template_lang,
                template_body_params=template_body_params,
                template_url_button_param=template_url_button_param,
                po_number=po_number,
                po_id=po_id,
            )
            if template_result.get("ok") and template_result.get("sent"):
                self._send_page_link_followups(to=to, source_text=text)
                return template_result
            return template_result

        return text_result

    def _send_page_link_followups(self, *, to: str, source_text: str) -> None:
        """Best-effort: send page URL(s) after a template (works inside the 24h window)."""
        for url in extract_http_urls(source_text):
            try:
                result = self.send_text_meta(to=to, text=url)
                if result.get("ok") and result.get("sent"):
                    self._log(f"[{self.log_prefix}] Sent page link follow-up: {url}")
            except Exception as exc:
                self._log(
                    f"[{self.log_prefix}] Page link follow-up failed for {url}: {exc}"
                )

    def notify_with_fallback(
        self,
        *,
        endpoint: str,
        payload: Dict[str, Any],
        fallback_text: str,
        to: Optional[str] = None,
        default_recipient: Optional[str] = None,
        no_recipient_reason: str = "no-recipient",
        template_name: Optional[str] = None,
        template_lang: Optional[str] = None,
        template_body_params: Optional[List[str]] = None,
        template_url_button_param: Optional[str] = None,
        po_number: Optional[str] = None,
        po_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Try whatsapp-backend endpoint first; on failure use Meta direct with fallback text.
        """
        via_backend = self.post_backend(endpoint, payload)
        if isinstance(via_backend, dict) and via_backend.get("ok") and via_backend.get("sent"):
            return via_backend

        recipient = (to or "").strip() or (default_recipient or "").strip() or None
        if not recipient:
            warn = f"[{self.log_prefix}] No recipient configured"
            self._log(warn)
            logger.warning(warn)
            return {"ok": False, "sent": False, "reason": no_recipient_reason}

        return self.send_meta_with_window_fallback(
            to=recipient,
            text=fallback_text,
            template_name=template_name,
            template_lang=template_lang,
            template_body_params=template_body_params,
            template_url_button_param=template_url_button_param,
            po_number=po_number,
            po_id=po_id,
        )


_default_service = WhatsAppNotifyService()


def _resolve_recipient(
    to: Optional[str],
    *configured_numbers: Optional[str],
) -> Optional[str]:
    explicit = (to or "").strip()
    if explicit:
        return explicit
    for number in configured_numbers:
        candidate = (number or "").strip()
        if candidate:
            return candidate
    return None


def normalize_whatsapp_phone(mobile: str) -> str:
    """E.164-ish digits for Meta (e.g. 919820187393)."""
    digits = re.sub(r"\D", "", str(mobile or ""))
    if len(digits) == 10:
        return f"91{digits}"
    return digits


def resolve_handoff_recipient(
    handoff_to: str,
    *,
    vendor_mobile: Optional[str] = None,
    explicit_to: Optional[str] = None,
) -> Optional[str]:
    """Resolve WhatsApp recipient for PO editor handoff."""
    explicit = (explicit_to or "").strip()
    if explicit:
        return normalize_whatsapp_phone(explicit)

    role = (handoff_to or "").strip().upper()
    if role == "VENDOR":
        mobile = normalize_whatsapp_phone(vendor_mobile or "")
        if mobile:
            return mobile
        return _resolve_recipient(None, settings.vendor_notify_number)
    if role == "BUYER":
        return _resolve_recipient(
            None,
            settings.buyer_notify_number,
            settings.po_notify_number,
        )
    return None


def build_po_page_url(po_id: Optional[int] = None) -> str:
    """Vendor purchase order detail page URL."""
    base = to_whatsapp_http_url(
        (settings.vendor_frontend_url or "http://localhost:3002").strip().rstrip("/")
    )
    if po_id is not None and int(po_id) > 0:
        path = f"{base}/vendor/purchase-orders/{int(po_id)}"
        if "{{" in path or "}}" in path:
            raise ValueError("Invalid WhatsApp PO URL contains template placeholders")
        return path
    return f"{base}/vendor/purchase-orders"


def build_po_template_url_button_param(po_id: Optional[int] = None) -> Optional[str]:
    """Meta dynamic URL button {{1}} — numeric po_id for /vendor/purchase-orders/{po_id}."""
    if po_id is not None and int(po_id) > 0:
        return str(int(po_id))
    return None


def _build_meta_template_components(
    body_params: List[str],
    *,
    url_button_param: Optional[str] = None,
) -> List[Dict[str, Any]]:
    components: List[Dict[str, Any]] = []
    if body_params:
        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": param} for param in body_params],
            }
        )
    button = (url_button_param or "").strip()
    if button:
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [
                    {"type": "text", "text": _sanitize_template_param(button)}
                ],
            }
        )
    return components


def build_invoice_page_url(po_number: Optional[str] = None) -> str:
    """Vendor invoice review page URL, optionally scoped to a PO."""
    base = to_whatsapp_http_url(
        (settings.vendor_frontend_url or "http://localhost:3002").strip().rstrip("/")
    )
    po = (po_number or "").strip()
    if po:
        return f"{base}/vendor/invoice-review/{quote(po)}"
    return f"{base}/vendor/invoice-review"


def build_grn_page_url(po_number: Optional[str] = None) -> str:
    """Vendor Add GRN page URL, optionally pre-filtered by PO number."""
    base = to_whatsapp_http_url(settings.grn_page_url)
    po = (po_number or "").strip()
    if po:
        return f"{base}?po={quote(po)}"
    return base


def _build_po_message(
    *,
    po_number: Optional[str] = None,
    vendor_name: Optional[str] = None,
    line_count: Optional[int] = None,
    grn_page_url: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    grn_url = (grn_page_url or build_grn_page_url(po_number)).strip()
    custom = (message or "").strip()
    if custom:
        return append_page_link_to_message(
            custom, grn_url, label="Add GRN for this delivery"
        )
    lines = ["✅ New Purchase Order created"]
    if po_number and str(po_number).strip():
        lines.append(f"PO Number: {str(po_number).strip()}")
    if vendor_name and str(vendor_name).strip():
        lines.append(f"Vendor: {str(vendor_name).strip()}")
    if isinstance(line_count, int) and line_count > 0:
        lines.append(f"Line items: {line_count}")
    if grn_url:
        lines.append("")
        lines.append("Add GRN for this delivery:")
        lines.append(grn_url)
    return "\n".join(lines)


def _build_grn_message(
    *,
    grn_number: Optional[str] = None,
    po_number: Optional[str] = None,
    po_page_url: Optional[str] = None,
    line_count: Optional[int] = None,
    message: Optional[str] = None,
    po_id: Optional[int] = None,
) -> str:
    po_url = (po_page_url or build_po_page_url(po_id)).strip()
    custom = (message or "").strip()
    if custom:
        return append_page_link_to_message(
            custom, po_url, label="View purchase order"
        )
    lines = ["✅ GRN created"]
    if grn_number and str(grn_number).strip():
        lines.append(f"GRN Number: {str(grn_number).strip()}")
    if po_number and str(po_number).strip():
        lines.append(f"PO Number: {str(po_number).strip()}")
    if isinstance(line_count, int) and line_count > 0:
        lines.append(f"Line items: {line_count}")
    if po_url:
        lines.append("")
        lines.append("View purchase order:")
        lines.append(po_url)
    return "\n".join(lines)


def _build_invoice_message(
    *,
    po_number: Optional[str] = None,
    invoice_no: Optional[str] = None,
    invoice_nos: Optional[Sequence[str]] = None,
    line_count: Optional[int] = None,
    invoice_page_url: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    invoice_url = (invoice_page_url or build_invoice_page_url(po_number)).strip()
    custom = (message or "").strip()
    if custom:
        return append_page_link_to_message(
            custom, invoice_url, label="Review invoice"
        )

    numbers = [str(n).strip() for n in (invoice_nos or []) if str(n).strip()]
    single_invoice = (invoice_no or "").strip() or (
        numbers[0] if len(numbers) == 1 else ""
    )

    lines = ["✅ Invoice confirmed by buyer"]
    if po_number and str(po_number).strip():
        lines.append(f"PO Number: {str(po_number).strip()}")
    if single_invoice:
        lines.append(f"Invoice: {single_invoice}")
    elif len(numbers) > 1:
        lines.append(f"Invoices: {', '.join(numbers)}")
    if isinstance(line_count, int) and line_count > 0:
        lines.append(f"Lines confirmed: {line_count}")
    if invoice_url:
        lines.append("")
        lines.append("Review invoice:")
        lines.append(invoice_url)
    return "\n".join(lines)


def _build_handoff_message(
    *,
    handoff_to: str,
    po_number: Optional[str] = None,
    po_page_url: Optional[str] = None,
    vendor_name: Optional[str] = None,
    message: Optional[str] = None,
    po_id: Optional[int] = None,
) -> str:
    page_url = (po_page_url or build_po_page_url(po_id)).strip()
    custom = (message or "").strip()
    if custom:
        return append_page_link_to_message(
            custom, page_url, label="Open purchase order"
        )

    role = (handoff_to or "").strip().upper()
    role_label = "vendor" if role == "VENDOR" else "buyer"
    lines = [f"✅ Purchase order submitted to {role_label}"]
    if po_number and str(po_number).strip():
        lines.append(f"PO Number: {str(po_number).strip()}")
    if vendor_name and str(vendor_name).strip():
        lines.append(f"Vendor: {str(vendor_name).strip()}")
    lines.append("You can now edit this purchase order.")
    if page_url:
        lines.append("")
        lines.append("Open purchase order:")
        lines.append(page_url)
    return "\n".join(lines)


def notify_po_handoff_whatsapp(
    *,
    handoff_to: str,
    po_number: Optional[str] = None,
    po_id: Optional[int] = None,
    vendor_name: Optional[str] = None,
    vendor_mobile: Optional[str] = None,
    to: Optional[str] = None,
    message: Optional[str] = None,
    service: Optional[WhatsAppNotifyService] = None,
) -> Dict[str, Any]:
    """Best-effort WhatsApp notify when PO current_editor handoff changes. Never raises."""
    role = (handoff_to or "").strip().upper()
    if role not in ("BUYER", "VENDOR"):
        return {"ok": False, "sent": False, "reason": "invalid-handoff-to"}

    svc = service or WhatsAppNotifyService(log_prefix="PO-HANDOFF-WHATSAPP")
    recipient = resolve_handoff_recipient(
        role, vendor_mobile=vendor_mobile, explicit_to=to
    )
    svc._log(
        "[PO-HANDOFF-WHATSAPP][0/3] PO handoff → notify WhatsApp "
        f"handoff_to={role} po_number={po_number} po_id={po_id} "
        f"recipient={'set' if recipient else 'unset'}"
    )

    page_url = build_po_page_url(po_id)
    payload: Dict[str, Any] = {
        "handoffTo": role,
        "poNumber": po_number,
        "poId": po_id,
        "poPageUrl": page_url,
        "vendorName": vendor_name,
    }
    if recipient:
        payload["to"] = recipient
    if message:
        payload["message"] = message

    fallback_text = _build_handoff_message(
        handoff_to=role,
        po_number=po_number,
        po_page_url=page_url,
        vendor_name=vendor_name,
        message=message,
        po_id=po_id,
    )

    handoff_template = (
        getattr(settings, "whatsapp_po_handoff_notify_template", None)
        or getattr(settings, "whatsapp_po_notify_template", None)
        or getattr(settings, "whatsapp_notify_template", None)
        or ""
    ).strip()
    handoff_template_lang = (
        getattr(settings, "whatsapp_po_handoff_notify_template_lang", None)
        or getattr(settings, "whatsapp_po_notify_template_lang", None)
        or getattr(settings, "whatsapp_notify_template_lang", None)
        or "en"
    ).strip()
    po_ref = (po_number or "").strip()
    handoff_template_params = [po_ref] if po_ref else None
    handoff_url_button = build_po_template_url_button_param(po_id)

    return svc.notify_with_fallback(
        endpoint="/po-handoff-notify",
        payload=payload,
        fallback_text=fallback_text,
        to=recipient,
        default_recipient=recipient,
        no_recipient_reason="no-recipient",
        template_name=handoff_template or None,
        template_lang=handoff_template_lang,
        template_body_params=handoff_template_params,
        template_url_button_param=handoff_url_button,
        po_number=po_number,
        po_id=po_id,
    )


def notify_po_created_whatsapp(
    *,
    po_number: Optional[str] = None,
    po_id: Optional[int] = None,
    vendor_name: Optional[str] = None,
    line_count: Optional[int] = None,
    to: Optional[str] = None,
    message: Optional[str] = None,
    service: Optional[WhatsAppNotifyService] = None,
) -> Dict[str, Any]:
    """Best-effort WhatsApp notify after PO create. Never raises."""
    svc = service or WhatsAppNotifyService(log_prefix="PO-WHATSAPP")
    svc._log(
        f"[PO-WHATSAPP][0/3] PO created → notify WhatsApp "
        f"po_number={po_number} po_id={po_id} line_count={line_count} vendor={vendor_name}"
    )

    grn_url = build_grn_page_url(po_number)
    payload: Dict[str, Any] = {
        "poNumber": po_number,
        "vendorName": vendor_name,
        "lineCount": line_count,
        "grnPageUrl": grn_url,
    }
    if po_id is not None:
        payload["poId"] = po_id
    if to:
        payload["to"] = to
    if message:
        payload["message"] = message

    po_template = (
        getattr(settings, "whatsapp_po_notify_template", None)
        or getattr(settings, "whatsapp_notify_template", None)
        or ""
    ).strip()
    po_template_lang = (
        getattr(settings, "whatsapp_po_notify_template_lang", None)
        or getattr(settings, "whatsapp_notify_template_lang", None)
        or "en"
    ).strip()
    po_ref = (po_number or "").strip()
    po_template_params = [po_ref] if po_ref else None
    po_url_button = build_po_template_url_button_param(po_id)

    return svc.notify_with_fallback(
        endpoint="/po-notify",
        payload=payload,
        fallback_text=_build_po_message(
            po_number=po_number,
            vendor_name=vendor_name,
            line_count=line_count,
            grn_page_url=grn_url,
            message=message,
        ),
        to=to,
        default_recipient=_resolve_recipient(to, settings.po_notify_number),
        no_recipient_reason="no-recipient",
        template_name=po_template or None,
        template_lang=po_template_lang,
        template_body_params=po_template_params,
        template_url_button_param=po_url_button,
        po_number=po_number,
        po_id=po_id,
    )


def notify_grn_created_whatsapp(
    *,
    grn_number: Optional[str] = None,
    po_number: Optional[str] = None,
    po_id: Optional[int] = None,
    po_page_url: Optional[str] = None,
    line_count: Optional[int] = None,
    to: Optional[str] = None,
    message: Optional[str] = None,
    service: Optional[WhatsAppNotifyService] = None,
) -> Dict[str, Any]:
    """Best-effort WhatsApp notify after GRN create. Never raises."""
    svc = service or WhatsAppNotifyService(log_prefix="GRN-WHATSAPP")
    svc._log(
        "[GRN-WHATSAPP][0/3] GRN created → notify WhatsApp "
        f"grn_number={grn_number} po_number={po_number} po_id={po_id}"
    )

    page_url = (po_page_url or build_po_page_url(po_id)).strip()
    payload: Dict[str, Any] = {
        "grnNumber": grn_number,
        "poNumber": po_number,
        "lineCount": line_count,
        "poPageUrl": page_url,
    }
    if po_id is not None:
        payload["poId"] = po_id
    if to:
        payload["to"] = to
    if message:
        payload["message"] = message

    return svc.notify_with_fallback(
        endpoint="/grn-notify",
        payload=payload,
        fallback_text=_build_grn_message(
            grn_number=grn_number,
            po_number=po_number,
            po_page_url=page_url,
            line_count=line_count,
            message=message,
            po_id=po_id,
        ),
        to=to,
        default_recipient=_resolve_recipient(
            to,
            settings.grn_notify_number,
            settings.po_notify_number,
        ),
        no_recipient_reason="no-recipient",
    )


def notify_invoice_confirmed_whatsapp(
    *,
    po_number: Optional[str] = None,
    invoice_no: Optional[str] = None,
    invoice_nos: Optional[List[str]] = None,
    line_count: Optional[int] = None,
    to: Optional[str] = None,
    message: Optional[str] = None,
    service: Optional[WhatsAppNotifyService] = None,
) -> Dict[str, Any]:
    """Best-effort WhatsApp notify after buyer confirms invoice lines. Never raises."""
    svc = service or WhatsAppNotifyService(log_prefix="INVOICE-WHATSAPP")
    svc._log(
        "[INVOICE-WHATSAPP][0/3] Invoice confirmed → notify WhatsApp "
        f"po_number={po_number} invoice_no={invoice_no} line_count={line_count}"
    )

    payload: Dict[str, Any] = {
        "poNumber": po_number,
        "invoiceNo": invoice_no,
        "lineCount": line_count,
        "invoicePageUrl": build_invoice_page_url(po_number),
    }
    if invoice_nos:
        payload["invoiceNos"] = invoice_nos
    if to:
        payload["to"] = to
    if message:
        payload["message"] = message

    return svc.notify_with_fallback(
        endpoint="/invoice-notify",
        payload=payload,
        fallback_text=_build_invoice_message(
            po_number=po_number,
            invoice_no=invoice_no,
            invoice_nos=invoice_nos,
            line_count=line_count,
            invoice_page_url=payload["invoicePageUrl"],
            message=message,
        ),
        to=to,
        default_recipient=_resolve_recipient(
            to,
            settings.invoice_notify_number,
            settings.po_notify_number,
        ),
        no_recipient_reason="no-recipient",
    )


def build_setup_password_url(email: Optional[str] = None) -> str:
    """Vendor set-password page URL, optionally prefilled with email."""
    base = to_whatsapp_http_url(
        (settings.vendor_frontend_url or "http://localhost:3002").strip().rstrip("/")
    )
    path = f"{base}/vendor/setup-password"
    em = (email or "").strip()
    if em:
        return f"{path}?email={quote(em)}"
    return path


def _build_vendor_approved_message(
    *,
    vendor_name: Optional[str] = None,
    vendor_email: Optional[str] = None,
    setup_password_url: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    if message and str(message).strip():
        return append_page_link_to_message(
            str(message).strip(),
            setup_password_url,
            label="Set your password",
        )

    lines = ["✅ Your vendor registration has been approved"]
    if vendor_name and str(vendor_name).strip():
        lines.append(f"Vendor: {str(vendor_name).strip()}")
    if vendor_email and str(vendor_email).strip():
        lines.append(f"Email: {str(vendor_email).strip()}")
    lines.append("Set your password to log in:")
    if setup_password_url:
        lines.append(setup_password_url)
    return "\n".join(lines)


def notify_vendor_approved_whatsapp(
    *,
    vendor_id: Optional[int] = None,
    vendor_name: Optional[str] = None,
    vendor_email: Optional[str] = None,
    vendor_mobile: Optional[str] = None,
    to: Optional[str] = None,
    message: Optional[str] = None,
    service: Optional[WhatsAppNotifyService] = None,
) -> Dict[str, Any]:
    """
    Best-effort WhatsApp notify after vendor approve → ACTIVE.
    Prefer whatsapp-backend (template-aware, same as PO handoff). Never raises.
    """
    svc = service or WhatsAppNotifyService(log_prefix="VENDOR-APPROVE-WHATSAPP")

    mobile_digits = normalize_whatsapp_phone(vendor_mobile or "")
    recipient = mobile_digits or normalize_whatsapp_phone(to or "") or None

    setup_url = build_setup_password_url(vendor_email)
    fallback_text = _build_vendor_approved_message(
        vendor_name=vendor_name,
        vendor_email=vendor_email,
        setup_password_url=setup_url,
        message=message,
    )
    svc._log(
        "[VENDOR-APPROVE-WHATSAPP][0/3] Vendor approved → notify WhatsApp "
        f"vendor_id={vendor_id} recipient={'set' if recipient else 'unset'} "
        f"setup_url={setup_url}"
    )

    payload: Dict[str, Any] = {
        "vendorId": vendor_id,
        "vendorName": vendor_name,
        "vendorEmail": vendor_email,
        "setupPasswordUrl": setup_url,
    }
    if recipient:
        payload["to"] = recipient
    if message:
        payload["message"] = message

    # Primary path: whatsapp-backend (uses approved Meta template outside 24h window).
    via_backend = svc.post_backend("/vendor-approved-notify", payload)
    if (
        isinstance(via_backend, dict)
        and via_backend.get("ok")
        and via_backend.get("sent")
    ):
        return via_backend

    if not recipient:
        warn = "[VENDOR-APPROVE-WHATSAPP] No recipient configured"
        svc._log(warn)
        logger.warning(warn)
        return {"ok": False, "sent": False, "reason": "no-vendor-mobile"}

    # Meta fallback: account_creation_confirmation_3
    # Body {{1}} = name; URL button {{1}} = email (setup-password?email=)
    template_name = (
        getattr(settings, "whatsapp_vendor_approved_notify_template", None)
        or "account_creation_confirmation_3"
    ).strip()
    template_lang = (
        getattr(settings, "whatsapp_vendor_approved_notify_template_lang", None)
        or "en_US"
    ).strip()
    body_name = (
        (vendor_name or "").strip()
        or (vendor_email or "").strip()
        or (f"Vendor #{vendor_id}" if vendor_id is not None else "")
        or "Vendor"
    )
    body_params = [_sanitize_template_param(body_name, 1024)]
    # Static URL button on account_creation_confirmation_3 — do not send button params
    # (Meta #132018: "Url does not require parameters"). Opt-in if template is dynamic.
    url_dynamic = str(
        getattr(settings, "whatsapp_vendor_approved_notify_url_button_dynamic", None)
        or ""
    ).strip().lower() in ("1", "true", "yes")
    url_button = (
        ((vendor_email or "").strip() or None) if url_dynamic else None
    )

    if template_name:
        template_result = svc.send_template_meta(
            to=recipient,
            text=fallback_text,
            template_name=template_name,
            template_lang=template_lang,
            template_body_params=body_params,
            template_url_button_param=url_button,
        )
        if template_result.get("ok") and template_result.get("sent"):
            return template_result

    return svc.send_text_meta(to=recipient, text=fallback_text)
