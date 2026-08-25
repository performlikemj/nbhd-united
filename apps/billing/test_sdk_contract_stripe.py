"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

import stripe
from django.test import SimpleTestCase


class StripeSdkContractTest(SimpleTestCase):
    def test_webhook_and_error_paths_exist(self):
        inspect.signature(stripe.Webhook.construct_event).bind(b"{}", "signature", "secret")

        self.assertTrue(issubclass(stripe.error.StripeError, Exception))
        self.assertTrue(issubclass(stripe.error.SignatureVerificationError, stripe.error.StripeError))

    def test_session_create_signatures_accept_our_kwargs(self):
        inspect.signature(stripe.billing_portal.Session.create).bind(
            customer="cus_123",
            return_url="https://example.test/billing",
            api_key="sk_test_offline",
        )
        inspect.signature(stripe.checkout.Session.create).bind(
            customer_email="person@example.test",
            line_items=[{"price": "price_123", "quantity": 1}],
            mode="subscription",
            success_url="https://example.test/success",
            cancel_url="https://example.test/cancel",
            metadata={"tenant_id": "tenant"},
            consent_collection={"terms_of_service": "required"},
            custom_text={"terms_of_service_acceptance": {"message": "Terms"}},
            api_key="sk_test_offline",
        )

    def test_retrieve_and_modify_signatures_accept_our_calls(self):
        inspect.signature(stripe.checkout.Session.retrieve).bind(
            "cs_123", expand=["line_items.data.price"], api_key="sk_test_offline"
        )
        inspect.signature(stripe.Subscription.retrieve).bind(
            "sub_123", expand=["items.data.price"], api_key="sk_test_offline"
        )
        inspect.signature(stripe.Subscription.modify).bind("sub_123", cancel_at_period_end=True)
        inspect.signature(stripe.Charge.retrieve).bind("ch_123", api_key="sk_test_offline")

    def test_stripe_objects_keep_mapping_and_to_dict_shapes(self):
        session = stripe.checkout.Session.construct_from(
            {"id": "cs_123", "url": "https://checkout.test/session"},
            key="sk_test_offline",
        )

        self.assertEqual(session["id"], "cs_123")
        self.assertEqual(session.url, "https://checkout.test/session")
        self.assertEqual(session.to_dict()["id"], "cs_123")
