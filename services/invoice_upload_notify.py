"""
WhatsApp the buyer and the store keeper when a vendor invoice is uploaded.

Recipients are the foundation users holding the BUYER / STOREKEEPER role in the
uploader's tenant + company (phone number from their user record). Both roles are
messaged at the same time. Best-effort: never raises; the outcome is returned so
the upload response can tell the user who was notified and which role is not
configured yet.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from config import settings
from services.whatsapp_notify_service import (
    WhatsAppNotifyService,
    append_page_link_to_message,
    build_invoice_page_url,
    normalize_whatsapp_phone,
)

logger = logging.getLogger(__name__)

# (role name in foundation, label shown to users)
INVOICE_UPLOAD_ROLES: Tuple[Tuple[str, str], ...] = (
    ("BUYER", "Buyer"),
    ("STOREKEEPER", "Store keeper"),
)


def _nestjs_base() -> str:
    return (settings.nestjs_api_url or "").strip().rstrip("/")


def fetch_role_recipients(
    bearer_token: str,
    role_names: Sequence[str],
) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    GET /users/notify-recipients → {ROLE: {"recipients": [...], "usersWithoutPhone": n}}.
    None when foundation can't be asked (not configured, unreachable, rejected).
    """
    base = _nestjs_base()
    token = (bearer_token or "").strip()
    if not base or not token:
        return None
    if not token.lower().startswith("bearer "):
        token = f"Bearer {token}"
    try:
        resp = requests.get(
            f"{base}/users/notify-recipients",
            params={"roles": ",".join(role_names)},
            headers={"Authorization": token},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("[INVOICE-UPLOAD-WHATSAPP] recipient lookup failed: %s", exc)
        return None
    if not resp.ok:
        logger.warning(
            "[INVOICE-UPLOAD-WHATSAPP] recipient lookup status=%s body=%s",
            resp.status_code,
            (resp.text or "")[:300],
        )
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, list):
        return None
    return {
        str(entry.get("role", "")).upper(): entry
        for entry in body
        if isinstance(entry, dict)
    }


def build_invoice_uploaded_message(
    *,
    invoice_no: Optional[str] = None,
    po_number: Optional[str] = None,
    vendor_name: Optional[str] = None,
    line_count: Optional[int] = None,
) -> str:
    lines = ["📄 New invoice uploaded"]
    if invoice_no and str(invoice_no).strip():
        lines.append(f"Invoice: {str(invoice_no).strip()}")
    if po_number and str(po_number).strip():
        lines.append(f"PO Number: {str(po_number).strip()}")
    if vendor_name and str(vendor_name).strip():
        lines.append(f"Vendor: {str(vendor_name).strip()}")
    if isinstance(line_count, int) and line_count > 0:
        lines.append(f"Line items: {line_count}")
    return "\n".join(lines)


def _send_one(
    svc: WhatsAppNotifyService,
    *,
    phone: str,
    text: str,
    page_url: str,
    invoice_no: Optional[str],
    po_number: Optional[str],
    line_count: Optional[int],
) -> bool:
    try:
        outcome = svc.notify_with_fallback(
            endpoint="/invoice-notify",
            payload={
                "to": phone,
                "message": text,
                "poNumber": po_number,
                "invoiceNo": invoice_no,
                "lineCount": line_count,
                "invoicePageUrl": page_url,
            },
            fallback_text=append_page_link_to_message(
                text, page_url, label="Review invoice"
            ),
            to=phone,
            default_recipient=phone,
            po_number=po_number,
        )
        return bool(outcome.get("ok") and outcome.get("sent"))
    except Exception as exc:
        logger.warning("[INVOICE-UPLOAD-WHATSAPP] send to %s failed: %s", phone, exc)
        return False


def _summary(notifications: List[Dict[str, Any]]) -> str:
    sent = [n["label"] for n in notifications if n["status"] == "sent"]
    parts: List[str] = []
    if sent:
        parts.append("WhatsApp sent to " + " and ".join(s.lower() for s in sent) + ".")
    parts.extend(n["message"] for n in notifications if n["status"] != "sent")
    return " ".join(parts)


def notify_invoice_uploaded(
    *,
    bearer_token: str,
    invoice_no: Optional[str] = None,
    po_number: Optional[str] = None,
    vendor_name: Optional[str] = None,
    line_count: Optional[int] = None,
    service: Optional[WhatsAppNotifyService] = None,
) -> Dict[str, Any]:
    """
    Message every buyer and store keeper in parallel.

    Returns {"notifications": [{role, label, status, sent, message}], "message": str}
    where status is "sent", "partial", "failed" or "not_configured".
    """
    svc = service or WhatsAppNotifyService(log_prefix="INVOICE-UPLOAD-WHATSAPP")
    role_names = [role for role, _ in INVOICE_UPLOAD_ROLES]

    found = fetch_role_recipients(bearer_token, role_names)
    if found is None:
        notifications = [
            {
                "role": role,
                "label": label,
                "status": "failed",
                "sent": 0,
                "message": f"Could not look up the {label.lower()} to notify.",
            }
            for role, label in INVOICE_UPLOAD_ROLES
        ]
        return {"notifications": notifications, "message": _summary(notifications)}

    text = build_invoice_uploaded_message(
        invoice_no=invoice_no,
        po_number=po_number,
        vendor_name=vendor_name,
        line_count=line_count,
    )
    page_url = build_invoice_page_url(po_number)

    jobs: List[Tuple[str, str]] = []  # (role, phone)
    for role, _ in INVOICE_UPLOAD_ROLES:
        entry = found.get(role) or {}
        for person in entry.get("recipients") or []:
            phone = normalize_whatsapp_phone(str(person.get("phoneNumber") or ""))
            if phone and (role, phone) not in jobs:
                jobs.append((role, phone))

    results: Dict[Tuple[str, str], bool] = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {
                job: pool.submit(
                    _send_one,
                    svc,
                    phone=job[1],
                    text=text,
                    page_url=page_url,
                    invoice_no=invoice_no,
                    po_number=po_number,
                    line_count=line_count,
                )
                for job in jobs
            }
            results = {job: future.result() for job, future in futures.items()}

    notifications: List[Dict[str, Any]] = []
    for role, label in INVOICE_UPLOAD_ROLES:
        attempted = [ok for (r, _), ok in results.items() if r == role]
        sent = sum(attempted)
        if not attempted:
            status = "not_configured"
            message = f"{label} is not configured yet."
        elif sent == len(attempted):
            status = "sent"
            message = f"WhatsApp sent to {label.lower()}."
        elif sent:
            status = "partial"
            message = f"WhatsApp reached {sent} of {len(attempted)} {label.lower()}s."
        else:
            status = "failed"
            message = f"WhatsApp to {label.lower()} could not be delivered."
        notifications.append(
            {"role": role, "label": label, "status": status, "sent": sent, "message": message}
        )

    svc._log(f"[INVOICE-UPLOAD-WHATSAPP] outcome={notifications}")
    return {"notifications": notifications, "message": _summary(notifications)}
