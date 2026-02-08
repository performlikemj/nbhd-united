"""Billing webhook view tests."""
from unittest.mock import patch

from django.test import TestCase, override_settings


@override_settings(DJSTRIPE_WEBHOOK_SECRET="whsec_test")
class StripeWebhookViewTest(TestCase):
    @patch("apps.billing.views.handle_checkout_completed")
    @patch("apps.billing.views.stripe.Webhook.construct_event")
    def test_checkout_session_event_dispatches(self, mock_construct, mock_handler):
        mock_construct.return_value = {
            "type": "checkout.session.completed",
            "data": {"object": {"id": "cs_test"}},
        }

        response = self.client.post(
            "/api/v1/billing/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )

        self.assertEqual(response.status_code, 200)
        mock_handler.assert_called_once_with({"id": "cs_test"})

    @patch("apps.billing.views.handle_invoice_payment_failed")
    @patch("apps.billing.views.stripe.Webhook.construct_event")
    def test_invoice_payment_failed_event_dispatches(self, mock_construct, mock_handler):
        mock_construct.return_value = {
            "type": "invoice.payment_failed",
            "data": {"object": {"id": "in_test"}},
        }

        response = self.client.post(
            "/api/v1/billing/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )

        self.assertEqual(response.status_code, 200)
        mock_handler.assert_called_once_with({"id": "in_test"})

    @patch("apps.billing.views.stripe.Webhook.construct_event")
    def test_invalid_signature_returns_400(self, mock_construct):
        mock_construct.side_effect = ValueError("bad payload")

        response = self.client.post(
            "/api/v1/billing/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="invalid",
        )

        self.assertEqual(response.status_code, 400)
