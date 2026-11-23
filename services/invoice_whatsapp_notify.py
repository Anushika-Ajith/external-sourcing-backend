"""Backward-compatible re-exports — use ``services.whatsapp_notify_service`` directly."""

from services.whatsapp_notify_service import (
    build_invoice_page_url,
    notify_invoice_confirmed_whatsapp,
)

__all__ = ["build_invoice_page_url", "notify_invoice_confirmed_whatsapp"]
