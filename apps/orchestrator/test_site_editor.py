"""KSE-1 contract tests for the tenant-gated GitHub site editor."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.orchestrator.azure_client import ensure_site_editor_secret, site_editor_kv_secret_name
from apps.orchestrator.config_generator import BOOTSTRAP_MAX_CHARS, generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.services import create_tenant

_GATE = "## Website edit gate"
_PORTFOLIO_GATE = "## Portfolio publish gate"
_GRAVITY_GATE = "## Gravity Observation Mode"


class SiteEditorGateAndConfigTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Site Editor", telegram_chat_id=940001)

    def test_gate_renders_only_when_enabled(self):
        off = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
        self.assertNotIn(_GATE, off)

        self.tenant.site_editor_enabled = True
        self.tenant.save(update_fields=["site_editor_enabled"])
        on = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
        self.assertIn(_GATE, on)
        self.assertIn("Every time: `site_read_file` first", on)
        self.assertIn("`site_show_pending` returns an approval code", on)
        self.assertIn("call `site_publish` with that code", on)
        self.assertIn("ONLY after they say go", on)
        self.assertIn("returned a commit THIS turn", on)
        expected = (
            (
                Path(__file__).resolve().parents[2]
                / "runtime"
                / "openclaw"
                / "plugins"
                / "nbhd-site-editor"
                / "agents-section.md"
            )
            .read_text()
            .strip()
        )
        self.assertTrue(on.endswith(expected))

    def test_plugin_load_and_config_injection_are_flagged_whitelisted_and_typed(self):
        off = generate_openclaw_config(self.tenant)
        self.assertNotIn("nbhd-site-editor", off.get("plugins", {}).get("entries", {}))

        self.tenant.site_editor_enabled = True
        self.tenant.site_editor_config = {
            "owner": "performlikemj",
            "repo": "kihoko",
            "branch": "main",
            "allowPaths": ["web/src/pages/AboutPage.js"],
            "denyPaths": ["safe", 9],
            "maxTextBytes": True,
            "maxImageBytes": "large",
            "maxFiles": 7,
            "maxTotalBytes": 12345,
            "deployMinutes": 6,
            "authorEmail": "site-editor@example.invalid",
            "siteNotes": "Home page hero = web/public/hero.jpg.",
            "garbage": "drop-me",
        }
        self.tenant.save(update_fields=["site_editor_enabled", "site_editor_config"])

        config = generate_openclaw_config(self.tenant)
        plugins = config["plugins"]
        self.assertIn("nbhd-site-editor", plugins["allow"])
        self.assertIn("/opt/nbhd/plugins/nbhd-site-editor", plugins["load"]["paths"])
        self.assertEqual(
            plugins["entries"]["nbhd-site-editor"]["config"],
            {
                "owner": "performlikemj",
                "repo": "kihoko",
                "branch": "main",
                "allowPaths": ["web/src/pages/AboutPage.js"],
                "maxFiles": 7,
                "maxTotalBytes": 12345,
                "deployMinutes": 6,
                "authorEmail": "site-editor@example.invalid",
                "siteNotes": "Home page hero = web/public/hero.jpg.",
            },
        )

    def test_site_notes_over_limit_is_not_injected(self):
        self.tenant.site_editor_enabled = True
        self.tenant.site_editor_config = {
            "owner": "performlikemj",
            "repo": "kihoko",
            "branch": "main",
            "allowPaths": ["web/src/pages/AboutPage.js"],
            "siteNotes": "x" * 1001,
        }
        self.tenant.save(update_fields=["site_editor_enabled", "site_editor_config"])

        config = generate_openclaw_config(self.tenant)

        self.assertNotIn("siteNotes", config["plugins"]["entries"]["nbhd-site-editor"]["config"])

    def test_flagged_generated_config_passes_write_validator(self):
        self.tenant.site_editor_enabled = True
        self.tenant.site_editor_config = {
            "owner": "performlikemj",
            "repo": "kihoko",
            "branch": "main",
            "allowPaths": ["web/src/pages/AboutPage.js"],
            "denyPaths": [".github/**"],
            "maxTextBytes": 262144,
            "maxImageBytes": 2097152,
            "maxFiles": 20,
            "maxTotalBytes": 5242880,
            "deployMinutes": 6,
            "authorEmail": "site-editor@example.invalid",
        }
        self.tenant.save(update_fields=["site_editor_enabled", "site_editor_config"])
        config = generate_openclaw_config(self.tenant)
        assert_config_writable(config)
        self.assertIn("nbhd-site-editor", config["plugins"]["entries"])


@override_settings(
    AZURE_KEY_VAULT_NAME="kv-test",
    AZURE_RESOURCE_GROUP="rg-test",
    OPENCLAW_CONTAINER_SECRET_BACKEND="keyvault",
)
class SiteEditorSecretReconcileTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Secret Tenant", telegram_chat_id=940002)
        self.tenant.container_id = "oc-test"
        self.tenant.managed_identity_id = "/subscriptions/test/mi-test"
        self.tenant.key_vault_prefix = "tenant-test"
        self.openclaw = SimpleNamespace(name="openclaw", env=[])
        self.sidecar = SimpleNamespace(name="sidecar", env=[{"name": "UNCHANGED", "value": "yes"}])
        self.app = SimpleNamespace(
            configuration=SimpleNamespace(secrets=[]),
            template=SimpleNamespace(containers=[self.openclaw, self.sidecar], revision_suffix="old"),
        )
        self.client = MagicMock()
        self.client.container_apps.get.return_value = self.app
        self.client.container_apps.begin_create_or_update.return_value.result.return_value = None

    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_container_client")
    def test_reconciles_both_directions_and_is_idempotent(self, get_client, _is_mock):
        get_client.return_value = self.client

        self.assertTrue(ensure_site_editor_secret(self.tenant, enabled=True))
        self.assertEqual(
            self.app.configuration.secrets,
            [
                {
                    "name": "github-site-token",
                    "keyVaultUrl": "https://kv-test.vault.azure.net/secrets/tenant-test-github-site-token",
                    "identity": "/subscriptions/test/mi-test",
                }
            ],
        )
        self.assertEqual(
            self.openclaw.env,
            [{"name": "NBHD_SITE_GITHUB_TOKEN", "secretRef": "github-site-token"}],
        )
        self.assertEqual(self.sidecar.env, [{"name": "UNCHANGED", "value": "yes"}])
        self.assertFalse(ensure_site_editor_secret(self.tenant, enabled=True))
        self.assertEqual(self.client.container_apps.begin_create_or_update.call_count, 1)

        self.assertTrue(ensure_site_editor_secret(self.tenant, enabled=True, force_revision=True))
        self.assertEqual(self.client.container_apps.begin_create_or_update.call_count, 2)

        self.assertTrue(ensure_site_editor_secret(self.tenant, enabled=False))
        self.assertEqual(self.app.configuration.secrets, [])
        self.assertEqual(self.openclaw.env, [])
        self.assertFalse(ensure_site_editor_secret(self.tenant, enabled=False))
        self.assertEqual(self.client.container_apps.begin_create_or_update.call_count, 3)

    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    @patch("apps.orchestrator.azure_client.get_container_client")
    def test_sdk_shaped_normalized_secret_is_idempotent(self, get_client, _is_mock):
        self.app.configuration.secrets = [
            SimpleNamespace(
                name="github-site-token",
                key_vault_url="HTTPS://KV-TEST.VAULT.AZURE.NET/SECRETS/TENANT-TEST-GITHUB-SITE-TOKEN",
                identity="/SUBSCRIPTIONS/TEST/MI-TEST",
            )
        ]
        self.openclaw.env = [SimpleNamespace(name="NBHD_SITE_GITHUB_TOKEN", secret_ref="github-site-token")]
        get_client.return_value = self.client

        self.assertFalse(ensure_site_editor_secret(self.tenant, enabled=True))
        self.client.container_apps.begin_create_or_update.assert_not_called()

    @patch("apps.orchestrator.azure_client._is_mock", return_value=False)
    def test_missing_container_id_and_openclaw_raise(self, _is_mock):
        self.tenant.container_id = ""
        with self.assertRaisesRegex(ValueError, "no container_id"):
            ensure_site_editor_secret(self.tenant, enabled=True)

        self.tenant.container_id = "oc-test"
        self.app.template.containers = [self.sidecar]
        with (
            patch("apps.orchestrator.azure_client.get_container_client", return_value=self.client),
            self.assertRaisesRegex(ValueError, "no openclaw container"),
        ):
            ensure_site_editor_secret(self.tenant, enabled=True)

    @patch("apps.orchestrator.azure_client._is_mock", return_value=True)
    def test_mock_mode_does_not_claim_a_new_revision(self, _is_mock):
        self.assertFalse(ensure_site_editor_secret(self.tenant, enabled=True))


class SiteEditorManagementCommandTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Command Tenant", telegram_chat_id=940003)
        self.tenant.key_vault_prefix = "tenant-command"
        self.tenant.container_id = "oc-command"
        self.tenant.managed_identity_id = "/subscriptions/test/resourceGroups/rg-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/mi-command"
        self.tenant.save(update_fields=["key_vault_prefix", "container_id", "managed_identity_id"])
        self.temp_paths: list[Path] = []

    def tearDown(self):
        for temp_path in self.temp_paths:
            temp_path.unlink(missing_ok=True)

    def config_file(self, value: object) -> str:
        with NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
            json.dump(value, handle)
            path = Path(handle.name)
        self.temp_paths.append(path)
        return str(path)

    @override_settings(AZURE_RESOURCE_GROUP="rg-test")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.is_mock", return_value=False)
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.get_identity_client")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.assign_key_vault_role")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.ensure_site_editor_secret")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret._write_secret_to_kv")
    def test_secret_orders_write_grant_and_forced_reconcile(
        self, write_secret, ensure_secret, assign_role, get_identity, _is_mock
    ):
        events = []
        write_secret.side_effect = lambda *args, **kwargs: events.append("write")
        assign_role.side_effect = lambda *args, **kwargs: events.append("grant")
        ensure_secret.side_effect = lambda *args, **kwargs: events.append("reconcile")
        get_identity.return_value.user_assigned_identities.get.return_value.principal_id = "principal-123"
        output = StringIO()
        with patch(
            "apps.orchestrator.management.commands.set_site_editor_secret.sys.stdin",
            StringIO("github_pat_FAKE_TEST_VALUE\n"),
        ):
            call_command(
                "set_site_editor_secret",
                "--tenant-id",
                str(self.tenant.id),
                "--from-stdin",
                stdout=output,
            )
        write_secret.assert_called_once_with(
            "tenant-command-github-site-token",
            "github_pat_FAKE_TEST_VALUE",
            log_label="site-editor",
        )
        get_identity.return_value.user_assigned_identities.get.assert_called_once_with(
            resource_group_name="rg-test",
            resource_name="mi-command",
        )
        assign_role.assert_called_once_with(
            "principal-123",
            secret_names=["tenant-command-github-site-token"],
        )
        ensure_secret.assert_called_once_with(self.tenant, enabled=True, force_revision=True)
        self.assertEqual(events, ["write", "grant", "reconcile"])
        self.assertEqual(output.getvalue(), "ok\n")

    @override_settings(AZURE_RESOURCE_GROUP="rg-test")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.is_mock", return_value=False)
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.get_identity_client")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.assign_key_vault_role")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.ensure_site_editor_secret")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret._write_secret_to_kv")
    def test_grant_failure_aborts_before_reconcile(
        self, write_secret, ensure_secret, assign_role, get_identity, _is_mock
    ):
        get_identity.return_value.user_assigned_identities.get.return_value.principal_id = "principal-123"
        assign_role.side_effect = RuntimeError("grant failed")
        output = StringIO()
        with (
            patch(
                "apps.orchestrator.management.commands.set_site_editor_secret.sys.stdin",
                StringIO("github_pat_FAKE_TEST_VALUE\n"),
            ),
            self.assertRaisesRegex(CommandError, "failed at grant; value not shown"),
        ):
            call_command(
                "set_site_editor_secret",
                "--tenant-id",
                str(self.tenant.id),
                "--from-stdin",
                stdout=output,
            )
        write_secret.assert_called_once()
        ensure_secret.assert_not_called()
        self.assertNotIn("ok", output.getvalue())

    @patch("apps.orchestrator.management.commands.set_site_editor_secret.is_mock", return_value=True)
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.ensure_site_editor_secret")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret._write_secret_to_kv")
    def test_blank_prefix_is_persisted_and_used_for_secret_name(self, write_secret, ensure_secret, _is_mock):
        self.tenant.key_vault_prefix = ""
        self.tenant.save(update_fields=["key_vault_prefix"])
        output = StringIO()
        with patch(
            "apps.orchestrator.management.commands.set_site_editor_secret.sys.stdin",
            StringIO("github_pat_FAKE_TEST_VALUE\n"),
        ):
            call_command(
                "set_site_editor_secret",
                "--tenant-id",
                str(self.tenant.id),
                "--from-stdin",
                stdout=output,
            )
        self.tenant.refresh_from_db()
        canonical = f"tenants-{self.tenant.user_id}"
        self.assertEqual(self.tenant.key_vault_prefix, canonical)
        self.assertEqual(site_editor_kv_secret_name(self.tenant), f"{canonical}-github-site-token")
        write_secret.assert_called_once_with(
            f"{canonical}-github-site-token",
            "github_pat_FAKE_TEST_VALUE",
            log_label="site-editor",
        )
        ensure_secret.assert_called_once_with(self.tenant, enabled=True, force_revision=True)
        self.assertEqual(output.getvalue(), f"key_vault_prefix={canonical}\nok\n")

    @patch("apps.orchestrator.management.commands.set_site_editor_secret.is_mock", return_value=True)
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.ensure_site_editor_secret")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret._write_secret_to_kv")
    def test_set_prefix_is_unchanged(self, write_secret, ensure_secret, _is_mock):
        with patch(
            "apps.orchestrator.management.commands.set_site_editor_secret.sys.stdin",
            StringIO("github_pat_FAKE_TEST_VALUE\n"),
        ):
            call_command(
                "set_site_editor_secret",
                "--tenant-id",
                str(self.tenant.id),
                "--from-stdin",
                stdout=StringIO(),
            )
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.key_vault_prefix, "tenant-command")

    @patch("apps.orchestrator.management.commands.set_site_editor_secret.ensure_site_editor_secret")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret._write_secret_to_kv")
    def test_secret_refuses_empty_and_embedded_newline(self, write_secret, ensure_secret):
        for supplied in ("   \n", "line-one\nline-two\n"):
            with (
                self.subTest(supplied=repr(supplied)),
                patch(
                    "apps.orchestrator.management.commands.set_site_editor_secret.sys.stdin",
                    StringIO(supplied),
                ),
                self.assertRaises(CommandError),
            ):
                call_command(
                    "set_site_editor_secret",
                    "--tenant-id",
                    str(self.tenant.id),
                    "--from-stdin",
                    stdout=StringIO(),
                )
        write_secret.assert_not_called()
        ensure_secret.assert_not_called()

    @patch("apps.orchestrator.management.commands.set_site_editor_secret.is_mock", return_value=True)
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.ensure_site_editor_secret")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret._write_secret_to_kv")
    def test_write_error_never_echoes_submitted_token(self, write_secret, ensure_secret, _is_mock):
        token = "github_pat_FAKE_SECRET_VALUE"
        write_secret.side_effect = RuntimeError(f"proxy echoed {token}")
        output = StringIO()
        errors = StringIO()
        with (
            patch(
                "apps.orchestrator.management.commands.set_site_editor_secret.sys.stdin",
                StringIO(f"{token}\n"),
            ),
            self.assertRaises(CommandError) as raised,
        ):
            call_command(
                "set_site_editor_secret",
                "--tenant-id",
                str(self.tenant.id),
                "--from-stdin",
                stdout=output,
                stderr=errors,
            )
        combined = output.getvalue() + errors.getvalue() + str(raised.exception)
        self.assertNotIn(token, combined)
        self.assertNotIn("github_pat_", combined)
        self.assertIn("failed at write; value not shown", str(raised.exception))
        ensure_secret.assert_not_called()

    @patch("apps.orchestrator.management.commands.set_site_editor_secret.is_mock", return_value=True)
    @patch("apps.orchestrator.management.commands.set_site_editor_secret.ensure_site_editor_secret")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret._write_secret_to_kv")
    def test_reconcile_failure_does_not_print_ok(self, write_secret, ensure_secret, _is_mock):
        ensure_secret.side_effect = ValueError("Tenant has no container_id")
        output = StringIO()
        with (
            patch(
                "apps.orchestrator.management.commands.set_site_editor_secret.sys.stdin",
                StringIO("github_pat_FAKE_TEST_VALUE\n"),
            ),
            self.assertRaisesRegex(CommandError, "failed at reconcile; value not shown"),
        ):
            call_command(
                "set_site_editor_secret",
                "--tenant-id",
                str(self.tenant.id),
                "--from-stdin",
                stdout=output,
            )
        self.assertNotIn("ok", output.getvalue())

    @patch("apps.orchestrator.management.commands.set_site_editor_secret.ensure_site_editor_secret")
    @patch("apps.orchestrator.management.commands.set_site_editor_secret._write_secret_to_kv")
    def test_secret_clear_removes_binding_but_does_not_touch_kv(self, write_secret, ensure_secret):
        output = StringIO()
        call_command(
            "set_site_editor_secret",
            "--tenant-id",
            str(self.tenant.id),
            "--clear",
            stdout=output,
        )
        write_secret.assert_not_called()
        ensure_secret.assert_called_once_with(self.tenant, enabled=False)
        self.assertEqual(output.getvalue(), "ok\n")

    def test_config_command_validates_saves_flag_and_bumps_pending_version(self):
        before = self.tenant.pending_config_version
        config = {
            "owner": "performlikemj",
            "repo": "kihoko",
            "branch": "main",
            "allowPaths": ["web/src/pages/AboutPage.js"],
            "maxFiles": 20,
            "siteNotes": "Home page hero = web/public/hero.jpg.",
        }
        output = StringIO()
        call_command(
            "set_site_editor_config",
            "--tenant-id",
            str(self.tenant.id),
            "--file",
            self.config_file(config),
            "--enable",
            stdout=output,
        )
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.site_editor_enabled)
        self.assertEqual(self.tenant.site_editor_config, config)
        self.assertEqual(self.tenant.pending_config_version, before + 1)
        self.assertEqual(
            output.getvalue(),
            "WARNING: the running image must already contain /opt/nbhd/plugins/nbhd-site-editor — "
            "enable only after the image roll\n"
            "site_editor_enabled=True keys=allowPaths,branch,maxFiles,owner,repo,siteNotes\n",
        )

    def test_config_command_rejects_unknown_keys_and_wrong_types_without_mutation(self):
        before = self.tenant.pending_config_version
        for config in (
            {"owner": "performlikemj", "token": "not-allowed"},
            {"allowPaths": ["good", 3]},
            {"maxFiles": True},
            {"owner": "bad/owner"},
            {"repo": "bad repo"},
            {"branch": "main/../escape"},
            {"branch": "main\nother"},
            {"siteNotes": "x" * 1001},
        ):
            with self.subTest(config=config), self.assertRaises(CommandError):
                call_command(
                    "set_site_editor_config",
                    "--tenant-id",
                    str(self.tenant.id),
                    "--file",
                    self.config_file(config),
                    "--enable",
                    stdout=StringIO(),
                )
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.site_editor_enabled)
        self.assertEqual(self.tenant.site_editor_config, {})
        self.assertEqual(self.tenant.pending_config_version, before)


@override_settings(GRAVITY_ENABLED=True)
class KihoShapeMaximalRenderTest(TestCase):
    def test_both_site_gates_identity_extras_and_soul_compose_under_budget(self):
        tenant = create_tenant(display_name="Kiho Shape", telegram_chat_id=940004)
        tenant.site_publishing_enabled = True
        tenant.site_editor_enabled = True
        tenant.finance_enabled = True
        tenant.save(update_fields=["site_publishing_enabled", "site_editor_enabled", "finance_enabled"])
        tenant.user.preferences = {
            "prompt_extras": {
                "agents_md": "## Who You Actually Are\n\nYou are Pistachio.",
                "soul_md": "CUSTOM_SOUL_TAIL",
            }
        }
        tenant.user.save(update_fields=["preferences"])
        tenant.user.refresh_from_db()

        rendered = render_workspace_files("neighbor", tenant=tenant)
        agents = rendered["NBHD_AGENTS_MD"]
        self.assertIn(_PORTFOLIO_GATE, agents)
        self.assertIn(_GATE, agents)
        self.assertIn("## Who You Actually Are", agents)
        self.assertIn("You are Pistachio.", agents)
        self.assertIn(_GRAVITY_GATE, agents)
        self.assertIn("CUSTOM_SOUL_TAIL", rendered["NBHD_SOUL_MD"])
        self.assertLess(agents.index(_PORTFOLIO_GATE), agents.index(_GATE))
        self.assertLess(agents.index(_GATE), agents.index(_GRAVITY_GATE))
        self.assertLess(len(agents), BOOTSTRAP_MAX_CHARS)
