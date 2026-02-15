"""Integration OAuth view tests."""
from __future__ import annotations

from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.core import signing
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from .views import OAUTH_STATE_MAX_AGE_SECONDS, _state_nonce_cache_key


class OAuthAuthorizeViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = create_tenant(display_name="OAuth User", telegram_chat_id=414141)
        self.client.force_authenticate(user=self.tenant.user)

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="google-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
        COMPOSIO_API_KEY="",
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

        scope_values = set(query["scope"][0].split(" "))
        self.assertIn("https://www.googleapis.com/auth/gmail.readonly", scope_values)
        self.assertNotIn("https://www.googleapis.com/auth/gmail.modify", scope_values)

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="google-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="",
        COMPOSIO_API_KEY="",
    )
    def test_authorize_requires_client_secret(self):
        response = self.client.get("/api/v1/integrations/authorize/gmail/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("OAuth not configured", response.data["detail"])

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="google-client-id",
        GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
        COMPOSIO_API_KEY="",
    )
    @patch("apps.integrations.views.cache.set", side_effect=RuntimeError("cache unavailable"))
    def test_authorize_handles_cache_write_failure(self, _mock_cache_set):
        response = self.client.get("/api/v1/integrations/authorize/gmail/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("url", response.data)


@override_settings(
    FRONTEND_URL="http://localhost:3000",
    GOOGLE_OAUTH_CLIENT_ID="google-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
    COMPOSIO_API_KEY="",
)
class OAuthCallbackViewTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = create_tenant(display_name="Callback User", telegram_chat_id=515151)

    def _state(self, provider: str = "gmail") -> str:
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.get(f"/api/v1/integrations/authorize/{provider}/")
        self.client.force_authenticate(user=None)
        self.assertEqual(response.status_code, 200)
        auth_url = response.data["url"]
        parsed = urlparse(auth_url)
        query = parse_qs(parsed.query)
        return query["state"][0]

    def test_callback_rejects_provider_mismatch_in_state(self):
        state = self._state(provider="google-calendar")
        response = self.client.get(
            f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "http://localhost:3000/settings/integrations?error=invalid_state",
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
            "http://localhost:3000/settings/integrations?connected=gmail",
        )
        mock_connect.assert_called_once()
        self.assertEqual(mock_connect.call_args.kwargs["provider_email"], "person@example.com")

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="",
        GOOGLE_OAUTH_CLIENT_SECRET="",
    )
    def test_callback_reports_when_oauth_not_configured(self):
        nonce = "test-nonce-missing-secret"
        cache.set(_state_nonce_cache_key(nonce), "1", timeout=OAUTH_STATE_MAX_AGE_SECONDS)
        state = signing.dumps(
            {"user_id": str(self.tenant.user.id), "provider": "gmail", "nonce": nonce},
            salt="oauth",
        )
        response = self.client.get(
            f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            "http://localhost:3000/settings/integrations?error=oauth_not_configured",
        )

    @patch("apps.integrations.views.fetch_provider_email", return_value=None)
    @patch("apps.integrations.views.connect_integration")
    @patch("apps.integrations.views.httpx.post")
    def test_callback_rejects_replayed_state(self, mock_post, _mock_connect, _mock_email):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_response

        state = self._state(provider="gmail")
        first = self.client.get(
            f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
        )
        second = self.client.get(
            f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(first["Location"], "http://localhost:3000/settings/integrations?connected=gmail")
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second["Location"], "http://localhost:3000/settings/integrations?error=invalid_state")

    @patch("apps.integrations.views.fetch_provider_email", return_value=None)
    @patch("apps.integrations.views.connect_integration")
    @patch("apps.integrations.views.httpx.post")
    def test_callback_uses_fallback_nonce_when_cache_unavailable(
        self,
        mock_post,
        _mock_connect,
        _mock_email,
    ):
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "expires_in": 3600,
        }
        mock_post.return_value = mock_response

        with patch("apps.integrations.views.cache.set", side_effect=RuntimeError("cache down")):
            state = self._state(provider="gmail")

        with (
            patch("apps.integrations.views.cache.get", side_effect=RuntimeError("cache down")),
            patch("apps.integrations.views.cache.delete", side_effect=RuntimeError("cache down")),
        ):
            first = self.client.get(
                f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
            )
            second = self.client.get(
                f"/api/v1/integrations/callback/gmail/?state={state}&code=auth-code"
            )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(first["Location"], "http://localhost:3000/settings/integrations?connected=gmail")
        self.assertEqual(second.status_code, 302)
        self.assertEqual(second["Location"], "http://localhost:3000/settings/integrations?error=invalid_state")
        self.assertEqual(mock_post.call_count, 1)
