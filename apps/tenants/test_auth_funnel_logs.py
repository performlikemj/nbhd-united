"""Log-contract tests for password signup and web→app authorization."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .oauth_models import pkce_s256
from .serializers import EmailTokenObtainPairSerializer

User = get_user_model()

REDIRECT_URI = "nbhd://auth/callback"
VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
CHALLENGE = pkce_s256(VERIFIER)


class SignupFunnelLogTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse("auth-signup")

    def _post_with_logs(self, body):
        with self.assertLogs("apps.tenants.auth_views", level="INFO") as logs:
            response = self.client.post(self.url, body, format="json")
        return response, [record.getMessage() for record in logs.records]

    def test_missing_fields_logs_reason(self):
        response, messages = self._post_with_logs({"email": "missing-password@example.com"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(messages, ["auth.signup.invalid reason=missing_fields source=web"])

    @override_settings(PREVIEW_ACCESS_KEY="required-preview-code")
    def test_invalid_invite_code_logs_reason(self):
        response, messages = self._post_with_logs(
            {
                "email": "invite@example.com",
                "password": "Xk9$mP2nQr!7vZ",
                "invite_code": "wrong-preview-code",
            }
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(messages, ["auth.signup.invalid reason=invite_code source=web"])

    def test_duplicate_email_logs_reason_without_email(self):
        email = "private-duplicate@example.com"
        User.objects.create_user(username=email, email=email, password="Xk9$mP2nQr!7vZ")

        response, messages = self._post_with_logs(
            {
                "email": email,
                "password": "Another9$StrongPassword",
                "source": "ios_handoff",
            }
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            messages,
            ["auth.signup.invalid reason=duplicate_email source=ios_handoff"],
        )
        self.assertNotIn(email, "\n".join(messages))

    def test_weak_password_logs_reason(self):
        response, messages = self._post_with_logs({"email": "weak@example.com", "password": "password"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(messages, ["auth.signup.invalid reason=weak_password source=web"])

    def test_success_logs_ios_handoff_source(self):
        response, messages = self._post_with_logs(
            {
                "email": "ios-success@example.com",
                "password": "Xk9$mP2nQr!7vZ",
                "source": "ios_handoff",
            }
        )

        user = User.objects.get(email="ios-success@example.com")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            messages,
            [f"auth.signup.success user_id={user.id} source=ios_handoff"],
        )

    def test_success_without_source_logs_web(self):
        response, messages = self._post_with_logs({"email": "web-success@example.com", "password": "Xk9$mP2nQr!7vZ"})

        user = User.objects.get(email="web-success@example.com")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            messages,
            [f"auth.signup.success user_id={user.id} source=web"],
        )

    def test_success_with_unknown_source_logs_web(self):
        response, messages = self._post_with_logs(
            {
                "email": "unknown-source@example.com",
                "password": "Xk9$mP2nQr!7vZ",
                "source": "partner",
            }
        )

        user = User.objects.get(email="unknown-source@example.com")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            messages,
            [f"auth.signup.success user_id={user.id} source=web"],
        )

    def test_unexpected_failure_logs_reason_and_reraises(self):
        with (
            patch(
                "apps.tenants.auth_views.User.objects.create_user",
                side_effect=RuntimeError("account creation failed"),
            ),
            self.assertLogs("apps.tenants.auth_views", level="INFO") as logs,
            self.assertRaisesRegex(RuntimeError, "account creation failed"),
        ):
            self.client.post(
                self.url,
                {
                    "email": "failure@example.com",
                    "password": "Xk9$mP2nQr!7vZ",
                    "source": "ios_handoff",
                },
                format="json",
            )

        self.assertEqual(
            [record.getMessage() for record in logs.records],
            ["auth.signup.failed reason=unexpected source=ios_handoff"],
        )


class AuthorizeFunnelLogTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="authorize@example.com",
            email="authorize@example.com",
            password="Pass1234!",
        )
        token = EmailTokenObtainPairSerializer.get_token(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.access_token}")
        self.url = reverse("auth-authorize")

    def _post_with_logs(self, body):
        with self.assertLogs("apps.tenants.oauth_views", level="INFO") as logs:
            response = self.client.post(self.url, body, format="json")
        return response, [record.getMessage() for record in logs.records]

    def _valid_body(self, *, client):
        return {
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "redirect_uri": REDIRECT_URI,
            "client": client,
        }

    def test_invalid_request_logs_reason(self):
        response, messages = self._post_with_logs(
            {
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            messages,
            [f"auth.authorize.invalid reason=invalid_request user_id={self.user.id}"],
        )

    def test_success_logs_ios_client(self):
        response, messages = self._post_with_logs(self._valid_body(client="ios"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            messages,
            [f"auth.authorize.success user_id={self.user.id} client=ios"],
        )

    def test_success_logs_unknown_client_as_other(self):
        response, messages = self._post_with_logs(self._valid_body(client="partner"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            messages,
            [f"auth.authorize.success user_id={self.user.id} client=other"],
        )
