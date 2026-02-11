"""Tests for Azure container payload wiring."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.orchestrator.azure_client import create_container_app


@override_settings(
    AZURE_LOCATION="westus2",
    AZURE_CONTAINER_ENV_ID="/subscriptions/test/resourceGroups/rg/providers/Microsoft.App/managedEnvironments/env",
    AZURE_ACR_SERVER="nbhdunited.azurecr.io",
    AZURE_RESOURCE_GROUP="rg-nbhd-prod",
    AZURE_KEY_VAULT_NAME="kv-nbhd-prod",
    ANTHROPIC_API_KEY="anthropic-secret",
    TELEGRAM_BOT_TOKEN="telegram-secret",
    NBHD_INTERNAL_API_KEY="internal-secret",
    API_BASE_URL="https://nbhd-django.example.com",
)
class AzureClientTest(SimpleTestCase):
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_container_client")
    def test_create_container_app_includes_runtime_config_env(
        self,
        mock_get_container_client,
        _mock_is_mock,
    ):
        mock_client = MagicMock()
        mock_get_container_client.return_value = mock_client

        mock_result = SimpleNamespace(
            properties=SimpleNamespace(
                configuration=SimpleNamespace(
                    ingress=SimpleNamespace(fqdn="oc-tenant.internal.azurecontainerapps.io")
                )
            )
        )
        mock_poller = MagicMock()
        mock_poller.result.return_value = mock_result
        mock_client.container_apps.begin_create_or_update.return_value = mock_poller

        config_json = '{"plugins":{"nbhd-google-tools":{"enabled":true}}}'
        response = create_container_app(
            tenant_id="tenant-123",
            container_name="oc-tenant",
            config_json=config_json,
            identity_id="/identities/tenant-123",
            identity_client_id="client-123",
        )

        self.assertEqual(
            response,
            {"name": "oc-tenant", "fqdn": "oc-tenant.internal.azurecontainerapps.io"},
        )

        call_args = mock_client.container_apps.begin_create_or_update.call_args.args
        self.assertEqual(call_args[0], "rg-nbhd-prod")
        self.assertEqual(call_args[1], "oc-tenant")
        payload = call_args[2]
        secrets = payload["properties"]["configuration"]["secrets"]
        secret_map = {entry["name"]: entry for entry in secrets}
        self.assertEqual(
            secret_map["anthropic-key"]["keyVaultUrl"],
            "https://kv-nbhd-prod.vault.azure.net/secrets/anthropic-api-key",
        )
        self.assertEqual(
            secret_map["telegram-token"]["keyVaultUrl"],
            "https://kv-nbhd-prod.vault.azure.net/secrets/telegram-bot-token",
        )
        self.assertEqual(
            secret_map["nbhd-internal-api-key"]["keyVaultUrl"],
            "https://kv-nbhd-prod.vault.azure.net/secrets/nbhd-internal-api-key",
        )
        self.assertEqual(
            secret_map["nbhd-internal-api-key"]["identity"],
            "/identities/tenant-123",
        )

        container = payload["properties"]["template"]["containers"][0]
        self.assertEqual(container["image"], "nbhdunited.azurecr.io/nbhd-openclaw:latest")

        env_entries = container["env"]
        env_map = {entry["name"]: entry for entry in env_entries}
        self.assertEqual(env_map["NBHD_TENANT_ID"]["value"], "tenant-123")
        self.assertEqual(env_map["NBHD_API_BASE_URL"]["value"], "https://nbhd-django.example.com")
        self.assertEqual(env_map["OPENCLAW_CONFIG_JSON"]["value"], config_json)
        self.assertEqual(env_map["AZURE_CLIENT_ID"]["value"], "client-123")
        self.assertEqual(env_map["NBHD_INTERNAL_API_KEY"]["secretRef"], "nbhd-internal-api-key")

    @override_settings(OPENCLAW_CONTAINER_SECRET_BACKEND="env")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_container_client")
    def test_create_container_app_supports_inline_secret_backend(
        self,
        mock_get_container_client,
        _mock_is_mock,
    ):
        mock_client = MagicMock()
        mock_get_container_client.return_value = mock_client

        mock_result = SimpleNamespace(
            properties=SimpleNamespace(
                configuration=SimpleNamespace(
                    ingress=SimpleNamespace(fqdn="oc-tenant.internal.azurecontainerapps.io")
                )
            )
        )
        mock_poller = MagicMock()
        mock_poller.result.return_value = mock_result
        mock_client.container_apps.begin_create_or_update.return_value = mock_poller

        create_container_app(
            tenant_id="tenant-123",
            container_name="oc-tenant",
            config_json='{"a":1}',
            identity_id="/identities/tenant-123",
            identity_client_id="client-123",
        )

        payload = mock_client.container_apps.begin_create_or_update.call_args.args[2]
        secrets = payload["properties"]["configuration"]["secrets"]
        secret_map = {entry["name"]: entry for entry in secrets}
        self.assertEqual(secret_map["anthropic-key"]["value"], "anthropic-secret")
        self.assertEqual(secret_map["telegram-token"]["value"], "telegram-secret")
        self.assertEqual(secret_map["nbhd-internal-api-key"]["value"], "internal-secret")
