"""
Pydantic schemas for the Foundation/Auth handoff-token flow.

The handoff-token flow lets a trusted Foundation Auth API mint a short-lived,
one-time token that one of our frontends (e.g. the Vendor App) can hand off
to another frontend (e.g. the Internal Sourcing App) which then exchanges
the handoff token for a real, longer-lived access token.

NOTE: The handoff token MUST be created by the trusted backend, never by
frontends — the frontend only triggers creation while authenticated with
its existing access token.
"""

from typing import Optional, Literal

from pydantic import BaseModel, Field


# Target apps that we currently allow handoff for.
TargetApp = Literal["internal_sourcing", "vendor", "buyer"]


class CreateHandoffTokenRequest(BaseModel):
    """Request body for POST /auth/create-handoff-token."""

    target_app: TargetApp = Field(
        ...,
        description="The application the user wants to be handed off to.",
    )
    return_to: Optional[str] = Field(
        None,
        description=(
            "Optional intra-app path the bridge should redirect to after a "
            "successful exchange (e.g. '/pcd-approval')."
        ),
        max_length=512,
    )
    tenant_id: Optional[int] = Field(
        None,
        ge=1,
        description="Optional tenant context copied into the handoff JWT and exchanged access token.",
    )
    company_id: Optional[int] = Field(
        None,
        ge=1,
        description="Optional company context copied into the handoff JWT and exchanged access token.",
    )


class CreateHandoffTokenResponse(BaseModel):
    """Response body for POST /auth/create-handoff-token."""

    handoff_token: str
    target_app: TargetApp
    return_to: Optional[str] = None
    expires_in: int = Field(..., description="Lifetime of the handoff token in seconds.")


class ExchangeHandoffTokenRequest(BaseModel):
    """Request body for POST /auth/exchange-handoff-token."""

    handoff_token: str = Field(..., min_length=10)


class ExchangedUser(BaseModel):
    user_id: int
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    vendor_id: Optional[int] = None


class ExchangeHandoffTokenResponse(BaseModel):
    """Response body for POST /auth/exchange-handoff-token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    target_app: TargetApp
    return_to: Optional[str] = None
    tenant_id: Optional[int] = Field(
        None,
        description="Echo of tenant_id from the handoff token, if present.",
    )
    company_id: Optional[int] = Field(
        None,
        description="Echo of company_id from the handoff token, if present.",
    )
    user: ExchangedUser
