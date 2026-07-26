from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.yardtalk.models import License


@override_settings(
    DJSTRIPE_WEBHOOK_SECRET="whsec_test",
    STRIPE_LIVE_MODE=False,
    STRIPE_TEST_SECRET_KEY="sk_test_placeholder",
)
class YardTalkRevocationWebhookTests(TestCase):
    def setUp(self):
        self.license = License.objects.create(
            key="YT-AAAA-BBBB-CCCC",
            purchaser_email="buyer@example.com",
            stripe_session_id="cs_yardtalk",
            stripe_payment_intent_id="pi_yardtalk",
        )

    def fire_webhook(self, event_type, data, event_id="evt_yardtalk"):
        event = {
            "id": event_id,
            "type": event_type,
            "data": {"object": data},
        }
        with patch(
            "apps.billing.views.stripe.Webhook.construct_event",
            return_value=event,
        ):
            return self.client.post(
                "/api/v1/billing/webhook/",
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="test-signature",
            )

    def assert_license_state(self, status, reason=""):
        self.license.refresh_from_db()
        self.assertEqual(self.license.status, status)
        self.assertEqual(self.license.revocation_reason, reason)

    @patch("apps.billing.views.handle_credit_refund")
    def test_full_refund_revokes_license(self, mock_credit_refund):
        response = self.fire_webhook(
            "charge.refunded",
            {
                "id": "ch_yardtalk",
                "payment_intent": "pi_yardtalk",
                "amount": 2000,
                "amount_refunded": 2000,
                "refunded": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        mock_credit_refund.assert_called_once()
        self.assert_license_state(
            License.Status.REVOKED,
            License.RevocationReason.REFUND,
        )

    @patch("apps.billing.views.handle_credit_refund")
    def test_partial_refund_does_not_revoke_and_logs_warning(self, mock_credit_refund):
        with self.assertLogs("apps.yardtalk.services", level="WARNING") as logs:
            response = self.fire_webhook(
                "charge.refunded",
                {
                    "id": "ch_yardtalk",
                    "payment_intent": "pi_yardtalk",
                    "amount": 2000,
                    "amount_refunded": 500,
                    "refunded": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("partial refund requires manual review" in message for message in logs.output))
        self.assert_license_state(License.Status.ACTIVE)

    @patch(
        "apps.yardtalk.services.stripe.Charge.retrieve",
        return_value={"id": "ch_yardtalk", "payment_intent": "pi_yardtalk"},
    )
    def test_dispute_resolves_charge_and_revokes_license(self, mock_retrieve):
        response = self.fire_webhook(
            "charge.dispute.created",
            {
                "id": "dp_yardtalk",
                "payment_intent": None,
                "charge": "ch_yardtalk",
            },
        )

        self.assertEqual(response.status_code, 200)
        mock_retrieve.assert_called_once_with(
            "ch_yardtalk",
            api_key="sk_test_placeholder",
        )
        self.assert_license_state(
            License.Status.REVOKED,
            License.RevocationReason.DISPUTE,
        )

    @patch("apps.billing.views.handle_credit_refund")
    def test_refund_redelivery_is_idempotent(self, mock_credit_refund):
        data = {
            "id": "ch_yardtalk",
            "payment_intent": "pi_yardtalk",
            "amount": 2000,
            "amount_refunded": 2000,
            "refunded": True,
        }

        first = self.fire_webhook("charge.refunded", data)
        with self.assertLogs("apps.yardtalk.services", level="DEBUG") as logs:
            second = self.fire_webhook("charge.refunded", data)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(any("already revoked" in message for message in logs.output))
        self.assert_license_state(
            License.Status.REVOKED,
            License.RevocationReason.REFUND,
        )

    @patch("apps.billing.views.handle_credit_refund")
    def test_non_yardtalk_payment_intent_is_ignored(self, mock_credit_refund):
        with self.assertLogs("apps.yardtalk.services", level="DEBUG") as logs:
            response = self.fire_webhook(
                "charge.refunded",
                {
                    "id": "ch_subscription",
                    "payment_intent": "pi_subscription",
                    "amount": 2000,
                    "amount_refunded": 2000,
                    "refunded": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("no license for PaymentIntent pi_subscription" in message for message in logs.output))
        self.assert_license_state(License.Status.ACTIVE)

    @patch("apps.billing.views.handle_credit_refund")
    def test_already_revoked_license_is_noop(self, mock_credit_refund):
        License.objects.filter(pk=self.license.pk).update(
            status=License.Status.REVOKED,
            revocation_reason=License.RevocationReason.MANUAL,
        )

        response = self.fire_webhook(
            "charge.refunded",
            {
                "id": "ch_yardtalk",
                "payment_intent": "pi_yardtalk",
                "amount": 2000,
                "amount_refunded": 2000,
                "refunded": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assert_license_state(
            License.Status.REVOKED,
            License.RevocationReason.MANUAL,
        )
