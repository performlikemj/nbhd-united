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

    def test_write_secret_recovers_from_soft_delete(self):
        """`set_secret` 409 with ObjectIsDeletedButRecoverable → recover, retry.

        Regression guard for the disconnect/reconnect cycle. KV soft-delete
        retains a deleted secret name for 7-90 days; without this recovery,
        any reconnect within the retention window fails with 409.
        """
        from unittest.mock import MagicMock

        from azure.core.exceptions import ResourceExistsError

        from apps.byo_models import services as svc

        # Run with real (non-mock) path so ResourceExistsError handling fires.
        os.environ["AZURE_MOCK"] = "false"
        try:
            mock_client = MagicMock()
            calls = {"n": 0}

            def set_secret_side_effect(name, value):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ResourceExistsError(
                        message="(Conflict) ObjectIsDeletedButRecoverable: secret is currently in a deleted but recoverable state"
                    )
                return MagicMock()

            mock_client.set_secret.side_effect = set_secret_side_effect
            recover_poller = MagicMock()
            mock_client.begin_recover_deleted_secret.return_value = recover_poller

            with (
                patch("azure.keyvault.secrets.SecretClient", return_value=mock_client),
                patch(
                    "apps.orchestrator.azure_client._get_provisioner_credential",
                    return_value=MagicMock(),
                ),
            ):
                svc._write_secret_to_kv("tenants-test-byo-anthropic-cli-subscription", "tok-newvalue")

            self.assertEqual(calls["n"], 2, "set_secret should be retried once after recovery")
            mock_client.begin_recover_deleted_secret.assert_called_once_with(
                "tenants-test-byo-anthropic-cli-subscription"
            )
            recover_poller.wait.assert_called_once()
        finally:
            os.environ["AZURE_MOCK"] = "true"

    def test_write_secret_reraises_other_resource_exists_errors(self):
        """`ResourceExistsError` without ObjectIsDeletedButRecoverable bubbles up.

        Don't silently retry on conflict reasons we don't understand.
        """
        from unittest.mock import MagicMock

        from azure.core.exceptions import ResourceExistsError

        from apps.byo_models import services as svc

        os.environ["AZURE_MOCK"] = "false"
        try:
            mock_client = MagicMock()
            mock_client.set_secret.side_effect = ResourceExistsError(message="(Conflict) SomeOtherReason")

            with (
                patch("azure.keyvault.secrets.SecretClient", return_value=mock_client),
                patch(
                    "apps.orchestrator.azure_client._get_provisioner_credential",
                    return_value=MagicMock(),
                ),
                self.assertRaises(ResourceExistsError),
            ):
                svc._write_secret_to_kv("tenants-test-byo-anthropic-cli-subscription", "tok-newvalue")

            mock_client.begin_recover_deleted_secret.assert_not_called()
        finally:
            os.environ["AZURE_MOCK"] = "true"


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

    def test_post_regenerates_config_before_revision(self):
        """Regression guard: openclaw.json must be regenerated BEFORE the
        env reconciliation creates a new revision. Otherwise the new
        revision boots into an inconsistent state — agentRuntime not yet
        set, ANTHROPIC_API_KEY removed — and Anthropic calls hang.
        """
        self._enable_flag()
        call_order: list[str] = []
        with (
            patch(
                "apps.byo_models.views.regenerate_tenant_config",
                side_effect=lambda *_a, **_kw: call_order.append("regenerate"),
            ),
            patch(
                "apps.byo_models.views.apply_byo_credentials_to_container",
                side_effect=lambda *_a, **_kw: call_order.append("apply_byo"),
            ),
        ):
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
        self.assertEqual(call_order, ["regenerate", "apply_byo"])

    def test_delete_regenerates_config_before_revision(self):
        """Same coupling on disconnect — config regen first, then env+revision."""
        self._enable_flag()
        cred = upsert_credential(self.tenant, "anthropic", "cli_subscription", "tok-deleteorder-abcdefghijkl12345")
        call_order: list[str] = []
        with (
            patch(
                "apps.byo_models.views.regenerate_tenant_config",
                side_effect=lambda *_a, **_kw: call_order.append("regenerate"),
            ),
            patch(
                "apps.byo_models.views.apply_byo_credentials_to_container",
                side_effect=lambda *_a, **_kw: call_order.append("apply_byo"),
            ),
        ):
            response = self.client.delete(f"/api/v1/tenants/byo-credentials/{cred.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(call_order, ["regenerate", "apply_byo"])

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
    """Tests for `_byo_model_extras` integration in `generate_openclaw_config`.

    Routing note: with PR #421, BYO Anthropic CLI routing is activated by an
    OpenClaw auth profile (registered at container boot by `entrypoint.sh`),
    NOT by the model prefix and NOT by `agentRuntime`/`cliBackends` config
    entries. So this generator's job is simply to expose the canonical
    `anthropic/<model>` ids in the model registry when an active credential
    exists.
    """

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
        self.assertNotIn(ANTHROPIC_SONNET_MODEL, cfg["agents"]["defaults"]["models"])

    def test_no_byo_models_when_no_cred(self):
        self.tenant.byo_models_enabled = True
        self.tenant.preferred_model = ANTHROPIC_SONNET_MODEL
        self.tenant.save(update_fields=["byo_models_enabled", "preferred_model"])
        cfg = self._generate()
        self.assertNotIn(ANTHROPIC_SONNET_MODEL, cfg["agents"]["defaults"]["models"])

    def test_byo_models_registered_when_cred_active(self):
        from apps.billing.constants import ANTHROPIC_OPUS_MODEL

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
        self.assertEqual(cfg["agents"]["defaults"]["model"]["primary"], ANTHROPIC_SONNET_MODEL)
        # Both Sonnet and Opus exposed so the picker can offer either.
        self.assertIn(ANTHROPIC_SONNET_MODEL, cfg["agents"]["defaults"]["models"])
        self.assertIn(ANTHROPIC_OPUS_MODEL, cfg["agents"]["defaults"]["models"])
        # cliBackends.claude-cli.command points at our wrapper so the spawned
        # `claude` binary gets CLAUDE_CODE_OAUTH_TOKEN restored (OpenClaw's
        # CLI backend strips that env var before spawn).
        self.assertEqual(
            cfg["agents"]["defaults"]["cliBackends"]["claude-cli"],
            {"command": "/opt/nbhd/claude-with-token.sh"},
        )
        # We don't set agentRuntime — auth profile drives runtime selection.
        self.assertNotIn("agentRuntime", cfg["agents"]["defaults"])

    def test_byo_anthropic_model_ids_use_canonical_prefix(self):
        """Regression guard for PR #421: the BYO Anthropic model ids must
        use the canonical `anthropic/<model>` prefix.

        OpenClaw 2026.4.25's model registry has no `anthropic-cli/...`
        prefix (that string is a UI choiceId only — see
        `dist/extensions/anthropic/openclaw.plugin.json`). Using
        `anthropic-cli/...` produces "Unknown model" + fallback to
        MiniMax — the bug PR #419 introduced.
        """
        from apps.billing.constants import ANTHROPIC_OPUS_MODEL

        for model_id in (ANTHROPIC_SONNET_MODEL, ANTHROPIC_OPUS_MODEL):
            self.assertTrue(
                model_id.startswith("anthropic/"),
                f"BYO Anthropic model id must start with 'anthropic/' but got {model_id!r}",
            )

    def test_error_status_excluded_from_byo_extras(self):
        from apps.billing.constants import ANTHROPIC_OPUS_MODEL

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
        self.assertNotIn(ANTHROPIC_SONNET_MODEL, cfg["agents"]["defaults"]["models"])
        self.assertNotIn(ANTHROPIC_OPUS_MODEL, cfg["agents"]["defaults"]["models"])

    def test_byo_primary_disables_fallbacks_to_avoid_silent_swap(self):
        """When the resolved primary is a BYO model, `fallbacks` must be
        empty so OpenClaw raises the original billing/auth error instead
        of silently swapping to a non-BYO model. The user paid Anthropic
        specifically for Claude — getting a MiniMax answer would feel
        broken.
        """
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
        self.assertEqual(cfg["agents"]["defaults"]["model"]["primary"], ANTHROPIC_SONNET_MODEL)
        self.assertEqual(
            cfg["agents"]["defaults"]["model"]["fallbacks"],
            [],
            "BYO primary must have an empty fallbacks list to surface billing errors",
        )

    def test_non_byo_primary_keeps_fallbacks_populated(self):
        """Tier default (MiniMax) is not a BYO model — `fallbacks` should
        still expose the rest of the tier's allowed models so
        rate-limit/overload on the cheap model still falls through.
        """
        from apps.billing.constants import GEMMA_MODEL, KIMI_MODEL, MINIMAX_MODEL

        # No preferred_model override — primary stays at tier default.
        cfg = self._generate()
        self.assertEqual(cfg["agents"]["defaults"]["model"]["primary"], MINIMAX_MODEL)
        fallbacks = cfg["agents"]["defaults"]["model"]["fallbacks"]
        self.assertIn(KIMI_MODEL, fallbacks)
        self.assertIn(GEMMA_MODEL, fallbacks)


class BYOMarkCredentialErrorTest(TestCase):
    """Tests for `mark_credential_error` service helper."""

    def setUp(self):
        self.tenant = create_tenant(display_name="MarkErrTest", telegram_chat_id=10006)

    def test_flips_verified_credential_to_error_with_message(self):
        from apps.byo_models.services import mark_credential_error

        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.VERIFIED,
        )
        cred = mark_credential_error(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            last_error="Your Claude account is out of extra usage.",
        )
        self.assertIsNotNone(cred)
        self.assertEqual(cred.status, BYOCredential.Status.ERROR)
        self.assertIn("out of extra usage", cred.last_error)

    def test_returns_none_when_no_credential_exists(self):
        from apps.byo_models.services import mark_credential_error

        cred = mark_credential_error(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            last_error="boom",
        )
        self.assertIsNone(cred)

    def test_skips_pending_credentials(self):
        # Pending creds shouldn't get flipped to error — they haven't
        # finished their first verification.
        from apps.byo_models.services import mark_credential_error

        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.PENDING,
        )
        cred = mark_credential_error(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            last_error="boom",
        )
        self.assertIsNone(cred)

    def test_truncates_overlong_messages(self):
        from apps.byo_models.services import mark_credential_error

        BYOCredential.objects.create(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
            key_vault_secret_name="x",
            status=BYOCredential.Status.VERIFIED,
        )
        long_msg = "x" * 1000
        cred = mark_credential_error(
            tenant=self.tenant,
            provider=BYOCredential.Provider.ANTHROPIC,
            last_error=long_msg,
        )
        self.assertIsNotNone(cred)
        self.assertLessEqual(len(cred.last_error), 240)


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
