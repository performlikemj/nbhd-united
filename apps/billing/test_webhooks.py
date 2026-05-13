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

    @patch("apps.billing.views.handle_checkout_completed")
    @patch("apps.billing.views.stripe.Webhook.construct_event")
    def test_stripeobject_is_coerced_to_plain_dict_before_dispatch(
        self,
        mock_construct,
        mock_handler,
    ):
        """Pin the 2026-05-13 prod-bug fix: stripe-py 15.x's StripeObject
        has a ``__getattr__`` that hijacks ``.get(...)`` (sends it through
        ``__getitem__`` → KeyError → AttributeError). All ``services.py``
        handlers use ``.get(...)`` throughout, so a real
        ``checkout.session.completed`` webhook would crash with
        ``AttributeError: get``. The fix coerces ``event["data"]["object"]``
        to a plain dict (recursive) before dispatching — this test asserts
        the handler receives a plain dict, not a StripeObject lookalike
        with the .get bug.
        """
        import stripe

        # Construct a real-shaped CheckoutSession + nested customer object,
        # the way Stripe's construct_event returns them. The bug only
        # manifests on actual StripeObject instances; passing plain dicts
        # would silently pass the test even without the fix.
        session = stripe.checkout.Session.construct_from(
            {
                "id": "cs_test",
                "object": "checkout.session",
                "customer": "cus_test",
                "subscription": "sub_test",
                "metadata": {"user_id": "u-1", "tier": "starter"},
            },
            key=None,
        )

        # Sanity: the test fixture must actually be a StripeObject (or a
        # subclass like checkout.Session), not a plain dict — otherwise
        # this test is exercising the dict path and proves nothing.
        # Using `type(...) is dict` because StripeObject INHERITS from
        # dict, so `isinstance(..., dict)` would pass for both. Don't
        # assert hasattr(session, "to_dict_recursive") — that helper
        # exists in stripe-py 14.x but was removed in 15.x; the fix
        # tolerates either via the helper's hasattr fallback path.
        from apps.billing.views import _stripe_object_to_plain_dict

        self.assertIsNot(type(session), dict)
        self.assertEqual(type(session).__name__, "Session")

        mock_construct.return_value = {
            "type": "checkout.session.completed",
            "data": {"object": session},
        }

        response = self.client.post(
            "/api/v1/billing/webhook/",
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )

        self.assertEqual(response.status_code, 200)
        mock_handler.assert_called_once()
        passed = mock_handler.call_args.args[0]
        # The handler must receive a vanilla dict — no StripeObject leaves
        # anywhere reachable via .get(...).
        self.assertIs(type(passed), dict)
        self.assertIs(type(passed["metadata"]), dict)
        # And the value must round-trip the meaningful fields.
        self.assertEqual(passed["customer"], "cus_test")
        self.assertEqual(passed["metadata"]["user_id"], "u-1")

        # Helper sanity — pure-Python unit assertion alongside the integration.
        coerced = _stripe_object_to_plain_dict(session)
        self.assertIs(type(coerced), dict)
        # .get() must work on the result (the original bug class).
        self.assertEqual(coerced.get("metadata", {}).get("user_id"), "u-1")


@override_settings(
    STRIPE_PRICE_ID="price_starter_test",
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
    def test_checkout_includes_starter_tier_in_metadata(self, mock_session_create):
        mock_session_create.return_value = MagicMock(url="https://checkout.stripe.com/test")

        response = self.client.post(
            "/api/v1/billing/checkout/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 200)
        mock_session_create.assert_called_once()
        call_kwargs = mock_session_create.call_args[1]
        self.assertEqual(call_kwargs["metadata"]["tier"], "starter")
        self.assertEqual(call_kwargs["metadata"]["user_id"], str(self.user.id))
        self.assertEqual(call_kwargs["api_key"], "sk_test_checkout")

    @override_settings(STRIPE_PRICE_ID="")
    @patch("apps.billing.views.stripe.checkout.Session.create")
    def test_checkout_disabled_when_no_price_configured(self, mock_session_create):
        response = self.client.post(
            "/api/v1/billing/checkout/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 503)
        mock_session_create.assert_not_called()

    @override_settings(STRIPE_TEST_SECRET_KEY="")
    @patch("apps.billing.views.stripe.checkout.Session.create")
    def test_checkout_returns_503_when_stripe_not_configured(self, mock_session_create):
        response = self.client.post(
            "/api/v1/billing/checkout/",
            {},
            content_type="application/json",
            HTTP_AUTHORIZATION=self.auth_header,
        )

        self.assertEqual(response.status_code, 503)
        mock_session_create.assert_not_called()


class StripePortalGatingTest(TestCase):
    """Billing should stay offline until launch re-opens."""

    @override_settings(
        STRIPE_PRICE_ID="",
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


@override_settings(
    STRIPE_TEST_SECRET_KEY="sk_test_portal",
    STRIPE_PRICE_ID="price_starter_test",
)
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
