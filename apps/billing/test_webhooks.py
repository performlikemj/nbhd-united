"""Billing webhook view tests."""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.models import User
from apps.tenants.services import create_tenant


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


@override_settings(
    STRIPE_PRICE_IDS={"plus": "price_plus_test"},
    STRIPE_TEST_SECRET_KEY="sk_test_checkout",
)
class StripeCheckoutViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Checkout", telegram_chat_id=600)
        self.user = self.tenant.user
        self.user.email = "checkout@example.com"
        self.user.save()
        refresh = RefreshToken.for_user(self.user)
        self.auth_header = f"Bearer {refresh.access_token}"

    @patch("apps.billing.views.stripe.checkout.Session.create")
    def test_checkout_includes_tier_in_metadata(self, mock_session_create):
        mock_session_create.return_value = MagicMock(url="https://checkout.stripe.com/test")

        response = self.client.post(
            "/api/v1/billing/checkout/",
            {"tier": "plus"},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 200)
        mock_session_create.assert_called_once()
        call_kwargs = mock_session_create.call_args[1]
        self.assertEqual(call_kwargs["metadata"]["tier"], "plus")
        self.assertEqual(call_kwargs["metadata"]["user_id"], str(self.user.id))
        self.assertEqual(call_kwargs["api_key"], "sk_test_checkout")

    @override_settings(STRIPE_TEST_SECRET_KEY="")
    @patch("apps.billing.views.stripe.checkout.Session.create")
    def test_checkout_returns_503_when_stripe_not_configured(self, mock_session_create):
        response = self.client.post(
            "/api/v1/billing/checkout/",
            {"tier": "plus"},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 503)
        mock_session_create.assert_not_called()


@override_settings(STRIPE_TEST_SECRET_KEY="sk_test_portal")
class StripePortalViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Portal", telegram_chat_id=601)
        self.tenant.stripe_customer_id = "cus_portal_123"
        self.tenant.save(update_fields=["stripe_customer_id", "updated_at"])
        self.user = self.tenant.user
        refresh = RefreshToken.for_user(self.user)
        self.auth_header = f"Bearer {refresh.access_token}"

    @patch("apps.billing.views.stripe.billing_portal.Session.create")
    def test_portal_uses_configured_api_key(self, mock_portal_create):
        mock_portal_create.return_value = MagicMock(url="https://billing.stripe.com/p/session")

        response = self.client.post(
            "/api/v1/billing/portal/",
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 200)
        mock_portal_create.assert_called_once()
        call_kwargs = mock_portal_create.call_args[1]
        self.assertEqual(call_kwargs["customer"], "cus_portal_123")
        self.assertEqual(call_kwargs["api_key"], "sk_test_portal")

    @override_settings(STRIPE_TEST_SECRET_KEY="")
    @patch("apps.billing.views.stripe.billing_portal.Session.create")
    def test_portal_returns_503_when_stripe_not_configured(self, mock_portal_create):
        response = self.client.post(
            "/api/v1/billing/portal/",
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 503)
        mock_portal_create.assert_not_called()
