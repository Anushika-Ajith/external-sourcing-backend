"""Unit tests for WhatsApp notify service."""

from __future__ import annotations

import unittest
from unittest import mock

from services.whatsapp_notify_service import (
    WhatsAppNotifyService,
    notify_po_created_whatsapp,
    notify_po_handoff_whatsapp,
    append_page_link_to_message,
    extract_http_urls,
    to_whatsapp_http_url,
    _build_template_body_params,
    normalize_whatsapp_phone,
    resolve_handoff_recipient,
)


class TestWhatsAppNotifyService(unittest.TestCase):
    @mock.patch("services.whatsapp_notify_service.requests.post")
    def test_post_backend_success(self, mock_post):
        mock_resp = mock.Mock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.content = b'{"sent": true}'
        mock_resp.json.return_value = {"sent": True}
        mock_post.return_value = mock_resp

        svc = WhatsAppNotifyService(log_prefix="TEST")
        with mock.patch.object(svc, "backend_base_url", return_value="http://wa:4000"):
            result = svc.post_backend("/po-notify", {"poNumber": "PO1"})

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("sent"))
        mock_post.assert_called_once_with(
            "http://wa:4000/po-notify",
            json={"poNumber": "PO1"},
            timeout=15,
        )

    @mock.patch.object(WhatsAppNotifyService, "send_text_meta")
    @mock.patch.object(WhatsAppNotifyService, "post_backend")
    def test_notify_with_fallback_uses_meta_when_backend_fails(
        self,
        mock_post,
        mock_meta,
    ):
        mock_post.return_value = None
        mock_meta.return_value = {"ok": True, "sent": True, "via": "meta-direct"}

        svc = WhatsAppNotifyService(log_prefix="TEST")
        result = svc.notify_with_fallback(
            endpoint="/po-notify",
            payload={"poNumber": "PO1"},
            fallback_text="hello",
            to="919999999999",
        )
        self.assertTrue(result.get("sent"))
        mock_meta.assert_called_once_with(to="919999999999", text="hello")

    @mock.patch.object(WhatsAppNotifyService, "notify_with_fallback")
    def test_notify_po_created_builds_payload(self, mock_notify):
        mock_notify.return_value = {"ok": True, "sent": True}
        svc = WhatsAppNotifyService(log_prefix="PO-WHATSAPP")

        notify_po_created_whatsapp(
            po_number="PO-100",
            vendor_name="Acme",
            line_count=3,
            service=svc,
        )

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        self.assertEqual(kwargs["endpoint"], "/po-notify")
        self.assertEqual(kwargs["payload"]["poNumber"], "PO-100")
        self.assertEqual(kwargs["payload"]["vendorName"], "Acme")
        self.assertEqual(kwargs["payload"]["lineCount"], 3)
        self.assertIn("grnPageUrl", kwargs["payload"])


class TestPoHandoffNotify(unittest.TestCase):
    def test_normalize_whatsapp_phone_indian_ten_digit(self):
        self.assertEqual(normalize_whatsapp_phone("8086076193"), "918086076193")

    def test_resolve_vendor_handoff_uses_registration_mobile(self):
        recipient = resolve_handoff_recipient(
            "VENDOR",
            vendor_mobile="8086076193",
        )
        self.assertEqual(recipient, "918086076193")

    @mock.patch.object(WhatsAppNotifyService, "notify_with_fallback")
    def test_notify_po_handoff_posts_to_backend(self, mock_notify):
        mock_notify.return_value = {"ok": True, "sent": True}
        svc = WhatsAppNotifyService(log_prefix="PO-HANDOFF-WHATSAPP")

        notify_po_handoff_whatsapp(
            handoff_to="VENDOR",
            po_number="PO00031",
            po_id=22,
            vendor_name="Metro Office",
            vendor_mobile="8086076193",
            service=svc,
        )

        mock_notify.assert_called_once()
        kwargs = mock_notify.call_args.kwargs
        self.assertEqual(kwargs["endpoint"], "/po-handoff-notify")
        self.assertEqual(kwargs["payload"]["handoffTo"], "VENDOR")
        self.assertEqual(kwargs["payload"]["poNumber"], "PO00031")
        self.assertEqual(kwargs["payload"]["poId"], 22)
        self.assertEqual(kwargs["payload"]["to"], "918086076193")
        self.assertEqual(kwargs["template_body_params"], ["PO00031"])
        self.assertEqual(kwargs["template_url_button_param"], "22")


class TestPoTemplateBodyParams(unittest.TestCase):
    def test_single_param_uses_po_number_from_field(self):
        params = _build_template_body_params("", param_count=1, po_number="PO00033")
        self.assertEqual(params, ["PO00033"])

    def test_single_param_extracts_po_from_message(self):
        text = "✅ New Purchase Order created\nPO Number: PO00033\nVendor: Acme"
        params = _build_template_body_params(text, param_count=1)
        self.assertEqual(params, ["PO00033"])


class TestWhatsAppMessageLinks(unittest.TestCase):
    def test_to_whatsapp_http_url_preserves_https_on_production_domain(self):
        self.assertEqual(
            to_whatsapp_http_url("https://po.odintech.co.in/vendor/purchase-orders/22"),
            "https://po.odintech.co.in/vendor/purchase-orders/22",
        )

    def test_to_whatsapp_http_url_downgrades_https(self):
        self.assertEqual(
            to_whatsapp_http_url(
                "https://ec2.example.com:3002/vendor/purchase-orders/1"
            ),
            "http://ec2.example.com:3002/vendor/purchase-orders/1",
        )

    def test_append_page_link_converts_https(self):
        msg = append_page_link_to_message(
            "Hello",
            "https://ec2.example.com:3002/vendor/purchase-orders/1",
            label="Open PO",
        )
        self.assertIn("http://ec2.example.com:3002/vendor/purchase-orders/1", msg)
        self.assertNotIn("https://", msg)

    def test_append_page_link_adds_url_when_missing(self):
        msg = append_page_link_to_message(
            "Hello",
            "http://localhost:3002/vendor/invoice-review/PO-1",
            label="Review invoice",
        )
        self.assertIn("Review invoice:", msg)
        self.assertIn("http://localhost:3002/vendor/invoice-review/PO-1", msg)

    def test_append_page_link_skips_duplicate_url(self):
        url = "http://localhost:3002/vendor/add-grn?po=PO-1"
        msg = append_page_link_to_message(f"See {url}", url)
        self.assertEqual(msg, f"See {url}")

    def test_extract_http_urls(self):
        urls = extract_http_urls("Open http://a.test/x and https://b.test/y")
        self.assertEqual(urls, ["http://a.test/x", "https://b.test/y"])


class TestInvoiceNotifyMessage(unittest.TestCase):
    @mock.patch.object(WhatsAppNotifyService, "notify_with_fallback")
    def test_notify_invoice_includes_page_url(self, mock_notify):
        from services.whatsapp_notify_service import notify_invoice_confirmed_whatsapp

        mock_notify.return_value = {"ok": True, "sent": True}
        notify_invoice_confirmed_whatsapp(po_number="PO-55", invoice_no="INV-1")

        kwargs = mock_notify.call_args.kwargs
        self.assertIn("invoicePageUrl", kwargs["payload"])
        self.assertIn("/vendor/invoice-review/", kwargs["payload"]["invoicePageUrl"])
        self.assertIn("http", kwargs["fallback_text"])
        self.assertIn("/vendor/invoice-review/", kwargs["fallback_text"])


if __name__ == "__main__":
    unittest.main()
