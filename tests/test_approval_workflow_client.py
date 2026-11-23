from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests
from fastapi import HTTPException

from services import approval_workflow_client as awc


def _resp(status_code=200, body=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body if body is not None else {}
    r.text = ""
    return r


def _allow(state, vendor_status="SUBMITTED"):
    with patch.object(awc.requests, "get", return_value=_resp(200, state)):
        awc.assert_vendor_activation_allowed(
            vendor_id=5, vendor_status=vendor_status, bearer_token="Bearer t"
        )


class ActivationGateTests(unittest.TestCase):
    def assert_blocked(self, state, contains, vendor_status="SUBMITTED"):
        with self.assertRaises(HTTPException) as ctx:
            _allow(state, vendor_status)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn(contains, ctx.exception.detail)

    def test_completed_workflow_allows(self):
        _allow({"workflowStatus": "completed"})

    def test_no_workflow_configured_allows(self):
        _allow({"workflowStatus": "not_configured"})

    def test_pending_approvers_block_and_are_named(self):
        self.assert_blocked(
            {
                "workflowStatus": "in_progress",
                "nextApprovers": [
                    {"userId": 1, "user": {"id": 1, "name": "Asha", "email": "a@x.com"}},
                    {"userId": 2, "user": {"id": 2, "name": "", "email": "b@x.com"}},
                    {"userId": 3, "user": None},
                    {"userId": 1, "user": {"id": 1, "name": "Asha"}},
                ],
            },
            "Asha, b@x.com, User #3.",
        )

    def test_in_progress_without_names_still_blocks(self):
        self.assert_blocked({"workflowStatus": "in_progress"}, "pending")

    def test_rejected_blocks(self):
        self.assert_blocked({"workflowStatus": "rejected"}, "rejected")

    def test_misconfigured_blocks_with_reason(self):
        self.assert_blocked(
            {"workflowStatus": "misconfigured", "notConfiguredReason": "no approvers"},
            "no approvers",
        )

    def test_draft_workflow_blocks(self):
        self.assert_blocked({"workflowStatus": "draft"}, "not been submitted")

    def test_vendor_that_never_submitted_is_blocked_without_calling_foundation(self):
        for vendor_status, expected in (
            ("pending_verification", "not finished"),
            ("otp_verified", "not finished"),
            (None, "not finished"),
            ("Rejected", "rejected"),
            ("inactive", "can't be approved"),
        ):
            with self.subTest(vendor_status=vendor_status):
                with patch.object(awc.requests, "get") as get:
                    with self.assertRaises(HTTPException) as ctx:
                        awc.assert_vendor_activation_allowed(
                            vendor_id=5, vendor_status=vendor_status, bearer_token="Bearer t"
                        )
                self.assertEqual(ctx.exception.status_code, 409)
                self.assertIn(expected, ctx.exception.detail)
                get.assert_not_called()

    def test_submitted_vendors_are_approvable(self):
        for vendor_status in ("SUBMITTED", "submitted"):
            with self.subTest(vendor_status=vendor_status):
                _allow({"workflowStatus": "completed"}, vendor_status)

    def test_legacy_pending_vendor_must_be_submitted_first(self):
        with patch.object(awc.requests, "get") as get:
            with self.assertRaises(HTTPException) as ctx:
                awc.assert_vendor_activation_allowed(
                    vendor_id=5, vendor_status="pending", bearer_token="Bearer t"
                )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("draft", ctx.exception.detail.lower())
        get.assert_not_called()

    def test_draft_vendor_blocks_without_calling_foundation(self):
        with patch.object(awc.requests, "get") as get:
            with self.assertRaises(HTTPException) as ctx:
                awc.assert_vendor_activation_allowed(
                    vendor_id=5, vendor_status="draft", bearer_token="Bearer t"
                )
        self.assertEqual(ctx.exception.status_code, 409)
        get.assert_not_called()

    def test_unknown_status_fails_closed(self):
        self.assert_blocked({"workflowStatus": "something_new"}, "not approved")
        self.assert_blocked({}, "not approved")

    def test_foundation_unreachable_fails_closed_with_503(self):
        with patch.object(awc.requests, "get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(HTTPException) as ctx:
                awc.assert_vendor_activation_allowed(
                    vendor_id=5, vendor_status="SUBMITTED", bearer_token="Bearer t"
                )
        self.assertEqual(ctx.exception.status_code, 503)

    def test_foundation_error_fails_closed_with_503(self):
        with patch.object(awc.requests, "get", return_value=_resp(500)):
            with self.assertRaises(HTTPException) as ctx:
                awc.assert_vendor_activation_allowed(
                    vendor_id=5, vendor_status="SUBMITTED", bearer_token="Bearer t"
                )
        self.assertEqual(ctx.exception.status_code, 503)

    def test_missing_token_is_401(self):
        with self.assertRaises(HTTPException) as ctx:
            awc.assert_vendor_activation_allowed(
                vendor_id=5, vendor_status="SUBMITTED", bearer_token=""
            )
        self.assertEqual(ctx.exception.status_code, 401)

    def test_asks_foundation_about_this_vendor_and_type(self):
        with patch.object(awc.requests, "get", return_value=_resp(200, {"workflowStatus": "completed"})) as get:
            awc.assert_vendor_activation_allowed(
                vendor_id=42, vendor_status="SUBMITTED", bearer_token="Bearer t"
            )
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["params"]["documentNo"], "42")
        self.assertEqual(kwargs["params"]["documentTypeCode"], "VENDOR_REG")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer t")


if __name__ == "__main__":
    unittest.main()
