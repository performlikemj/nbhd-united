"""Regression tests for FA-0029: async_payment_succeeded subscription leg was dropped.

Before the fix, checkout.session.async_payment_succeeded for a subscription session
(mode='subscription', kind != 'credit_topup') fell into an else-branch that only
logged and never called handle_checkout_completed.  Tenants paying via ACH / SEPA /
bank-debit would never be provisioned.

After the fix, the else-branch calls handle_checkout_completed for all non-credit
sessions regardless of which of the two matched event types fired.
"""

from unittest.mock import patch

from django.test import RequestFactory, TestCase


class AsyncPaymentSucceededSubscriptionTest(TestCase):
    """FA-0029: async_payment_succeeded for a subscription session must provision."""

    def _make_stripe_event(self, event_type, mode, kind=None):
        """Build a minimal fake Stripe event dict."""
        meta = {}
        if kind:
            meta["kind"] = kind
        session_data = {
            "id": "cs_test_abc",
            "mode": mode,
            "metadata": meta,
            "customer": "cus_test",
            "subscription": "sub_test",
        }
        return {
            "type": event_type,
            "id": f"evt_{event_type}",
            "data": {"object": session_data},
        }

    def _post_fake_webhook(self, event):
        """Invoke stripe_webhook with a pre-verified fake event."""
        from apps.billing.views import stripe_webhook

        factory = RequestFactory()
        request = factory.post(
            "/api/billing/webhook/",
            data=b"{}",
            content_type="application/json",
        )
        request.META["HTTP_STRIPE_SIGNATURE"] = "fake"

        # Bypass signature verification and RLS middleware
        with (
            patch("stripe.Webhook.construct_event", return_value=event),
            patch("apps.tenants.middleware.set_rls_context"),
        ):
            return stripe_webhook(request)

    def test_async_payment_succeeded_subscription_calls_handle_checkout_completed(self):
        """async_payment_succeeded for a subscription session must call handle_checkout_completed."""
        event = self._make_stripe_event(
            "checkout.session.async_payment_succeeded",
            mode="subscription",
        )
        with (
            patch("apps.billing.views.handle_checkout_completed") as mock_hcc,
            patch("apps.billing.views.handle_credit_topup_completed") as mock_topup,
        ):
            response = self._post_fake_webhook(event)

        self.assertEqual(response.status_code, 200)
        mock_hcc.assert_called_once()
        mock_topup.assert_not_called()
        # Verify the correct session data dict was passed
        passed_data = mock_hcc.call_args[0][0]
        self.assertEqual(passed_data["mode"], "subscription")

    def test_completed_subscription_still_calls_handle_checkout_completed(self):
        """checkout.session.completed for a subscription must still call handle_checkout_completed."""
        event = self._make_stripe_event(
            "checkout.session.completed",
            mode="subscription",
        )
        with (
            patch("apps.billing.views.handle_checkout_completed") as mock_hcc,
            patch("apps.billing.views.handle_credit_topup_completed") as mock_topup,
        ):
            response = self._post_fake_webhook(event)

        self.assertEqual(response.status_code, 200)
        mock_hcc.assert_called_once()
        mock_topup.assert_not_called()

    def test_async_payment_succeeded_credit_topup_calls_credit_handler(self):
        """async_payment_succeeded for a credit top-up must still call handle_credit_topup_completed."""
        event = self._make_stripe_event(
            "checkout.session.async_payment_succeeded",
            mode="payment",
            kind="credit_topup",
        )
        with (
            patch("apps.billing.views.handle_checkout_completed") as mock_hcc,
            patch("apps.billing.views.handle_credit_topup_completed") as mock_topup,
        ):
            response = self._post_fake_webhook(event)

        self.assertEqual(response.status_code, 200)
        mock_topup.assert_called_once()
        mock_hcc.assert_not_called()

    def test_completed_credit_topup_calls_credit_handler(self):
        """checkout.session.completed for a credit top-up must call handle_credit_topup_completed."""
        event = self._make_stripe_event(
            "checkout.session.completed",
            mode="payment",
            kind="credit_topup",
        )
        with (
            patch("apps.billing.views.handle_checkout_completed") as mock_hcc,
            patch("apps.billing.views.handle_credit_topup_completed") as mock_topup,
        ):
            response = self._post_fake_webhook(event)

        self.assertEqual(response.status_code, 200)
        mock_topup.assert_called_once()
        mock_hcc.assert_not_called()
