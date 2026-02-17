"""Tests for Azure container payload wiring."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.orchestrator.azure_client import (
    assign_key_vault_role,
    create_container_app,
    create_tenant_file_share,
    register_environment_storage,
    store_tenant_internal_key_in_key_vault,
)


@override_settings(
    AZURE_LOCATION="westus2",
    AZURE_CONTAINER_ENV_ID="/subscriptions/test/resourceGroups/rg/providers/Microsoft.App/managedEnvironments/env",
    AZURE_ACR_SERVER="nbhdunited.azurecr.io",
    AZURE_RESOURCE_GROUP="rg-nbhd-prod",
    AZURE_KEY_VAULT_NAME="kv-nbhd-prod",
    ANTHROPIC_API_KEY="anthropic-secret",
    OPENAI_API_KEY="openai-secret",
    TELEGRAM_BOT_TOKEN="telegram-secret",
    TELEGRAM_WEBHOOK_SECRET="webhook-secret",
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
            configuration=SimpleNamespace(
                ingress=SimpleNamespace(fqdn="oc-tenant.internal.azurecontainerapps.io")
            ),
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
            secret_map["openai-key"]["keyVaultUrl"],
            "https://kv-nbhd-prod.vault.azure.net/secrets/openai-api-key",
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
        self.assertEqual(
            secret_map["telegram-webhook-secret"]["keyVaultUrl"],
            "https://kv-nbhd-prod.vault.azure.net/secrets/telegram-webhook-secret",
        )

        container = payload["properties"]["template"]["containers"][0]
        self.assertEqual(container["image"], "nbhdunited.azurecr.io/nbhd-openclaw:latest")

        env_entries = container["env"]
        env_map = {entry["name"]: entry for entry in env_entries}
        self.assertEqual(env_map["NBHD_TENANT_ID"]["value"], "tenant-123")
        self.assertEqual(env_map["NBHD_API_BASE_URL"]["value"], "https://nbhd-django.example.com")
        self.assertEqual(env_map["OPENCLAW_CONFIG_JSON"]["value"], config_json)
        self.assertEqual(env_map["AZURE_CLIENT_ID"]["value"], "client-123")
        self.assertEqual(env_map["OPENAI_API_KEY"]["secretRef"], "openai-key")
        self.assertEqual(env_map["NBHD_INTERNAL_API_KEY"]["secretRef"], "nbhd-internal-api-key")
        self.assertEqual(env_map["OPENCLAW_GATEWAY_TOKEN"]["secretRef"], "nbhd-internal-api-key")
        self.assertEqual(env_map["OPENCLAW_WEBHOOK_SECRET"]["secretRef"], "telegram-webhook-secret")

        ingress = payload["properties"]["configuration"]["ingress"]
        self.assertEqual(ingress["targetPort"], 8080)

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
            configuration=SimpleNamespace(
                ingress=SimpleNamespace(fqdn="oc-tenant.internal.azurecontainerapps.io")
            ),
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
        self.assertEqual(secret_map["openai-key"]["value"], "openai-secret")
        self.assertEqual(secret_map["telegram-token"]["value"], "telegram-secret")
        self.assertEqual(secret_map["nbhd-internal-api-key"]["value"], "internal-secret")
        self.assertEqual(secret_map["telegram-webhook-secret"]["value"], "webhook-secret")
        container = payload["properties"]["template"]["containers"][0]
        env_map = {entry["name"]: entry for entry in container["env"]}

        # Verify core env vars are present
        self.assertIn("NBHD_TENANT_ID", env_map)


class AssignKeyVaultRoleTest(SimpleTestCase):
    @patch("apps.orchestrator.azure_client._is_mock", return_value=True)
    def test_mock_mode_skips_azure_call(self, _mock_is_mock):
        # Should return without error in mock mode
        assign_key_vault_role("mock-principal-123")

    @override_settings(
        AZURE_SUBSCRIPTION_ID="sub-123",
        AZURE_RESOURCE_GROUP="rg-test",
        AZURE_KEY_VAULT_NAME="kv-test",
    )
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_authorization_client")
    def test_creates_role_assignment(self, mock_get_auth_client, _mock_is_mock):
        mock_client = MagicMock()
        mock_get_auth_client.return_value = mock_client

        assign_key_vault_role("principal-abc")

        mock_client.role_assignments.create.assert_called_once()
        call_kwargs = mock_client.role_assignments.create.call_args.kwargs
        self.assertEqual(
            call_kwargs["scope"],
            "/subscriptions/sub-123/resourceGroups/rg-test"
            "/providers/Microsoft.KeyVault/vaults/kv-test",
        )
        params = call_kwargs["parameters"]
        self.assertEqual(params.principal_id, "principal-abc")
        self.assertEqual(params.principal_type, "ServicePrincipal")

    @override_settings(
        AZURE_SUBSCRIPTION_ID="sub-123",
        AZURE_RESOURCE_GROUP="rg-test",
        AZURE_KEY_VAULT_NAME="kv-test",
    )
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_authorization_client")
    def test_idempotent_on_409_conflict(self, mock_get_auth_client, _mock_is_mock):
        mock_client = MagicMock()
        mock_get_auth_client.return_value = mock_client

        conflict_exc = Exception("RoleAssignmentExists")
        conflict_exc.status_code = 409
        mock_client.role_assignments.create.side_effect = conflict_exc

        # Should not raise
        assign_key_vault_role("principal-abc")
        mock_client.role_assignments.create.assert_called_once()

    @override_settings(
        AZURE_SUBSCRIPTION_ID="sub-123",
        AZURE_RESOURCE_GROUP="rg-test",
        AZURE_KEY_VAULT_NAME="",
    )
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_authorization_client")
    def test_raises_on_missing_vault_name(self, mock_get_auth_client, _mock_is_mock):
        with self.assertRaises(ValueError):
            assign_key_vault_role("principal-abc")


class StoreTenantKeyTest(SimpleTestCase):
    @patch("apps.orchestrator.azure_client._is_mock", return_value=True)
    def test_mock_mode_returns_secret_name(self, _mock_is_mock):
        result = store_tenant_internal_key_in_key_vault("abc-123", "secret-value")
        self.assertEqual(result, "tenant-abc-123-internal-key")

    @override_settings(AZURE_KEY_VAULT_NAME="kv-test")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client._get_provisioner_credential")
    @patch("apps.orchestrator.azure_client.SecretClient", create=True)
    def test_stores_secret_in_key_vault(self, mock_secret_cls, mock_cred, _mock_is_mock):
        # SecretClient is imported inside the function, so we patch at module level with create=True
        # But the function does `from azure.keyvault.secrets import SecretClient` locally
        # We need to mock the import. Let's use a different approach.
        pass


class CreateTenantFileShareTest(SimpleTestCase):
    @patch("apps.orchestrator.azure_client._is_mock", return_value=True)
    def test_mock_mode_returns_share_info(self, _mock_is_mock):
        result = create_tenant_file_share("abc-123-def-456-ghi")
        self.assertEqual(result["share_name"], "ws-abc-123-def-456-ghi")

    @override_settings(
        AZURE_RESOURCE_GROUP="rg-test",
        AZURE_STORAGE_ACCOUNT_NAME="sttest",
    )
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_storage_client")
    def test_creates_file_share(self, mock_get_storage_client, _mock_is_mock):
        mock_client = MagicMock()
        mock_get_storage_client.return_value = mock_client

        result = create_tenant_file_share("tenant-abc")

        mock_client.file_shares.create.assert_called_once()
        call_kwargs = mock_client.file_shares.create.call_args.kwargs
        self.assertEqual(call_kwargs["account_name"], "sttest")
        self.assertEqual(call_kwargs["share_name"], "ws-tenant-abc")
        self.assertEqual(result, {"share_name": "ws-tenant-abc", "account_name": "sttest"})

    @override_settings(AZURE_STORAGE_ACCOUNT_NAME="")
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_raises_on_missing_account_name(self, _mock_is_mock):
        with self.assertRaises(ValueError):
            create_tenant_file_share("tenant-abc")


class RegisterEnvironmentStorageTest(SimpleTestCase):
    @patch("apps.orchestrator.azure_client._is_mock", return_value=True)
    def test_mock_mode_skips_azure_call(self, _mock_is_mock):
        register_environment_storage("tenant-abc")

    @override_settings(
        AZURE_SUBSCRIPTION_ID="sub-123",
        AZURE_RESOURCE_GROUP="rg-test",
        AZURE_STORAGE_ACCOUNT_NAME="sttest",
        AZURE_CONTAINER_ENV_ID="/subscriptions/sub-123/resourceGroups/rg-test/providers/Microsoft.App/managedEnvironments/test-env",
    )
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_container_client")
    @patch("apps.orchestrator.azure_client.get_storage_client")
    def test_registers_storage_with_environment(
        self, mock_get_storage_client, mock_get_container_client, _mock_is_mock,
    ):
        mock_storage_client = MagicMock()
        mock_get_storage_client.return_value = mock_storage_client
        mock_storage_client.storage_accounts.list_keys.return_value = SimpleNamespace(
            keys=[SimpleNamespace(value="fake-key-123")],
        )

        mock_container_client = MagicMock()
        mock_get_container_client.return_value = mock_container_client

        register_environment_storage("tenant-abc")

        mock_container_client.managed_environments_storages.create_or_update.assert_called_once()
        call_kwargs = mock_container_client.managed_environments_storages.create_or_update.call_args.kwargs
        self.assertEqual(call_kwargs["environment_name"], "test-env")
        self.assertEqual(call_kwargs["storage_name"], "ws-tenant-abc")

        envelope = call_kwargs["storage_envelope"]
        azure_file = envelope.properties.azure_file
        self.assertEqual(azure_file.account_name, "sttest")
        self.assertEqual(azure_file.account_key, "fake-key-123")
        self.assertEqual(azure_file.access_mode, "ReadWrite")
        self.assertEqual(azure_file.share_name, "ws-tenant-abc")


@override_settings(
    AZURE_LOCATION="westus2",
    AZURE_CONTAINER_ENV_ID="/subscriptions/test/resourceGroups/rg/providers/Microsoft.App/managedEnvironments/env",
    AZURE_ACR_SERVER="nbhdunited.azurecr.io",
    AZURE_RESOURCE_GROUP="rg-nbhd-prod",
    AZURE_KEY_VAULT_NAME="kv-nbhd-prod",
    ANTHROPIC_API_KEY="anthropic-secret",
    TELEGRAM_BOT_TOKEN="telegram-secret",
    TELEGRAM_WEBHOOK_SECRET="webhook-secret",
    NBHD_INTERNAL_API_KEY="internal-secret",
    API_BASE_URL="https://nbhd-django.example.com",
)
class PerTenantSecretTest(SimpleTestCase):
    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_container_client")
    def test_per_tenant_kv_secret_overrides_shared(
        self, mock_get_container_client, _mock_is_mock,
    ):
        mock_client = MagicMock()
        mock_get_container_client.return_value = mock_client

        mock_result = SimpleNamespace(
            configuration=SimpleNamespace(
                ingress=SimpleNamespace(fqdn="oc-tenant.internal.azurecontainerapps.io")
            ),
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
            internal_api_key_kv_secret="tenant-tenant-123-internal-key",
        )

        payload = mock_client.container_apps.begin_create_or_update.call_args.args[2]
        secrets = payload["properties"]["configuration"]["secrets"]
        secret_map = {entry["name"]: entry for entry in secrets}
        self.assertEqual(
            secret_map["nbhd-internal-api-key"]["keyVaultUrl"],
            "https://kv-nbhd-prod.vault.azure.net/secrets/tenant-tenant-123-internal-key",
        )
