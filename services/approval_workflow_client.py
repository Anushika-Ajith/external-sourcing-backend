"""Server-side gate on the foundation approval workflow.

The browser decides *when* to call the vendor Approve endpoints, but it must
not decide *whether* the vendor may be activated. These helpers ask foundation
for the workflow state and only let activation through when the workflow says
so. Reject is intentionally not covered here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from fastapi import HTTPException, status

from config import settings
from services.object_reservation_client import _auth_headers, _nestjs_base

logger = logging.getLogger(__name__)

# Workflow statuses that allow the vendor to be activated.
#   completed      -> every required approver has approved
#   not_configured -> no approval workflow exists for this document type
ACTIVATION_ALLOWED_STATUSES = frozenset({"completed", "not_configured"})


def vendor_registration_document_type_code() -> str:
    return (
        getattr(settings, "vendor_registration_document_type_code", None)
        or "VENDOR_REG"
    ).strip()


def fetch_workflow_state(
    *,
    document_no: str,
    document_type_code: str,
    bearer_token: str,
) -> dict[str, Any]:
    """GET /approval-workflow/state. Raises HTTP 503 if foundation can't answer."""
    base = _nestjs_base()
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The approval service is not configured. Please try again later.",
        )
    if not (bearer_token or "").strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please log in again.",
        )

    try:
        resp = requests.get(
            f"{base}/approval-workflow/state",
            headers=_auth_headers(bearer_token),
            params={
                "documentNo": document_no,
                "documentTypeCode": document_type_code,
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        logger.warning("approval-workflow/state request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not check the approval status right now. Please try again.",
        ) from exc

    if resp.status_code in (401, 403):
        raise HTTPException(
            status_code=resp.status_code,
            detail="You are not allowed to check the approval status for this vendor.",
        )
    if resp.status_code >= 400:
        logger.warning(
            "approval-workflow/state status=%s body=%s",
            resp.status_code,
            (resp.text or "")[:300],
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not check the approval status right now. Please try again.",
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The approval service returned an unreadable response.",
        ) from exc
    return body if isinstance(body, dict) else {}


def _pending_names(state: dict[str, Any]) -> list[str]:
    """Names of the approvers the workflow is waiting on (foundation nests them under ``user``)."""
    names: list[str] = []
    for member in state.get("nextApprovers") or []:
        if not isinstance(member, dict):
            continue
        user = member.get("user") if isinstance(member.get("user"), dict) else {}
        label = (
            (user.get("name") or "").strip()
            or (user.get("email") or "").strip()
            or (f"User #{member['userId']}" if member.get("userId") is not None else "")
        )
        if label and label not in names:
            names.append(label)
    return names


def blocked_message(state: dict[str, Any]) -> Optional[str]:
    """Friendly reason the vendor can't be activated, or None if it can."""
    wf = str(state.get("workflowStatus") or "").lower()
    if wf in ACTIVATION_ALLOWED_STATUSES:
        return None
    if wf == "in_progress":
        pending = _pending_names(state)
        if pending:
            return "Approval is still pending from " + ", ".join(pending) + "."
        return "Approval is still pending from other approvers."
    if wf == "draft":
        return "This vendor registration has not been submitted for approval yet."
    if wf == "rejected":
        return "This vendor registration was rejected and must be resubmitted."
    if wf == "misconfigured":
        reason = state.get("notConfiguredReason")
        return (
            f"The approval workflow is not set up correctly: {reason}"
            if reason
            else "The approval workflow is not set up correctly. Please contact an administrator."
        )
    return "This vendor registration is not approved yet."


# Only a submitted registration may be activated. Buyer drafts use DRAFT;
# legacy "pending" rows must also be submitted first (complete-registration).
APPROVABLE_VENDOR_STATUSES = frozenset({"SUBMITTED"})


def vendor_status_block_message(vendor_status: Optional[str]) -> Optional[str]:
    """Why a vendor in this status can't be approved, or None if it can."""
    current = (vendor_status or "").strip().upper()
    if current in APPROVABLE_VENDOR_STATUSES:
        return None
    if current in ("DRAFT", "PENDING"):
        return "This vendor registration is still a draft and has not been submitted."
    if current in ("PENDING_VERIFICATION", "OTP_VERIFIED", ""):
        return (
            "This vendor has not finished and submitted the registration form yet, "
            "so it can't be approved."
        )
    if current == "REJECTED":
        return "This vendor registration was rejected and must be resubmitted before it can be approved."
    return f"A vendor with status \"{vendor_status}\" can't be approved."


def assert_vendor_activation_allowed(
    *,
    vendor_id: int,
    vendor_status: Optional[str],
    bearer_token: str,
) -> None:
    """
    Raise unless the vendor may move to ACTIVE/APPROVED.

    Fails closed: if foundation can't be reached the vendor is NOT activated.
    """
    status_message = vendor_status_block_message(vendor_status)
    if status_message:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=status_message)

    state = fetch_workflow_state(
        document_no=str(int(vendor_id)),
        document_type_code=vendor_registration_document_type_code(),
        bearer_token=bearer_token,
    )
    message = blocked_message(state)
    if message:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)
