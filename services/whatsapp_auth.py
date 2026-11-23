"""Service-to-service auth for WhatsApp / bot integrations."""

from fastapi import Header, HTTPException, status

from config import settings


def require_whatsapp_api_key(
    x_whatsapp_api_key: str | None = Header(None, alias="X-WhatsApp-API-Key"),
) -> None:
    expected = (settings.whatsapp_api_key or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp integration is not configured (WHATSAPP_API_KEY)",
        )
    if not x_whatsapp_api_key or x_whatsapp_api_key.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-WhatsApp-API-Key",
        )
