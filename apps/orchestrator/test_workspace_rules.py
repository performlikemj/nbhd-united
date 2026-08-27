"""Tests for the workspace rules upload mechanism.

The rules templates in templates/openclaw/rules/ are uploaded to each tenant's
file share under workspace/rules/<filename> on config refresh. This test ensures:

1. render_workspace_rules() discovers all .md files in the rules dir
2. update_tenant_config() uploads each rule to workspace/rules/<filename>
"""

from __future__ import annotations

from unittest.mock import call as mock_call
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.orchestrator.config_generator import _prepare_cron_prompt
from apps.orchestrator.personas import render_workspace_files, render_workspace_rules
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class RenderWorkspaceRulesTest(TestCase):
    """render_workspace_rules() loads all rules templates from disk."""

    def test_returns_dict_of_rule_files(self):
        rules = render_workspace_rules()
        # Should be a dict mapping filename → content
        self.assertIsInstance(rules, dict)
        # All keys should end in .md
        for filename in rules.keys():
            self.assertTrue(filename.endswith(".md"))
        # All values should be non-empty strings
        for content in rules.values():
            self.assertIsInstance(content, str)
            self.assertGreater(len(content), 0)

    def test_includes_core_rules(self):
        """The core rule files should be discovered by render_workspace_rules."""
        rules = render_workspace_rules()
        expected_rules = {
            "journal-capture.md",
            "lessons-constellation.md",
            "messaging.md",
            "week-ahead.md",
        }
        # All expected rules should be present (may have more)
        self.assertTrue(expected_rules.issubset(set(rules.keys())))

    def test_workspaces_rule_removed(self):
        """rules/workspaces.md was deleted with the workspace-chat-routing removal."""
        rules = render_workspace_rules()
        self.assertNotIn("workspaces.md", rules)

    def test_retired_rules_are_absent(self):
        rules = render_workspace_rules()
        self.assertNotIn("memory.md", rules)
        self.assertNotIn("document-ingestion.md", rules)
        self.assertNotIn("onboarding.md", rules)
        self.assertNotIn("_principles.md", rules)

    def test_fuel_rule_is_a_small_cron_only_stub(self):
        fuel = render_workspace_rules()["fuel.md"]
        self.assertTrue(fuel.startswith("<!-- CRON-ONLY:"))
        self.assertLessEqual(len(fuel), 1_500)
        self.assertIn("For Fuel plans/fill-ins, first use `tool_search`", fuel)
        self.assertIn("background workout cron runs silently", fuel)
        self.assertIn("Morning briefings", fuel)
        self.assertIn("Evening check-ins", fuel)
        self.assertIn("Week-ahead reviews", fuel)

    def test_rendered_agents_has_always_loaded_fuel_search_gate(self):
        agents = render_workspace_files("neighbor")["NBHD_AGENTS_MD"]
        self.assertIn(
            "For Fuel plans/fill-ins, first use `tool_search` for exact `nbhd_fuel_search_exercises`",
            agents,
        )

    def test_messaging_rule_is_a_cron_only_stub(self):
        messaging = render_workspace_rules()["messaging.md"]
        self.assertTrue(messaging.startswith("<!-- CRON-ONLY:"))
        self.assertIn("Only message if you have something genuinely useful to say.", messaging)
        self.assertIn("Outside the window: respond to messages but don't proactively check in.", messaging)
        self.assertIn(
            "Read `docs/cron-management.md` before creating, editing, or disabling scheduled tasks.",
            messaging,
        )
        self.assertNotIn("PATCH /api/v1/tenants/heartbeat/", messaging)
        self.assertNotIn("Nightly Extraction", messaging)
        self.assertNotIn("Project Check-in", messaging)

    def test_week_ahead_rule_is_cron_only_and_excludes_reactive_chat_rules(self):
        week_ahead = render_workspace_rules()["week-ahead.md"]
        self.assertTrue(week_ahead.startswith("<!-- CRON-ONLY:"))
        self.assertIn("Once a week, make yourself aware of the user's upcoming week", week_ahead)
        self.assertIn("Current active cron jobs (`cron list`)", week_ahead)
        self.assertIn("All decisions are logged", week_ahead)
        self.assertNotIn("Reactive:", week_ahead)
        self.assertNotIn("Any user plan change mid-week", week_ahead)
        self.assertNotIn("immediately re-run the same check", week_ahead)

    def test_cron_management_doc_does_not_copy_seed_schedules_or_nightly_extraction(self):
        cron_doc = render_workspace_files("neighbor")["NBHD_DOC_CRON_MANAGEMENT"]
        self.assertNotIn("Nightly Extraction", cron_doc)
        self.assertNotIn("## System tasks (do NOT recreate, delete, or disable)", cron_doc)
        self.assertNotIn("The 2:00 AM cron", cron_doc)


class SubagentWorkspaceRulesTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Subagent Rules", telegram_chat_id=606061)

    @override_settings(SUBAGENT_TENANT_IDS="")
    def test_disabled_tenant_gets_exact_pre_feature_rule_set_and_no_chat_or_cron_index_row(self):
        rules = render_workspace_rules(tenant=self.tenant)
        self.assertEqual(
            set(rules),
            {
                "fuel.md",
                "journal-capture.md",
                "lessons-constellation.md",
                "messaging.md",
                "week-ahead.md",
            },
        )
        self.assertNotIn("rules/subagents.md", rules["messaging.md"])
        agents_md = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
        cron_prompt = _prepare_cron_prompt("Task body", self.tenant)
        self.assertNotIn("| `rules/subagents.md` |", agents_md)
        self.assertNotIn("| `rules/subagents.md` |", cron_prompt)

    def test_enabled_tenant_gets_subagent_rule_and_cron_index_but_no_chat_index(self):
        with override_settings(SUBAGENT_TENANT_IDS=str(self.tenant.id)):
            rules = render_workspace_rules(tenant=self.tenant)
            agents_md = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
            cron_prompt = _prepare_cron_prompt("Task body", self.tenant)

        self.assertIn("subagents.md", rules)
        self.assertIn(
            "If `rules/subagents.md` is present in your workspace, follow it",
            rules["messaging.md"],
        )
        self.assertNotIn("| `rules/subagents.md` |", agents_md)
        self.assertIn(
            "| `rules/subagents.md` | Slow-task delegation and app completion delivery |",
            cron_prompt,
        )


class UpdateTenantConfigUploadsRulesTest(TestCase):
    """update_tenant_config() uploads rules to workspace/rules/."""

    def setUp(self):
        self.tenant = create_tenant(display_name="RulesUpload", telegram_chat_id=606060)
        # Tenant must be ACTIVE with a container_id for update_tenant_config to proceed
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-test-container"
        self.tenant.save(update_fields=["status", "container_id"])

    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services._audit_and_log")
    @patch(
        "apps.orchestrator.services.refresh_system_cron_rows_from_seed",
        return_value={"created": 0, "updated": 0, "preserved_custom": 0, "unchanged": 0},
    )
    @patch("apps.orchestrator.azure_client.delete_workspace_file")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_update_tenant_config_uploads_rules_and_deletes_retired_rules(
        self,
        mock_upload_workspace_file,
        mock_delete_workspace_file,
        _mock_update_crons,
        _mock_audit,
        _mock_generate_config,
        _mock_config_to_json,
        _mock_upload_config,
    ):
        from apps.orchestrator.services import update_tenant_config

        update_tenant_config(str(self.tenant.id))

        # Collect all (file_path,) args passed to upload_workspace_file
        uploaded_paths = [
            call.args[1] if len(call.args) > 1 else call.kwargs.get("file_path", "")
            for call in mock_upload_workspace_file.call_args_list
        ]

        # Verify the rules dir was uploaded (covers any of the core rule files)
        rules_paths = [p for p in uploaded_paths if "workspace/rules/" in p]
        self.assertGreater(
            len(rules_paths),
            0,
            f"No rules uploaded. All paths: {uploaded_paths}",
        )
        self.assertNotIn("workspace/rules/memory.md", rules_paths)
        self.assertNotIn("workspace/rules/document-ingestion.md", rules_paths)
        self.assertNotIn("workspace/rules/onboarding.md", rules_paths)
        self.assertNotIn("workspace/rules/_principles.md", rules_paths)
        self.assertNotIn("workspace/rules/subagents.md", rules_paths)
        mock_delete_workspace_file.assert_has_calls(
            [
                mock_call(str(self.tenant.id), "workspace/rules/voice-journal.md"),
                mock_call(str(self.tenant.id), "workspace/rules/reply-markers.md"),
                mock_call(str(self.tenant.id), "workspace/rules/memory.md"),
                mock_call(str(self.tenant.id), "workspace/rules/document-ingestion.md"),
                mock_call(str(self.tenant.id), "workspace/rules/onboarding.md"),
                mock_call(str(self.tenant.id), "workspace/rules/_principles.md"),
                mock_call(str(self.tenant.id), "workspace/rules/subagents.md"),
            ],
            any_order=True,
        )
        self.assertEqual(mock_delete_workspace_file.call_count, 7)

    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services._audit_and_log")
    @patch(
        "apps.orchestrator.services.refresh_system_cron_rows_from_seed",
        return_value={"created": 0, "updated": 0, "preserved_custom": 0, "unchanged": 0},
    )
    @patch("apps.orchestrator.azure_client.delete_workspace_file")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_enabled_tenant_keeps_subagent_rule_and_deletes_retired_rules(
        self,
        mock_upload_workspace_file,
        mock_delete_workspace_file,
        _mock_update_crons,
        _mock_audit,
        _mock_generate_config,
        _mock_config_to_json,
        _mock_upload_config,
    ):
        from apps.orchestrator.services import update_tenant_config

        with override_settings(SUBAGENT_TENANT_IDS=str(self.tenant.id)):
            update_tenant_config(str(self.tenant.id))

        mock_delete_workspace_file.assert_has_calls(
            [
                mock_call(str(self.tenant.id), "workspace/rules/voice-journal.md"),
                mock_call(str(self.tenant.id), "workspace/rules/reply-markers.md"),
                mock_call(str(self.tenant.id), "workspace/rules/memory.md"),
                mock_call(str(self.tenant.id), "workspace/rules/document-ingestion.md"),
                mock_call(str(self.tenant.id), "workspace/rules/onboarding.md"),
                mock_call(str(self.tenant.id), "workspace/rules/_principles.md"),
            ],
            any_order=True,
        )
        self.assertEqual(mock_delete_workspace_file.call_count, 6)
        uploaded_paths = [call.args[1] for call in mock_upload_workspace_file.call_args_list]
        self.assertIn("workspace/rules/subagents.md", uploaded_paths)

    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services._audit_and_log")
    @patch(
        "apps.orchestrator.services.refresh_system_cron_rows_from_seed",
        return_value={"created": 0, "updated": 0, "preserved_custom": 0, "unchanged": 0},
    )
    @patch("apps.orchestrator.azure_client.delete_workspace_file")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file", return_value=None)
    def test_soul_and_identity_use_merge_push(
        self,
        _mock_download_workspace_file,
        mock_upload_workspace_file,
        _mock_delete_workspace_file,
        _mock_update_crons,
        _mock_audit,
        _mock_generate_config,
        _mock_config_to_json,
        _mock_upload_config,
    ):
        """SOUL.md and IDENTITY.md are merge-pushed (read current → splice →
        upload), NOT skip_if_exists. The managed region is re-asserted every
        config apply while the agent's growth region is preserved; a fresh share
        (download → None) writes the managed baseline + growth seed.
        """
        from apps.orchestrator import identity_merge as im
        from apps.orchestrator.services import update_tenant_config

        update_tenant_config(str(self.tenant.id))

        identity_paths = {"workspace/SOUL.md", "workspace/IDENTITY.md"}
        seen: dict[str, dict] = {}
        for call in mock_upload_workspace_file.call_args_list:
            file_path = call.args[1] if len(call.args) > 1 else call.kwargs.get("file_path")
            if file_path in identity_paths:
                seen[file_path] = {
                    "content": call.args[2] if len(call.args) > 2 else call.kwargs.get("content", ""),
                    "skip": call.kwargs.get("skip_if_exists", False),
                }

        self.assertEqual(
            set(seen.keys()),
            identity_paths,
            f"Expected SOUL.md and IDENTITY.md to be merge-pushed, got {set(seen.keys())}",
        )
        # Must NOT use skip_if_exists anymore — the merge preserves growth instead.
        for path, info in seen.items():
            self.assertFalse(info["skip"], f"{path} must merge-push, not skip-if-exists")

        # A fresh share writes the managed region (markers present) + growth seed.
        self.assertTrue(seen["workspace/SOUL.md"]["content"].startswith(im.SOUL_BEGIN_MARKER))
        self.assertIn("This space is yours", seen["workspace/SOUL.md"]["content"])
        self.assertTrue(seen["workspace/IDENTITY.md"]["content"].startswith(im.IDENTITY_BEGIN_MARKER))

        # Sanity: AGENTS.md and rules must still be unconditional overwrites
        for call in mock_upload_workspace_file.call_args_list:
            file_path = call.args[1] if len(call.args) > 1 else call.kwargs.get("file_path", "")
            if file_path == "workspace/AGENTS.md" or "workspace/rules/" in file_path:
                self.assertFalse(
                    call.kwargs.get("skip_if_exists", False),
                    f"{file_path} must overwrite, not skip-if-exists",
                )

    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services._audit_and_log")
    @patch(
        "apps.orchestrator.services.refresh_system_cron_rows_from_seed",
        return_value={"created": 0, "updated": 0, "preserved_custom": 0, "unchanged": 0},
    )
    @patch("apps.orchestrator.azure_client.delete_workspace_file")
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    @patch("apps.orchestrator.azure_client.download_workspace_file")
    def test_identity_read_error_fails_closed(
        self,
        mock_download_workspace_file,
        mock_upload_workspace_file,
        _mock_delete_workspace_file,
        _mock_update_crons,
        _mock_audit,
        _mock_generate_config,
        _mock_config_to_json,
        _mock_upload_config,
    ):
        """A read failure on SOUL/IDENTITY must skip the write (fail-closed) —
        never blindly overwrite an unreadable growth region — while AGENTS.md
        (which does not read the identity share) still uploads.
        """
        from apps.orchestrator.services import update_tenant_config

        mock_download_workspace_file.side_effect = RuntimeError("azure throttled")

        update_tenant_config(str(self.tenant.id))

        uploaded_paths = [
            call.args[1] if len(call.args) > 1 else call.kwargs.get("file_path", "")
            for call in mock_upload_workspace_file.call_args_list
        ]
        self.assertNotIn("workspace/SOUL.md", uploaded_paths)
        self.assertNotIn("workspace/IDENTITY.md", uploaded_paths)
        self.assertIn("workspace/AGENTS.md", uploaded_paths)
