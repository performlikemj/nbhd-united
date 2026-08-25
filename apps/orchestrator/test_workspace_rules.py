"""Tests for the workspace rules upload mechanism.

The rules templates in templates/openclaw/rules/ are uploaded to each tenant's
file share under workspace/rules/<filename> on config refresh. This test ensures:

1. render_workspace_rules() discovers all .md files in the rules dir
2. update_tenant_config() uploads each rule to workspace/rules/<filename>
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings

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
            "memory.md",
            "messaging.md",
            "onboarding.md",
        }
        # All expected rules should be present (may have more)
        self.assertTrue(expected_rules.issubset(set(rules.keys())))

    def test_workspaces_rule_removed(self):
        """rules/workspaces.md was deleted with the workspace-chat-routing removal."""
        rules = render_workspace_rules()
        self.assertNotIn("workspaces.md", rules)

    def test_memory_rule_carries_redacted_identity_honesty(self):
        rules = render_workspace_rules()
        memory = rules["memory.md"]
        self.assertIn("## Redacted identities", memory)
        self.assertIn("|unresolved", memory)
        self.assertIn("Never assert familiarity, deny", memory)

    def test_memory_rule_requires_lookup_backed_check_claims(self):
        memory = render_workspace_rules()["memory.md"]
        self.assertIn("## Claims about checking", memory)
        self.assertIn("only if you actually\ncalled a lookup tool THIS turn", memory)
        self.assertIn("worst\nfailure mode", memory)


class SubagentWorkspaceRulesTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Subagent Rules", telegram_chat_id=606061)

    @override_settings(SUBAGENT_TENANT_IDS="")
    def test_disabled_tenant_gets_exact_pre_feature_rule_set_and_agents_index(self):
        rules = render_workspace_rules(tenant=self.tenant)
        self.assertEqual(
            set(rules),
            {
                "_principles.md",
                "document-ingestion.md",
                "fuel.md",
                "journal-capture.md",
                "lessons-constellation.md",
                "memory.md",
                "messaging.md",
                "onboarding.md",
                "reply-markers.md",
                "voice-journal.md",
                "week-ahead.md",
            },
        )
        self.assertNotIn("rules/subagents.md", rules["messaging.md"])
        agents_md = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]
        self.assertNotIn("| `rules/subagents.md` |", agents_md)

    def test_enabled_tenant_gets_subagent_rule_messaging_exception_and_agents_index(self):
        with override_settings(SUBAGENT_TENANT_IDS=str(self.tenant.id)):
            rules = render_workspace_rules(tenant=self.tenant)
            agents_md = render_workspace_files("neighbor", tenant=self.tenant)["NBHD_AGENTS_MD"]

        self.assertIn("subagents.md", rules)
        self.assertIn(
            "If `rules/subagents.md` is present in your workspace, follow it",
            rules["messaging.md"],
        )
        self.assertIn("| `rules/subagents.md` |", agents_md)


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
    def test_update_tenant_config_uploads_rules(
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
        self.assertTrue(
            any("memory.md" in p for p in rules_paths),
            f"memory.md not found in uploaded rules: {rules_paths}",
        )
        self.assertNotIn("workspace/rules/subagents.md", rules_paths)
        mock_delete_workspace_file.assert_called_once_with(
            str(self.tenant.id),
            "workspace/rules/subagents.md",
        )
        memory_upload = next(
            call for call in mock_upload_workspace_file.call_args_list if call.args[1] == "workspace/rules/memory.md"
        )
        self.assertIn("## Redacted identities", memory_upload.args[2])
        self.assertIn("|unresolved", memory_upload.args[2])
        self.assertIn("## Claims about checking", memory_upload.args[2])
        self.assertIn("called a lookup tool THIS turn", memory_upload.args[2])

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
    def test_enabled_tenant_does_not_delete_subagent_rule(
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

        mock_delete_workspace_file.assert_not_called()
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
