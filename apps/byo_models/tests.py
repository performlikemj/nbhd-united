"""Tests for BYO subscription credentials.

All KV/Azure SDK calls are exercised in mock mode (`AZURE_MOCK=true`)
via `@override_settings(AZURE_MOCK=True)` at the env-var level — the
helpers in `apps.byo_models.services` and
`apps.orchestrator.azure_client` check `AZURE_MOCK` directly.
"""

from __future__ import annotations

import os
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.constants import ANTHROPIC_SONNET_MODEL
from apps.byo_models.models import BYOCredential
from apps.byo_models.services import (
    _BYO_MOCK_KV_STORE,
    delete_credential,
    secret_name_for,
    upsert_credential,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _mock_env() -> dict[str, str]:
    """Env vars to set during tests so KV/Azure helpers run in mock mode."""
    return {"AZURE_MOCK": "true"}


class BYOCredentialModelTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="ModelTest", telegram_chat_id=10001)

    def test_unique_constraint_one_credential_per_provider(self):
        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="tenants-X-byo-anthropic-cli_subscription",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            BYOCredential.objects.create(
                tenant=self.tenant,
                provider=BYOCredential.Provider.ANTHROPIC,
                mode=BYOCredential.Mode.API_KEY,
                key_vault_secret_name="tenants-X-byo-anthropic-api_key",
            )

    def test_status_choices_accept_all_four(self):
        for status_value in (
            BYOCredential.Status.PENDING,
            BYOCredential.Status.VERIFIED,
            BYOCredential.Status.EXPIRED,
            BYOCredential.Status.ERROR,
        ):
            cred = BYOCredential(
                tenant=self.tenant,
                provider=BYOCredential.Provider.ANTHROPIC,
                mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
                key_vault_secret_name="x",
                status=status_value,
            )
            cred.full_clean(exclude=["tenant"])  # should not raise

    def test_seed_version_default_zero(self):
        cred = BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.OPENAI,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
        )
        self.assertEqual(cred.seed_version, 0)


class BYOServiceTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="SvcTest", telegram_chat_id=10002)
        os.environ["AZURE_MOCK"] = "true"
        _BYO_MOCK_KV_STORE.clear()

    def tearDown(self):
        os.environ.pop("AZURE_MOCK", None)
        _BYO_MOCK_KV_STORE.clear()

    def test_secret_name_for_returns_expected_format(self):
        name = secret_name_for(self.tenant, "anthropic", "cli_subscription")
        self.assertTrue(name.startswith(self.tenant.key_vault_prefix))
        # Underscore in `cli_subscription` is sanitized to a hyphen because
        # Azure Key Vault names must match ^[0-9a-zA-Z-]+$.
        self.assertIn("byo-anthropic-cli-subscription", name)
        self.assertNotIn("_", name)

    def test_secret_name_for_matches_kv_naming_regex(self):
        """Azure Key Vault rejects secret names outside ^[0-9a-zA-Z-]+$.

        Regression guard for the underscore bug shipped in initial PR #2:
        Mode.CLI_SUBSCRIPTION = "cli_subscription" was concatenated raw
        into the secret name, producing 502 BadParameter from KV.
        """
        import re

        kv_pattern = re.compile(r"^[0-9a-zA-Z-]+$")
        for provider, mode in [
            ("anthropic", "cli_subscription"),
            ("anthropic", "api_key"),
            ("openai", "cli_subscription"),
            ("openai", "api_key"),
        ]:
            name = secret_name_for(self.tenant, provider, mode)
            self.assertRegex(name, kv_pattern, f"KV-illegal name for {provider}/{mode}: {name}")

    def test_secret_name_for_raises_when_no_prefix(self):
        self.tenant.key_vault_prefix = ""
        self.tenant.save(update_fields=["key_vault_prefix"])
        with self.assertRaises(ValueError):
            secret_name_for(self.tenant, "anthropic", "cli_subscription")

    def test_upsert_creates_row_and_writes_kv(self):
        cred = upsert_credential(self.tenant, "anthropic", "cli_subscription", "tok-abcdefghijklmnopqrstuvwxyz12345")
        self.assertEqual(cred.tenant_id, self.tenant.id)
        self.assertEqual(cred.provider, BYOCredential.Provider.ANTHROPIC)
        self.assertEqual(cred.status, BYOCredential.Status.PENDING)
        self.assertEqual(cred.seed_version, 1)
        # KV mock store has the value
        self.assertIn(cred.key_vault_secret_name, _BYO_MOCK_KV_STORE)
        self.assertEqual(_BYO_MOCK_KV_STORE[cred.key_vault_secret_name], "tok-abcdefghijklmnopqrstuvwxyz12345")

    def test_upsert_second_call_updates_same_row(self):
        c1 = upsert_credential(self.tenant, "anthropic", "cli_subscription", "tok-firstpasted-abcdefghijk12345")
        c2 = upsert_credential(self.tenant, "anthropic", "cli_subscription", "tok-secondpasted-abcdefghij12345")
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(c2.seed_version, 2)
        # KV value reflects the latest token
        self.assertEqual(_BYO_MOCK_KV_STORE[c2.key_vault_secret_name], "tok-secondpasted-abcdefghij12345")

    def test_delete_removes_row_and_kv(self):
        cred = upsert_credential(self.tenant, "anthropic", "cli_subscription", "tok-abcdefghijklmnopqrstuvwxyz12345")
        secret_name = cred.key_vault_secret_name
        delete_credential(cred)
        self.assertFalse(BYOCredential.objects.filter(id=cred.id).exists())
        self.assertNotIn(secret_name, _BYO_MOCK_KV_STORE)


class BYOEndpointTest(TestCase):
    """Integration tests for the REST endpoints — feature-flag gate,
    token validation, no-token-leak in errors."""

    def setUp(self):
        self.tenant = create_tenant(display_name="EndpointTest", telegram_chat_id=10003)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        os.environ["AZURE_MOCK"] = "true"
        _BYO_MOCK_KV_STORE.clear()

    def tearDown(self):
        os.environ.pop("AZURE_MOCK", None)
        _BYO_MOCK_KV_STORE.clear()

    def _enable_flag(self):
        self.tenant.byo_models_enabled = True
        self.tenant.save(update_fields=["byo_models_enabled"])

    def test_post_returns_404_when_flag_off(self):
        response = self.client.post(
            "/api/v1/tenants/byo-credentials/",
            {
                "provider": "anthropic",
                "mode": "cli_subscription",
                "token": "tok-abcdefghijklmnopqrstuvwxyz12345",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_post_succeeds_when_flag_on(self):
        self._enable_flag()
        response = self.client.post(
            "/api/v1/tenants/byo-credentials/",
            {
                "provider": "anthropic",
                "mode": "cli_subscription",
                "token": "tok-abcdefghijklmnopqrstuvwxyz12345",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertEqual(data["provider"], "anthropic")
        self.assertEqual(data["mode"], "cli_subscription")
        self.assertEqual(data["status"], "pending")

    def test_post_rejects_short_token(self):
        self._enable_flag()
        response = self.client.post(
            "/api/v1/tenants/byo-credentials/",
            {"provider": "anthropic", "mode": "cli_subscription", "token": "short"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_post_rejects_unsupported_provider_mode(self):
        self._enable_flag()
        # OpenAI is not yet supported in Phase 1
        response = self.client.post(
            "/api/v1/tenants/byo-credentials/",
            {
                "provider": "openai",
                "mode": "cli_subscription",
                "token": "tok-abcdefghijklmnopqrstuvwxyz12345",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_post_does_not_echo_token_on_kv_failure(self):
        """When upsert_credential raises, the token must not appear in
        the response body."""
        self._enable_flag()
        token = "tok-secret-must-not-leak-1234567890"
        with patch(
            "apps.byo_models.views.upsert_credential",
            side_effect=RuntimeError("simulated KV outage"),
        ):
            response = self.client.post(
                "/api/v1/tenants/byo-credentials/",
                {"provider": "anthropic", "mode": "cli_subscription", "token": token},
                format="json",
            )
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(token, response.content.decode())

    def test_list_returns_only_serialized_fields_no_token(self):
        self._enable_flag()
        token = "tok-listendpoint-must-not-leak-12"
        upsert_credential(self.tenant, "anthropic", "cli_subscription", token)
        response = self.client.get("/api/v1/tenants/byo-credentials/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn(token, body)
        self.assertNotIn("key_vault_secret_name", body)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["provider"], "anthropic")

    def test_delete_removes_row_and_returns_204(self):
        self._enable_flag()
        cred = upsert_credential(self.tenant, "anthropic", "cli_subscription", "tok-deletetest-abcdefghijkl12345")
        response = self.client.delete(f"/api/v1/tenants/byo-credentials/{cred.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(BYOCredential.objects.filter(id=cred.id).exists())

    def test_delete_returns_404_when_flag_off(self):
        cred = BYOCredential.objects.create(
            tenant=self.tenant,
            provider="anthropic",
            mode="cli_subscription",
            key_vault_secret_name="x",
        )
        response = self.client.delete(f"/api/v1/tenants/byo-credentials/{cred.id}/")
        self.assertEqual(response.status_code, 404)

    def test_endpoints_require_authentication(self):
        anon = APIClient()
        for method, path in (
            ("get", "/api/v1/tenants/byo-credentials/"),
            ("post", "/api/v1/tenants/byo-credentials/"),
            ("delete", "/api/v1/tenants/byo-credentials/00000000-0000-0000-0000-000000000000/"),
        ):
            response = getattr(anon, method)(path)
            self.assertEqual(response.status_code, 401, f"{method} {path}")


class BYOConfigGeneratorTest(TestCase):
    """Tests for the agentRuntime + model_entries extension in
    `generate_openclaw_config`."""

    def setUp(self):
        self.tenant = create_tenant(display_name="CfgTest", telegram_chat_id=10004)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status"])

    def _generate(self) -> dict:
        from apps.orchestrator.config_generator import generate_openclaw_config

        return generate_openclaw_config(self.tenant)

    def test_no_byo_when_flag_off(self):
        # Even with a verified cred, byo_models_enabled=False keeps things off
        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.VERIFIED,
        )
        cfg = self._generate()
        self.assertNotIn("agentRuntime", cfg["agents"]["defaults"])
        self.assertNotIn(ANTHROPIC_SONNET_MODEL, cfg["agents"]["defaults"]["models"])

    def test_no_agent_runtime_when_no_cred(self):
        self.tenant.byo_models_enabled = True
        self.tenant.preferred_model = ANTHROPIC_SONNET_MODEL
        self.tenant.save(update_fields=["byo_models_enabled", "preferred_model"])
        cfg = self._generate()
        self.assertNotIn("agentRuntime", cfg["agents"]["defaults"])

    def test_agent_runtime_set_when_cred_active_and_anthropic_primary(self):
        self.tenant.byo_models_enabled = True
        self.tenant.preferred_model = ANTHROPIC_SONNET_MODEL
        self.tenant.save(update_fields=["byo_models_enabled", "preferred_model"])
        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.VERIFIED,
        )
        cfg = self._generate()
        self.assertEqual(cfg["agents"]["defaults"]["agentRuntime"], {"id": "claude-cli"})
        self.assertEqual(cfg["agents"]["defaults"]["model"]["primary"], ANTHROPIC_SONNET_MODEL)
        self.assertIn(ANTHROPIC_SONNET_MODEL, cfg["agents"]["defaults"]["models"])

    def test_no_agent_runtime_when_primary_not_anthropic(self):
        # BYO Anthropic cred connected, but tenant has selected an OpenRouter
        # model — no claude-cli runtime.
        self.tenant.byo_models_enabled = True
        self.tenant.save(update_fields=["byo_models_enabled"])
        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.VERIFIED,
        )
        cfg = self._generate()
        self.assertNotIn("agentRuntime", cfg["agents"]["defaults"])

    def test_error_status_excluded_from_byo_extras(self):
        self.tenant.byo_models_enabled = True
        self.tenant.preferred_model = ANTHROPIC_SONNET_MODEL
        self.tenant.save(update_fields=["byo_models_enabled", "preferred_model"])
        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.ERROR,
        )
        cfg = self._generate()
        self.assertNotIn("agentRuntime", cfg["agents"]["defaults"])
        self.assertNotIn(ANTHROPIC_SONNET_MODEL, cfg["agents"]["defaults"]["models"])


class EnableByoCommandTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="CmdTest", telegram_chat_id=10005)

    def test_enable_flips_flag(self):
        self.assertFalse(self.tenant.byo_models_enabled)
        out = StringIO()
        call_command("enable_byo", "--tenant", str(self.tenant.id), stdout=out)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.byo_models_enabled)
        self.assertIn("enabled", out.getvalue())

    def test_disable_flips_flag(self):
        self.tenant.byo_models_enabled = True
        self.tenant.save(update_fields=["byo_models_enabled"])
        out = StringIO()
        call_command("enable_byo", "--tenant", str(self.tenant.id), "--disable", stdout=out)
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.byo_models_enabled)
        self.assertIn("disabled", out.getvalue())

    def test_unknown_tenant_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command("enable_byo", "--tenant", "00000000-0000-0000-0000-000000000000")

    def test_no_op_when_already_in_target_state(self):
        out = StringIO()
        call_command("enable_byo", "--tenant", str(self.tenant.id), "--disable", stdout=out)
        self.assertIn("no-op", out.getvalue())
