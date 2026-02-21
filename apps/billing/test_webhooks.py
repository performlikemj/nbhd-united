"""Billing webhook view tests."""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework_simplejwt.tokens import RefreshToken

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
    STRIPE_PRICE_IDS={"starter": "price_starter_test", "premium": "price_premium_test"},
    ENABLED_STRIPE_TIERS=["starter", "premium"],
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
            {"tier": "premium"},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 200)
        mock_session_create.assert_called_once()
        call_kwargs = mock_session_create.call_args[1]
        self.assertEqual(call_kwargs["metadata"]["tier"], "premium")
        self.assertEqual(call_kwargs["metadata"]["user_id"], str(self.user.id))
        self.assertEqual(call_kwargs["api_key"], "sk_test_checkout")

    @override_settings(ENABLED_STRIPE_TIERS=["starter"])
    @patch("apps.billing.views.stripe.checkout.Session.create")
    def test_checkout_rejects_disabled_tier(self, mock_session_create):
        response = self.client.post(
            "/api/v1/billing/checkout/",
            {"tier": "premium"},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 400)
        mock_session_create.assert_not_called()
        self.assertIn("temporarily unavailable", response.json()["detail"])

    @override_settings(ENABLED_STRIPE_TIERS=[])
    @patch("apps.billing.views.stripe.checkout.Session.create")
    def test_checkout_disabled_globally(self, mock_session_create):
        response = self.client.post(
            "/api/v1/billing/checkout/",
            {"tier": "starter"},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 503)
        mock_session_create.assert_not_called()
        self.assertIn("Billing is temporarily disabled", response.json()["detail"])

    @override_settings(STRIPE_TEST_SECRET_KEY="")
    @patch("apps.billing.views.stripe.checkout.Session.create")
    def test_checkout_returns_503_when_stripe_not_configured(self, mock_session_create):
        response = self.client.post(
            "/api/v1/billing/checkout/",
            {"tier": "premium"},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 503)
        mock_session_create.assert_not_called()


class StripePortalGatingTest(TestCase):
    """Billing should stay offline until launch re-opens."""

    @override_settings(
        ENABLED_STRIPE_TIERS=[],
        STRIPE_TEST_SECRET_KEY="sk_test_portal",
    )
    @patch("apps.billing.views.stripe.billing_portal.Session.create")
    def test_portal_is_blocked_while_billing_disabled(self, mock_portal_create):
        tenant = create_tenant(display_name="Portal Disabled", telegram_chat_id=607)
        tenant.stripe_customer_id = "cus_portal_321"
        tenant.save(update_fields=["stripe_customer_id", "updated_at"])
        user = tenant.user
        refresh = RefreshToken.for_user(user)

        response = self.client.post(
            "/api/v1/billing/portal/",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}",
        )

        self.assertEqual(response.status_code, 503)
        mock_portal_create.assert_not_called()
        self.assertIn("temporarily disabled", response.json()["detail"])



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
