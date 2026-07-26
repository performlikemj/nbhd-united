from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings

from apps.yardtalk.models import License
from apps.yardtalk.services import YARDTALK_DOWNLOAD_URL

YARDTALK_PRICE_ID = "price_yardtalk_test"


def checkout_session(
    session_id="cs_yardtalk",
    *,
    payment_status="paid",
    price_id=YARDTALK_PRICE_ID,
):
    return {
        "id": session_id,
        "mode": "payment",
        "payment_status": payment_status,
        "customer": "cus_yardtalk",
        "payment_intent": "pi_yardtalk",
        "customer_details": {"email": "buyer@example.com"},
        "line_items": {
            "data": [
                {
                    "price": {
                        "id": price_id,
                    }
                }
            ]
        },
    }


@override_settings(
    DJSTRIPE_WEBHOOK_SECRET="whsec_test",
    STRIPE_LIVE_MODE=False,
    STRIPE_TEST_SECRET_KEY="sk_test_placeholder",
    YARDTALK_STRIPE_PRICE_ID=YARDTALK_PRICE_ID,
)
class YardTalkWebhookTests(TestCase):
    def webhook_event(self, session_id="cs_yardtalk"):
        return {
            "id": f"evt_{session_id}",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "mode": "payment",
                    "metadata": {},
                }
            },
        }

    def fire_webhook(self, event, retrieved_session):
        with (
            patch(
                "apps.billing.views.stripe.Webhook.construct_event",
                return_value=event,
            ),
            patch(
                "apps.yardtalk.services.stripe.checkout.Session.retrieve",
                return_value=retrieved_session,
            ),
        ):
            return self.client.post(
                "/api/v1/billing/webhook/",
                data=b"{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="test-signature",
            )

    def test_completed_session_issues_one_license_and_emails_key(self):
        response = self.fire_webhook(
            self.webhook_event(),
            checkout_session(),
        )
        self.assertEqual(response.status_code, 200)

        license_obj = License.objects.get(stripe_session_id="cs_yardtalk")
        self.assertRegex(license_obj.key, r"^YT-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")
        self.assertEqual(license_obj.purchaser_email, "buyer@example.com")
        self.assertEqual(license_obj.stripe_customer_id, "cus_yardtalk")
        self.assertEqual(license_obj.stripe_payment_intent_id, "pi_yardtalk")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn(license_obj.key, message.body)
        self.assertIn(YARDTALK_DOWNLOAD_URL, message.body)
        html_body = next(
            alternative.content for alternative in message.alternatives if alternative.mimetype == "text/html"
        )
        self.assertIn(f'href="{YARDTALK_DOWNLOAD_URL}"', html_body)

    def test_webhook_redelivery_is_idempotent(self):
        event = self.webhook_event()
        session = checkout_session()
        self.fire_webhook(event, session)
        self.fire_webhook(event, session)

        self.assertEqual(License.objects.filter(stripe_session_id="cs_yardtalk").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_unpaid_or_foreign_session_does_not_issue(self):
        cases = [
            checkout_session(session_id="cs_unpaid", payment_status="unpaid"),
            checkout_session(session_id="cs_foreign", price_id="price_foreign"),
        ]
        for session in cases:
            with self.subTest(session_id=session["id"]):
                response = self.fire_webhook(
                    self.webhook_event(session["id"]),
                    session,
                )
                self.assertEqual(response.status_code, 200)
        self.assertFalse(License.objects.exists())


@override_settings(
    STRIPE_LIVE_MODE=False,
    STRIPE_TEST_SECRET_KEY="sk_test_placeholder",
    YARDTALK_STRIPE_PRICE_ID=YARDTALK_PRICE_ID,
)
class YardTalkClaimTests(TestCase):
    def claim(self, session_id, retrieved_session):
        with patch(
            "apps.yardtalk.services.stripe.checkout.Session.retrieve",
            return_value=retrieved_session,
        ):
            return self.client.get(
                "/api/v1/yardtalk/licenses/claim/",
                {"session_id": session_id},
            )

    def test_paid_session_returns_same_key_on_refresh(self):
        session = checkout_session()
        first = self.claim("cs_yardtalk", session)
        second = self.claim("cs_yardtalk", session)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertRegex(first.json()["license_key"], r"^YT-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$")
        self.assertEqual(License.objects.filter(stripe_session_id="cs_yardtalk").count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_unpaid_session_is_refused(self):
        response = self.claim(
            "cs_unpaid",
            checkout_session(session_id="cs_unpaid", payment_status="unpaid"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(License.objects.exists())

    def test_foreign_price_session_is_refused(self):
        response = self.claim(
            "cs_foreign",
            checkout_session(session_id="cs_foreign", price_id="price_foreign"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(License.objects.exists())

    def test_missing_session_id_is_400(self):
        response = self.client.get("/api/v1/yardtalk/licenses/claim/")
        self.assertEqual(response.status_code, 400)
