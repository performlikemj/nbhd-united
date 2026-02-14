"""Additional integration service coverage."""
from datetime import timedelta
import json
import os
from unittest.mock import Mock, patch

import httpx
from django.test import TestCase
from django.test.utils import override_settings
from django.utils import timezone

from apps.tenants.services import create_tenant

from .models import Integration
from .services import (
    IntegrationInactiveError,
    IntegrationNotConnectedError,
    IntegrationProviderConfigError,
    IntegrationRefreshError,
    IntegrationScopeError,
    IntegrationTokenDataError,
    initiate_composio_connection,
    connect_integration,
    disconnect_integration,
    get_valid_provider_access_token,
    get_key_vault_secret_name,
    load_tokens_from_key_vault,
    refresh_integration_tokens,
)


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

    def test_load_tokens_from_key_vault_returns_none_for_non_object_payload(self):
        with patch.dict(os.environ, {"AZURE_MOCK": "true"}):
            from . import services as integration_services

            secret_name = get_key_vault_secret_name(self.tenant, "gmail")
            integration_services._MOCK_KEY_VAULT_STORE[secret_name] = json.dumps(["x"])

        payload = load_tokens_from_key_vault(self.tenant, "gmail")

        self.assertIsNone(payload)

    @override_settings(
        COMPOSIO_API_KEY="test-key",
        COMPOSIO_GMAIL_AUTH_CONFIG_ID="ac-test-gmail",
        COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS=True,
    )
    @patch("apps.integrations.services._get_composio_client")
    def test_initiate_composio_connection_allows_multiple_accounts(
        self,
        mock_get_client,
    ):
        mock_connected_accounts = Mock()
        mock_connected_accounts.initiate.return_value = Mock(
            redirect_url="https://composio.example/connect",
            id="conn-1",
        )
        mock_get_client.return_value = Mock(connected_accounts=mock_connected_accounts)

        redirect_url, request_id = initiate_composio_connection(
            self.tenant,
            "gmail",
            "https://app.example.com/callback",
        )

        self.assertEqual(redirect_url, "https://composio.example/connect")
        self.assertEqual(request_id, "conn-1")
        mock_connected_accounts.initiate.assert_called_once_with(
            user_id=f"tenant-{self.tenant.id}",
            auth_config_id="ac-test-gmail",
            callback_url="https://app.example.com/callback",
            allow_multiple=True,
        )


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="google-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="google-client-secret",
)
class IntegrationCredentialBrokerTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Broker", telegram_chat_id=919191)

    def test_get_valid_provider_access_token_requires_connected_integration(self):
        with self.assertRaises(IntegrationNotConnectedError):
            get_valid_provider_access_token(self.tenant, "gmail")

    def test_get_valid_provider_access_token_requires_active_status(self):
        Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.REVOKED,
        )

        with self.assertRaises(IntegrationInactiveError):
            get_valid_provider_access_token(self.tenant, "gmail")

    @patch("apps.integrations.services.load_tokens_from_key_vault")
    def test_get_valid_provider_access_token_rejects_insufficient_scope(self, mock_load_tokens):
        Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            token_expires_at=timezone.now() + timedelta(hours=1),
        )
        mock_load_tokens.return_value = {"access_token": "access-token"}

        with self.assertRaises(IntegrationScopeError):
            get_valid_provider_access_token(self.tenant, "gmail")

    @patch("apps.integrations.services.load_tokens_from_key_vault")
    def test_get_valid_provider_access_token_accepts_legacy_gmail_modify_scope(self, mock_load_tokens):
        Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
            token_expires_at=timezone.now() + timedelta(hours=1),
        )
        mock_load_tokens.return_value = {"access_token": "access-token"}

        result = get_valid_provider_access_token(self.tenant, "gmail")

        self.assertEqual(result.access_token, "access-token")

    @patch("apps.integrations.services.refresh_integration_tokens")
    @patch("apps.integrations.services.load_tokens_from_key_vault")
    def test_get_valid_provider_access_token_returns_existing_access_token(
        self,
        mock_load_tokens,
        mock_refresh_tokens,
    ):
        Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
            token_expires_at=timezone.now() + timedelta(hours=1),
        )
        mock_load_tokens.return_value = {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }

        result = get_valid_provider_access_token(self.tenant, "gmail")

        self.assertEqual(result.access_token, "access-token")
        self.assertEqual(result.provider, "gmail")
        self.assertEqual(result.tenant_id, str(self.tenant.id))
        mock_refresh_tokens.assert_not_called()

    @patch("apps.integrations.services.refresh_integration_tokens")
    @patch("apps.integrations.services.load_tokens_from_key_vault")
    def test_get_valid_provider_access_token_refreshes_when_expiring(
        self,
        mock_load_tokens,
        mock_refresh_tokens,
    ):
        integration = Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
            token_expires_at=timezone.now() + timedelta(seconds=30),
        )
        integration.token_expires_at = timezone.now() + timedelta(hours=1)
        mock_refresh_tokens.return_value = integration
        mock_load_tokens.side_effect = [
            {"access_token": "old-access", "refresh_token": "refresh-token"},
            {"access_token": "new-access", "refresh_token": "refresh-token"},
        ]

        result = get_valid_provider_access_token(self.tenant, "gmail")

        self.assertEqual(result.access_token, "new-access")
        mock_refresh_tokens.assert_called_once()

    @patch("apps.integrations.services.load_tokens_from_key_vault")
    def test_get_valid_provider_access_token_marks_expired_without_refresh_token(self, mock_load_tokens):
        integration = Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
            token_expires_at=timezone.now() + timedelta(seconds=30),
        )
        mock_load_tokens.return_value = {"access_token": "access-only"}

        with self.assertRaises(IntegrationTokenDataError):
            get_valid_provider_access_token(self.tenant, "gmail")

        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.EXPIRED)

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="",
        GOOGLE_OAUTH_CLIENT_SECRET="",
    )
    @patch("apps.integrations.services.load_tokens_from_key_vault")
    def test_get_valid_provider_access_token_marks_error_when_provider_config_missing(self, mock_load_tokens):
        integration = Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
            token_expires_at=timezone.now() + timedelta(seconds=30),
        )
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}

        with self.assertRaises(IntegrationProviderConfigError):
            get_valid_provider_access_token(self.tenant, "gmail")

        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.ERROR)

    @patch("apps.integrations.services.refresh_integration_tokens")
    @patch("apps.integrations.services.load_tokens_from_key_vault")
    def test_get_valid_provider_access_token_marks_expired_on_refresh_400(
        self,
        mock_load_tokens,
        mock_refresh_tokens,
    ):
        integration = Integration.objects.create(
            tenant=self.tenant,
            provider="gmail",
            status=Integration.Status.ACTIVE,
            token_expires_at=timezone.now() + timedelta(seconds=30),
        )
        mock_load_tokens.return_value = {"refresh_token": "refresh-token"}
        req = httpx.Request("POST", "https://oauth2.googleapis.com/token")
        resp = httpx.Response(400, request=req)
        mock_refresh_tokens.side_effect = httpx.HTTPStatusError(
            "bad request",
            request=req,
            response=resp,
        )

        with self.assertRaises(IntegrationRefreshError):
            get_valid_provider_access_token(self.tenant, "gmail")

        integration.refresh_from_db()
        self.assertEqual(integration.status, Integration.Status.EXPIRED)
