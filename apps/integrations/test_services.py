"""Additional integration service coverage."""
from unittest.mock import Mock, patch

from django.test import TestCase

from apps.tenants.services import create_tenant

from .models import Integration
from .services import connect_integration, disconnect_integration, refresh_integration_tokens


class IntegrationServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Integrations", telegram_chat_id=818181)

    @patch("apps.integrations.services.store_tokens_in_key_vault", return_value="secret-name")
    def test_connect_integration_creates_record(self, _mock_store_tokens):
        integration = connect_integration(
            tenant=self.tenant,
            provider="gmail",
            tokens={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
            provider_email="user@example.com",
        )

        self.assertEqual(integration.status, Integration.Status.ACTIVE)
        self.assertEqual(integration.provider_email, "user@example.com")
        self.assertEqual(integration.key_vault_secret_name, "secret-name")
        self.assertIsNotNone(integration.token_expires_at)

    @patch("apps.integrations.services.store_tokens_in_key_vault", return_value="secret-name")
    def test_connect_integration_update_preserves_email_when_none_passed(self, _mock_store_tokens):
        connect_integration(
            tenant=self.tenant,
            provider="gmail",
            tokens={"access_token": "a", "refresh_token": "r", "expires_in": 3600},
            provider_email="kept@example.com",
        )

        integration = connect_integration(
            tenant=self.tenant,
            provider="gmail",
            tokens={"access_token": "b", "refresh_token": "r", "expires_in": 3600},
            provider_email=None,
        )
        integration.refresh_from_db()
        self.assertEqual(integration.provider_email, "kept@example.com")

    @patch("apps.integrations.services.delete_tokens_from_key_vault")
    def test_disconnect_integration_marks_revoked(self, mock_delete_tokens):
        integration = Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
        )

        disconnect_integration(self.tenant, "gmail")

        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.REVOKED)
        mock_delete_tokens.assert_called_once_with(self.tenant, "gmail")

    def test_connect_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            connect_integration(
                tenant=self.tenant,
                provider="unknown-provider",
                tokens={"access_token": "a"},
            )

    @patch("apps.integrations.services.store_tokens_in_key_vault", return_value="secret-refreshed")
    @patch("apps.integrations.services.httpx.post")
    def test_refresh_integration_tokens_updates_record(self, mock_post, _mock_store_tokens):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "access_token": "new-token",
            "expires_in": 1200,
            "scope": "https://www.googleapis.com/auth/gmail.modify",
        }
        mock_post.return_value = response

        integration = refresh_integration_tokens(
            tenant=self.tenant,
            provider="gmail",
            refresh_token="refresh-token",
            client_id="client-id",
            client_secret="client-secret",
        )

        self.assertEqual(integration.status, Integration.Status.ACTIVE)
        self.assertEqual(integration.key_vault_secret_name, "secret-refreshed")
        self.assertIsNotNone(integration.token_expires_at)
        self.assertEqual(
            integration.scopes,
            ["https://www.googleapis.com/auth/gmail.modify"],
        )
