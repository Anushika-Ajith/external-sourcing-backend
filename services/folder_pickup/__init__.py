"""Automatic document pickup from a configured local folder."""

from services.folder_pickup.service import (
    start_folder_pickup_service,
    stop_folder_pickup_service,
)

__all__ = ["start_folder_pickup_service", "stop_folder_pickup_service"]
