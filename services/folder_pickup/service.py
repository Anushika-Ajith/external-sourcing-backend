"""Lifecycle helpers for the folder pickup background service."""

from __future__ import annotations

import logging
from pathlib import Path

from config import settings
from services.folder_pickup.document_path_client import resolve_document_upload_folder
from services.folder_pickup.watcher import start_folder_pickup, stop_folder_pickup

logger = logging.getLogger(__name__)


def start_folder_pickup_service() -> None:
    if not settings.document_pickup_enabled:
        logger.info("[folder-pickup] Disabled (DOCUMENT_PICKUP_ENABLED=false)")
        return

    if settings.document_pickup_vendor_id is None:
        logger.warning(
            "[folder-pickup] Disabled: DOCUMENT_PICKUP_VENDOR_ID is not configured"
        )
        return

    try:
        folder = resolve_document_upload_folder()
    except Exception:
        logger.exception("[folder-pickup] Could not resolve document upload folder")
        return

    if not folder:
        logger.info(
            "[folder-pickup] Disabled: no document folder resolved "
            "(set DOCUMENT_UPLOAD_FOLDER or configure Foundation API lookup)"
        )
        return

    inbox = Path(folder).expanduser().resolve()
    try:
        start_folder_pickup(inbox)
    except Exception:
        logger.exception("[folder-pickup] Failed to start service for %s", inbox)


def stop_folder_pickup_service() -> None:
    try:
        stop_folder_pickup()
    except Exception:
        logger.exception("[folder-pickup] Failed to stop service")
