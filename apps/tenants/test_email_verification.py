"""Tests for the email-verification flow (signup → resend → confirm)."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from apps.tenants.email_verification import email_verification_token_generator

User = get_user_model()


SIGNUP_URL = "/api/v1/auth/signup/"
REQUEST_URL = "/api/v1/auth/email-verification/request/"
CONFIRM_URL = "/api/v1/auth/email-verification/confirm/"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FRONTEND_URL="https://app.example.test",
    DEFAULT_FROM_EMAIL="NBHD United <noreply@example.test>",
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "email-verification-tests",
        }
    },
)
class EmailVerificationFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []
        # DRF APIClient supports force_authenticate, which is the right tool
        # for JWT-protected endpoints — force_login only sets a Django session.
        self.client = APIClient()

    # ----- signup -----

    def test_signup_creates_unverified_user_and_sends_email(self):
        response = self.client.post(
            SIGNUP_URL,
            {
                "email": "alice@example.test",
                "password": "originalpass-987!",
                "display_name": "Alice",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertIn("access", response.json())

        user = User.objects.get(email="alice@example.test")
        self.assertFalse(user.email_verified)
        self.assertIsNone(user.email_verified_at)

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["alice@example.test"])
        self.assertIn("https://app.example.test/verify-email/confirm?uid=", msg.body)

    # ----- resend (authenticated) -----

    def _make_user(self, email="bob@example.test", verified=False):
        return User.objects.create_user(
            username=email,
            email=email,
            password="originalpass-987!",
            display_name="Bob",
            email_verified=verified,
        )

    def test_request_resends_email_for_unverified_user(self):
        user = self._make_user()
        self.client.force_authenticate(user)

        response = self.client.post(REQUEST_URL, {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])

    def test_request_is_noop_when_already_verified(self):
        user = self._make_user(verified=True)
        self.client.force_authenticate(user)

        response = self.client.post(REQUEST_URL, {}, format="json")
        # Still 200 (idempotent) but no email leaves
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_request_requires_authentication(self):
        response = self.client.post(REQUEST_URL, {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_request_rate_limit_per_email(self):
        user = self._make_user()
        self.client.force_authenticate(user)

        for _ in range(3):
            response = self.client.post(REQUEST_URL, {}, format="json")
            self.assertEqual(response.status_code, 200)
        response = self.client.post(REQUEST_URL, {}, format="json")
        self.assertEqual(response.status_code, 429)

    # ----- confirm -----

    def _make_uid_token(self, user) -> tuple[str, str]:
        return (
            urlsafe_base64_encode(force_bytes(user.pk)),
            email_verification_token_generator.make_token(user),
        )

    def test_confirm_marks_email_verified(self):
        user = self._make_user()
        uid, token = self._make_uid_token(user)

        response = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": token},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["email_verified"])

        user.refresh_from_db()
        self.assertTrue(user.email_verified)
        self.assertIsNotNone(user.email_verified_at)

    def test_confirm_rejects_bad_token(self):
        user = self._make_user()
        uid, _ = self._make_uid_token(user)

        response = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": "not-a-real-token"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.email_verified)

    def test_confirm_rejects_bad_uid(self):
        user = self._make_user()
        _, token = self._make_uid_token(user)

        response = self.client.post(
            CONFIRM_URL,
            {"uid": "garbage", "token": token},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        user.refresh_from_db()
        self.assertFalse(user.email_verified)

    def test_confirm_requires_all_fields(self):
        response = self.client.post(
            CONFIRM_URL,
            {"uid": "", "token": ""},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_confirm_is_idempotent_when_already_verified(self):
        user = self._make_user(verified=True)
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        # We don't even need a valid token — the early idempotent branch
        # accepts on already-verified to make double-clicking the link safe.
        response = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": "stale-or-empty-token"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["email_verified"])

    def test_token_invalidates_if_email_changes(self):
        """The HMAC includes email, so changing it nukes outstanding tokens."""
        user = self._make_user()
        uid, token = self._make_uid_token(user)

        user.email = "alice.renamed@example.test"
        user.save(update_fields=["email"])

        response = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": token},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class EmailVerificationTokenGeneratorTests(TestCase):
    """Direct unit tests for the token generator."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="carol@example.test",
            email="carol@example.test",
            password="originalpass-987!",
        )

    def test_token_validates_pre_verify(self):
        token = email_verification_token_generator.make_token(self.user)
        self.assertTrue(email_verification_token_generator.check_token(self.user, token))

    def test_token_invalidates_post_verify(self):
        token = email_verification_token_generator.make_token(self.user)
        self.user.email_verified = True
        self.user.save(update_fields=["email_verified"])
        self.assertFalse(email_verification_token_generator.check_token(self.user, token))

    def test_token_invalidates_on_email_change(self):
        token = email_verification_token_generator.make_token(self.user)
        self.user.email = "carol.new@example.test"
        self.user.save(update_fields=["email"])
        self.assertFalse(email_verification_token_generator.check_token(self.user, token))


class StripeCheckoutEmailGateTests(TestCase):
    """The Stripe checkout endpoint must short-circuit to 403 for unverified users."""

    def setUp(self):
        self.client = APIClient()

    def test_checkout_blocks_unverified_user(self):
        user = User.objects.create_user(
            username="dan@example.test",
            email="dan@example.test",
            password="originalpass-987!",
            email_verified=False,
        )
        self.client.force_authenticate(user)
        response = self.client.post("/api/v1/billing/checkout/", {}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json().get("code"), "email_not_verified")

    def test_checkout_allows_verified_user_past_gate(self):
        # We only assert the verification gate passes — Stripe is not
        # configured in tests so the next failure is a 503, which is the
        # correct "we got past the gate" signal.
        user = User.objects.create_user(
            username="eve@example.test",
            email="eve@example.test",
            password="originalpass-987!",
            email_verified=True,
        )
        self.client.force_authenticate(user)
        response = self.client.post("/api/v1/billing/checkout/", {}, format="json")
        self.assertNotEqual(response.status_code, 403)
