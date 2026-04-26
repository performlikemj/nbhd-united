"""Tests for the workspace rules upload mechanism (Phase 4 of workspace routing).

The rules templates in templates/openclaw/rules/ were created in PR #172 but
the upload mechanism was never wired. This test ensures:

1. render_workspace_rules() discovers all .md files in the rules dir
2. update_tenant_config() uploads each rule to workspace/rules/<filename>
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from apps.orchestrator.personas import render_workspace_rules
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

    def test_includes_workspaces_rule(self):
        """Phase 4 added rules/workspaces.md — verify it's discovered."""
        rules = render_workspace_rules()
        self.assertIn("workspaces.md", rules)
        self.assertIn("workspace", rules["workspaces.md"].lower())

    def test_includes_existing_rules(self):
        """Pre-existing rules from PR #172 should also be discovered."""
        rules = render_workspace_rules()
        expected_rules = {
            "journal-capture.md",
            "lessons-constellation.md",
            "memory.md",
            "messaging.md",
            "onboarding.md",
            "workspaces.md",
        }
        # All expected rules should be present (may have more)
        self.assertTrue(expected_rules.issubset(set(rules.keys())))


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
    @patch("apps.orchestrator.services.update_system_cron_prompts", return_value={"updated": 0})
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_update_tenant_config_uploads_rules(
        self,
        mock_upload_workspace_file,
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

        # Verify at least the new workspaces.md rule was uploaded
        rules_paths = [p for p in uploaded_paths if "workspace/rules/" in p]
        self.assertGreater(
            len(rules_paths),
            0,
            f"No rules uploaded. All paths: {uploaded_paths}",
        )
        self.assertTrue(
            any("workspaces.md" in p for p in rules_paths),
            f"workspaces.md not found in uploaded rules: {rules_paths}",
        )

    @patch("apps.orchestrator.services.upload_config_to_file_share")
    @patch("apps.orchestrator.services.config_to_json", return_value="{}")
    @patch("apps.orchestrator.services.generate_openclaw_config", return_value={"gateway": {}})
    @patch("apps.orchestrator.services._audit_and_log")
    @patch("apps.orchestrator.services.update_system_cron_prompts", return_value={"updated": 0})
    @patch("apps.orchestrator.azure_client.upload_workspace_file")
    def test_soul_and_identity_use_skip_if_exists(
        self,
        mock_upload_workspace_file,
        _mock_update_crons,
        _mock_audit,
        _mock_generate_config,
        _mock_config_to_json,
        _mock_upload_config,
    ):
        """SOUL.md and IDENTITY.md must be uploaded with skip_if_exists=True so a
        config refresh never overwrites agent-evolved soul/identity content.
        Mirrors the `[ ! -f ]` guard in runtime/openclaw/entrypoint.sh.
        """
        from apps.orchestrator.services import update_tenant_config

        update_tenant_config(str(self.tenant.id))

        seed_once_paths = {"workspace/SOUL.md", "workspace/IDENTITY.md"}
        seen_seed_once: dict[str, bool] = {}
        for call in mock_upload_workspace_file.call_args_list:
            file_path = call.args[1] if len(call.args) > 1 else call.kwargs.get("file_path")
            if file_path in seed_once_paths:
                seen_seed_once[file_path] = call.kwargs.get("skip_if_exists", False)

        self.assertEqual(
            set(seen_seed_once.keys()),
            seed_once_paths,
            f"Expected SOUL.md and IDENTITY.md to be uploaded, got {set(seen_seed_once.keys())}",
        )
        for path, skip_flag in seen_seed_once.items():
            self.assertTrue(
                skip_flag,
                f"{path} must be uploaded with skip_if_exists=True (got {skip_flag})",
            )

        # Sanity: AGENTS.md and rules must still be unconditional overwrites
        for call in mock_upload_workspace_file.call_args_list:
            file_path = call.args[1] if len(call.args) > 1 else call.kwargs.get("file_path", "")
            if file_path == "workspace/AGENTS.md" or "workspace/rules/" in file_path:
                self.assertFalse(
                    call.kwargs.get("skip_if_exists", False),
                    f"{file_path} must overwrite, not skip-if-exists",
                )
