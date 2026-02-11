"""Integration scheduled task tests."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenants.services import create_tenant

from .models import Integration
from .tasks import refresh_expiring_integrations_task


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="google-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
)
class RefreshExpiringIntegrationsTaskTest(TestCase):
    def setUp(self):
        tenant = create_tenant(display_name="Refresh User", telegram_chat_id=616161)
        self.integration = Integration.objects.create(
            tenant=tenant,
            provider=Integration.Provider.GMAIL,
            status=Integration.Status.ACTIVE,
            token_expires_at=timezone.now() + timedelta(minutes=5),
        )

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_refreshes_expiring_integration(self, mock_load_tokens, mock_refresh):
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["errored"], 0)
        mock_refresh.assert_called_once()

    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_marks_expired_when_refresh_token_missing(self, mock_load_tokens):
        mock_load_tokens.return_value = {"access_token": "access-only"}

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.integration.status, Integration.Status.EXPIRED)

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="",
        GOOGLE_OAUTH_CLIENT_SECRET="",
    )
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_marks_error_when_oauth_credentials_missing(self, mock_load_tokens):
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["errored"], 1)
        self.assertEqual(self.integration.status, Integration.Status.ERROR)

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_marks_expired_on_http_400(self, mock_load_tokens, mock_refresh):
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}
        req = httpx.Request("POST", "https://oauth2.googleapis.com/token")
        resp = httpx.Response(400, request=req)
        mock_refresh.side_effect = httpx.HTTPStatusError(
            "bad request",
            request=req,
            response=resp,
        )

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.integration.status, Integration.Status.EXPIRED)

    @patch("apps.integrations.tasks.refresh_integration_tokens")
    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_refreshes_when_token_expiry_is_unknown(self, mock_load_tokens, mock_refresh):
        self.integration.token_expires_at = None
        self.integration.save(update_fields=["token_expires_at", "updated_at"])
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        result = refresh_expiring_integrations_task()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["refreshed"], 1)
        mock_refresh.assert_called_once()

    @patch("apps.integrations.tasks.load_tokens_from_key_vault")
    def test_handles_malformed_token_payload_without_crashing(self, mock_load_tokens):
        mock_load_tokens.return_value = ["not-a-dict"]

        result = refresh_expiring_integrations_task()
        self.integration.refresh_from_db()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.integration.status, Integration.Status.EXPIRED)
