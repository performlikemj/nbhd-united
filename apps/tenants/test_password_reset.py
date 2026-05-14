"""Tests for the password-reset request + confirm flow."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

User = get_user_model()


REQUEST_URL = "/api/v1/auth/password-reset/request/"
CONFIRM_URL = "/api/v1/auth/password-reset/confirm/"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    FRONTEND_URL="https://app.example.test",
    DEFAULT_FROM_EMAIL="NBHD United <noreply@example.test>",
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "password-reset-tests",
        }
    },
)
class PasswordResetFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        mail.outbox = []
        self.user = User.objects.create_user(
            username="alice@example.test",
            email="alice@example.test",
            password="originalpass-987!",
            display_name="Alice",
        )

    # ----- request endpoint -----

    def test_request_sends_email_for_known_user(self):
        response = self.client.post(
            REQUEST_URL,
            {"email": "alice@example.test"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["alice@example.test"])
        self.assertIn("https://app.example.test/reset-password?uid=", msg.body)
        html_body = next(
            (body for body, mimetype in msg.alternatives if mimetype == "text/html"),
            "",
        )
        self.assertIn("https://app.example.test/reset-password?uid=", html_body)

    def test_request_returns_200_for_unknown_email_and_sends_no_mail(self):
        response = self.client.post(
            REQUEST_URL,
            {"email": "ghost@example.test"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_request_for_inactive_user_sends_no_mail(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        response = self.client.post(
            REQUEST_URL,
            {"email": "alice@example.test"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mail.outbox, [])

    def test_request_is_case_insensitive_on_email(self):
        response = self.client.post(
            REQUEST_URL,
            {"email": "ALICE@example.TEST"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

    def test_request_rate_limit_per_email(self):
        for _ in range(3):
            response = self.client.post(
                REQUEST_URL,
                {"email": "alice@example.test"},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
        response = self.client.post(
            REQUEST_URL,
            {"email": "alice@example.test"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)

    # ----- confirm endpoint -----

    def _make_uid_token(self, user) -> tuple[str, str]:
        return (
            urlsafe_base64_encode(force_bytes(user.pk)),
            default_token_generator.make_token(user),
        )

    def test_confirm_sets_new_password_and_returns_jwt_pair(self):
        uid, token = self._make_uid_token(self.user)
        response = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "newpass-456-secure!"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("access", payload)
        self.assertIn("refresh", payload)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("newpass-456-secure!"))
        self.assertFalse(self.user.check_password("originalpass-987!"))

    def test_confirm_rejects_bad_token(self):
        uid, _ = self._make_uid_token(self.user)
        response = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": "not-a-real-token", "new_password": "newpass-456!"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("originalpass-987!"))

    def test_confirm_rejects_bad_uid(self):
        _, token = self._make_uid_token(self.user)
        response = self.client.post(
            CONFIRM_URL,
            {"uid": "garbage", "token": token, "new_password": "newpass-456!"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_confirm_token_is_single_use(self):
        uid, token = self._make_uid_token(self.user)
        first = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "firstrotate-789!"},
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)

        # The password hash just changed, so the old token's HMAC no
        # longer validates.
        second = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "secondrotate-789!"},
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 400)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("firstrotate-789!"))
        self.assertFalse(self.user.check_password("secondrotate-789!"))

    def test_confirm_rejects_weak_password(self):
        uid, token = self._make_uid_token(self.user)
        response = self.client.post(
            CONFIRM_URL,
            {"uid": uid, "token": token, "new_password": "short"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("originalpass-987!"))

    def test_confirm_requires_all_fields(self):
        response = self.client.post(
            CONFIRM_URL,
            {"uid": "", "token": "", "new_password": ""},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
