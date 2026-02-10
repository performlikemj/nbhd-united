"""Integration OAuth view tests."""
from __future__ import annotations

from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from django.core import signing
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant


class OAuthAuthorizeViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = create_tenant(display_name="OAuth User", telegram_chat_id=414141)
        self.client.force_authenticate(user=self.tenant.user)

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="google-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
    )
    def test_authorize_state_binds_user_and_provider(self):
        response = self.client.get("/api/v1/integrations/authorize/gmail/")
        self.assertEqual(response.status_code, 200)

        auth_url = response.data["url"]
        parsed = urlparse(auth_url)
        query = parse_qs(parsed.query)
        state = query["state"][0]

        payload = signing.loads(state, salt="oauth")
        self.assertEqual(payload["user_id"], str(self.tenant.user.id))
        self.assertEqual(payload["provider"], "gmail")


@override_settings(
    FRONTEND_URL="http://localhost:3000",
    GOOGLE_OAUTH_CLIENT_ID="google-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
)
class OAuthCallbackViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = create_tenant(display_name="Callback User", telegram_chat_id=515151)

    def _state(self, provider: str = "gmail") -> str:
        return signing.dumps(
            {"user_id": str(self.tenant.user.id), "provider": provider},
            salt="oauth",
        )

    def test_callback_rejects_provider_mismatch_in_state(self):
        state = self._state(provider="google-calendar")
        response = self.client.get(
            f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "http://localhost:3000/integrations?error=invalid_state",
        )

    @patch("apps.integrations.views.fetch_provider_email", return_value="person@example.com")
    @patch("apps.integrations.views.connect_integration")
    @patch("apps.integrations.views.httpx.post")
    def test_callback_passes_provider_email_to_connect(
        self,
        mock_post,
        mock_connect,
        _mock_fetch_provider_email,
    ):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_response

        state = self._state(provider="gmail")
        response = self.client.get(
            f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "http://localhost:3000/integrations?connected=gmail",
        )
        mock_connect.assert_called_once()
        self.assertEqual(mock_connect.call_args.kwargs["provider_email"], "person@example.com")

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="",
        GOOGLE_OAUTH_CLIENT_SECRET="",
    )
    def test_callback_reports_when_oauth_not_configured(self):
        state = self._state(provider="gmail")
        response = self.client.get(
            f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "http://localhost:3000/integrations?error=oauth_not_configured",
        )
