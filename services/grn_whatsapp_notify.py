"""Backward-compatible re-exports — use ``services.whatsapp_notify_service`` directly."""

from services.whatsapp_notify_service import (
    build_po_page_url,
    notify_grn_created_whatsapp,
)

__all__ = ["build_po_page_url", "notify_grn_created_whatsapp"]
