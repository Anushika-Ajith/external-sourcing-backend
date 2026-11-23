import threading
import unittest
from unittest import mock

import requests

from services import invoice_upload_notify as notify


def _lookup_response(entries):
    resp = mock.Mock(ok=True, status_code=200)
    resp.json.return_value = entries
    return resp


def _entry(role, phones=(), without_phone=0):
    return {
        "role": role,
        "recipients": [
            {"userId": i + 1, "name": f"{role} {i}", "phoneNumber": p}
            for i, p in enumerate(phones)
        ],
        "usersWithoutPhone": without_phone,
    }


class FakeService:
    def __init__(self, ok=True, barrier=None):
        self.ok = ok
        self.barrier = barrier
        self.sent_to = []
        self.lock = threading.Lock()

    def notify_with_fallback(self, **kwargs):
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        with self.lock:
            self.sent_to.append(kwargs["to"])
        return {"ok": self.ok, "sent": self.ok}

    def _log(self, _message):
        pass


class NotifyInvoiceUploadedTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(notify.settings, "nestjs_api_url", "http://foundation:3000")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, entries, service, **kwargs):
        with mock.patch.object(notify.requests, "get", return_value=_lookup_response(entries)) as get:
            outcome = notify.notify_invoice_uploaded(
                bearer_token="Bearer t",
                invoice_no="INV-1",
                po_number="PO-9",
                vendor_name="Acme",
                line_count=2,
                service=service,
                **kwargs,
            )
        return outcome, get

    def _status(self, outcome):
        return {n["role"]: n["status"] for n in outcome["notifications"]}

    def test_buyer_and_store_keeper_are_messaged_at_the_same_time(self):
        # Each send waits for the other: a sequential implementation would time out here.
        svc = FakeService(barrier=threading.Barrier(2))
        outcome, get = self._run(
            [_entry("BUYER", ["9000000001"]), _entry("STOREKEEPER", ["9000000002"])], svc
        )

        self.assertEqual(self._status(outcome), {"BUYER": "sent", "STOREKEEPER": "sent"})
        self.assertCountEqual(svc.sent_to, ["919000000001", "919000000002"])
        self.assertEqual(outcome["message"], "WhatsApp sent to buyer and store keeper.")
        self.assertEqual(get.call_args.kwargs["params"], {"roles": "BUYER,STOREKEEPER"})
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": "Bearer t"})

    def test_missing_store_keeper_is_reported_not_configured(self):
        svc = FakeService()
        outcome, _ = self._run([_entry("BUYER", ["9000000001"]), _entry("STOREKEEPER")], svc)

        self.assertEqual(
            self._status(outcome), {"BUYER": "sent", "STOREKEEPER": "not_configured"}
        )
        self.assertEqual(svc.sent_to, ["919000000001"])
        self.assertEqual(
            outcome["message"],
            "WhatsApp sent to buyer. Store keeper is not configured yet.",
        )

    def test_neither_configured(self):
        svc = FakeService()
        outcome, _ = self._run([_entry("BUYER"), _entry("STOREKEEPER")], svc)

        self.assertEqual(
            self._status(outcome), {"BUYER": "not_configured", "STOREKEEPER": "not_configured"}
        )
        self.assertEqual(svc.sent_to, [])
        self.assertEqual(
            outcome["message"],
            "Buyer is not configured yet. Store keeper is not configured yet.",
        )

    def test_role_holder_without_phone_counts_as_not_configured(self):
        svc = FakeService()
        outcome, _ = self._run(
            [_entry("BUYER", without_phone=1), _entry("STOREKEEPER", ["9000000002"])], svc
        )
        self.assertEqual(self._status(outcome)["BUYER"], "not_configured")

    def test_every_holder_of_a_role_is_messaged(self):
        svc = FakeService()
        outcome, _ = self._run(
            [_entry("BUYER", ["9000000001", "9000000003"]), _entry("STOREKEEPER", ["9000000002"])],
            svc,
        )
        self.assertEqual(len(svc.sent_to), 3)
        self.assertEqual(outcome["notifications"][0]["sent"], 2)

    def test_delivery_failure_is_reported(self):
        svc = FakeService(ok=False)
        outcome, _ = self._run(
            [_entry("BUYER", ["9000000001"]), _entry("STOREKEEPER", ["9000000002"])], svc
        )
        self.assertEqual(self._status(outcome), {"BUYER": "failed", "STOREKEEPER": "failed"})

    def test_lookup_failure_never_raises(self):
        svc = FakeService()
        with mock.patch.object(
            notify.requests, "get", side_effect=requests.ConnectionError("down")
        ):
            outcome = notify.notify_invoice_uploaded(bearer_token="Bearer t", service=svc)

        self.assertEqual(self._status(outcome), {"BUYER": "failed", "STOREKEEPER": "failed"})
        self.assertEqual(svc.sent_to, [])

    def test_message_text(self):
        text = notify.build_invoice_uploaded_message(
            invoice_no="INV-1", po_number="PO-9", vendor_name="Acme", line_count=2
        )
        self.assertEqual(
            text,
            "📄 New invoice uploaded\nInvoice: INV-1\nPO Number: PO-9\nVendor: Acme\nLine items: 2",
        )


if __name__ == "__main__":
    unittest.main()
