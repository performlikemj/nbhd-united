"""Per-tenant journal-shaping gate, doc, plugin config, and capability command."""

from __future__ import annotations

import io
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.test.utils import override_settings

from apps.orchestrator.config_generator import generate_openclaw_config
from apps.orchestrator.config_validator import assert_config_writable
from apps.orchestrator.personas import render_workspace_files
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

_JOURNAL_SHAPING_GATE = """## Journal shaping

This user can reshape their journal template through you.
- `nbhd_journal_template_get` — read the current daily-note sections.
- `nbhd_journal_template_update` — replace the sections list.
- Before ANY reshape: call `nbhd_journal_template_get` to list the current sections, then propose the exact sections and get explicit agreement. Never reshape silently.
- Template = future structure only; existing notes are never modified by a template change.
- Pair every section change with its check-in schedule: prefer folding into an existing check-in over creating new ones."""


def _agents_md(tenant) -> str:
    return render_workspace_files("neighbor", tenant=tenant)["NBHD_AGENTS_MD"]


def _plugins(tenant) -> tuple[list[str], dict]:
    plugins = generate_openclaw_config(tenant).get("plugins", {})
    return plugins.get("load", {}).get("paths", []), plugins.get("entries", {})


class JournalShapingGateAndPluginTest(TestCase):
    def test_flag_off_has_zero_prompt_or_plugin_surface(self):
        tenant = create_tenant(display_name="Journal Shaping Off", telegram_chat_id=945001)

        agents_md = _agents_md(tenant)
        paths, entries = _plugins(tenant)

        self.assertFalse(tenant.journal_shaping_enabled)
        self.assertNotIn("## Journal shaping", agents_md)
        self.assertNotIn("nbhd_journal_template_get", agents_md)
        self.assertNotIn("nbhd_journal_template_update", agents_md)
        self.assertNotIn("/opt/nbhd/plugins/nbhd-journal-shaping", paths)
        self.assertNotIn("nbhd-journal-shaping", entries)

    @override_settings(GRAVITY_ENABLED=True)
    def test_flag_on_gate_precedes_gravity_and_plugin_is_emitted(self):
        tenant = create_tenant(display_name="Journal Shaping On", telegram_chat_id=945002)
        tenant.journal_shaping_enabled = True
        tenant.finance_enabled = True
        tenant.save(update_fields=["journal_shaping_enabled", "finance_enabled"])

        agents_md = _agents_md(tenant)
        paths, entries = _plugins(tenant)

        self.assertLess(agents_md.index("## Journal shaping"), agents_md.index("## Gravity Observation Mode"))
        self.assertIn("nbhd_journal_template_get", agents_md)
        self.assertIn("nbhd_journal_template_update", agents_md)
        self.assertIn("/opt/nbhd/plugins/nbhd-journal-shaping", paths)
        self.assertEqual(
            entries["nbhd-journal-shaping"]["config"],
            {"journalShapingEnabled": True},
        )
        assert_config_writable(generate_openclaw_config(tenant))

    def test_gate_block_is_verbatim_and_under_defensive_length_guard(self):
        tenant = create_tenant(display_name="Journal Shaping Lean", telegram_chat_id=945003)
        tenant.journal_shaping_enabled = True
        tenant.save(update_fields=["journal_shaping_enabled"])

        agents_md = _agents_md(tenant)
        gate = agents_md[agents_md.index("## Journal shaping") :]

        self.assertEqual(gate, _JOURNAL_SHAPING_GATE)
        self.assertLessEqual(len(gate), 700)


class JournalShapingServiceDocTest(TestCase):
    def _uploaded_paths(self, *, enabled: bool) -> tuple[list[str], str]:
        tenant = create_tenant(
            display_name=f"Journal Shaping Upload {enabled}",
            telegram_chat_id=945010 if enabled else 945011,
        )
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = f"oc-journal-shaping-{enabled}"
        tenant.journal_shaping_enabled = enabled
        tenant.save(update_fields=["status", "container_id", "journal_shaping_enabled"])

        rendered = render_workspace_files("neighbor", tenant=tenant)
        with (
            patch("apps.orchestrator.services.upload_config_to_file_share"),
            patch("apps.orchestrator.services.config_to_json", return_value="{}"),
            patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}}),
            patch("apps.orchestrator.services._audit_and_log"),
            patch(
                "apps.orchestrator.services.refresh_system_cron_rows_from_seed",
                return_value={"created": 0, "updated": 0, "preserved_custom": 0, "unchanged": 0},
            ),
            patch("apps.orchestrator.azure_client.download_workspace_file", return_value=None),
            patch("apps.orchestrator.azure_client.upload_workspace_file") as upload_workspace_file,
            patch("apps.orchestrator.workspace_envelope.push_user_md"),
        ):
            from apps.orchestrator.services import update_tenant_config

            update_tenant_config(str(tenant.id))

        paths = [call.args[1] for call in upload_workspace_file.call_args_list]
        return paths, rendered["NBHD_DOC_JOURNAL_SHAPING"]

    def test_flag_on_uploads_registered_doc(self):
        paths, doc = self._uploaded_paths(enabled=True)

        self.assertIn("workspace/docs/journal-shaping.md", paths)
        self.assertIn("# Journal shaping — making this journal theirs", doc)
        self.assertIn("Tomorrow's daily note materializes", doc)

    def test_flag_off_does_not_upload_doc(self):
        paths, _doc = self._uploaded_paths(enabled=False)

        self.assertNotIn("workspace/docs/journal-shaping.md", paths)


class SetJournalShapingCommandTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Journal Shaping Command", telegram_chat_id=945020)

    def _call(self, *args) -> str:
        stdout = io.StringIO()
        call_command("set_journal_shaping", *args, stdout=stdout)
        return stdout.getvalue()

    def test_enable_sets_flag_and_bumps_pending_config(self):
        initial_version = self.tenant.pending_config_version

        output = self._call("--tenant-id", str(self.tenant.id), "--enable")

        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.journal_shaping_enabled)
        self.assertEqual(self.tenant.pending_config_version, initial_version + 1)
        self.assertIn("journal_shaping_enabled=True", output)
        self.assertIn("force_apply_configs --tenant-id", output)

    def test_disable_clears_flag(self):
        self.tenant.journal_shaping_enabled = True
        self.tenant.save(update_fields=["journal_shaping_enabled"])

        self._call("--tenant-id", str(self.tenant.id), "--disable")

        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.journal_shaping_enabled)

    def test_unknown_tenant_errors(self):
        with self.assertRaises(CommandError):
            self._call(
                "--tenant-id",
                "00000000-0000-0000-0000-000000000000",
                "--enable",
            )
