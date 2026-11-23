"""Backward-compatible re-exports — use ``services.whatsapp_notify_service`` directly."""

from services.whatsapp_notify_service import (
    build_grn_page_url,
    notify_po_created_whatsapp,
)

__all__ = ["build_grn_page_url", "notify_po_created_whatsapp"]
