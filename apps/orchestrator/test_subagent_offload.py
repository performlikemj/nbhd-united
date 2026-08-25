"""Canary-gated OpenClaw sub-agent config and validation coverage."""

import re
from copy import deepcopy
from pathlib import Path

from django.test import TestCase, override_settings

from apps.orchestrator.config_generator import (
    SUBAGENT_READ_ONLY_TOOLS,
    TIER_TASK_DEFAULTS,
    generate_openclaw_config,
    subagents_enabled,
)
from apps.orchestrator.config_validator import assert_config_writable, validate_openclaw_config
from apps.orchestrator.tool_policy import generate_tool_config
from apps.tenants.services import create_tenant


class SubagentCanaryConfigTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Subagent Config", telegram_chat_id=908001)

    @override_settings(SUBAGENT_TENANT_IDS="")
    def test_empty_gate_keeps_legacy_deny_and_subagents_blocks_exactly(self):
        config = generate_openclaw_config(self.tenant)
        baseline_tools = generate_tool_config("starter", version=self.tenant.openclaw_version)

        self.assertEqual(config["tools"]["deny"], baseline_tools["deny"])
        self.assertNotIn("subagents", config["tools"])
        self.assertEqual(
            config["agents"]["defaults"]["subagents"],
            {
                "maxConcurrent": 2,
                "model": config["agents"]["defaults"]["model"]["primary"],
            },
        )
        self.assertNotIn("nbhd-subagent-bridge", config.get("plugins", {}).get("allow", []))
        usage_entry = config["plugins"]["entries"].get("nbhd-usage-reporter")
        if usage_entry is not None:
            self.assertEqual(usage_entry, {"enabled": True})

    def test_non_allowlisted_tenant_snapshot_is_identical_to_empty_gate(self):
        with override_settings(SUBAGENT_TENANT_IDS=""):
            before = generate_openclaw_config(self.tenant)
        with override_settings(SUBAGENT_TENANT_IDS="00000000-0000-4000-8000-000000000999"):
            after = generate_openclaw_config(self.tenant)

        self.assertEqual(after, before)
        self.assertEqual(after["tools"]["deny"], before["tools"]["deny"])
        self.assertEqual(
            after["agents"]["defaults"]["subagents"],
            before["agents"]["defaults"]["subagents"],
        )

    def test_enabled_tenant_unlocks_exact_tools_and_gets_bounded_helpers(self):
        with override_settings(SUBAGENT_TENANT_IDS=f" {str(self.tenant.id).upper()} "):
            config = generate_openclaw_config(self.tenant)

        baseline_deny = generate_tool_config("starter", version=self.tenant.openclaw_version)["deny"]
        self.assertEqual(
            config["tools"]["deny"],
            [name for name in baseline_deny if name not in {"sessions_spawn", "subagents"}],
        )
        self.assertNotIn("sessions_spawn", config["tools"]["deny"])
        self.assertNotIn("subagents", config["tools"]["deny"])
        self.assertIn("sessions_yield", config["tools"]["deny"])
        for still_denied in (
            "sessions_send",
            "message",
            "gateway",
            "agents_list",
            "sessions_list",
            "sessions_history",
            "session_status",
            "nodes",
        ):
            self.assertIn(still_denied, config["tools"]["deny"])
        self.assertEqual(
            config["tools"]["subagents"],
            {"tools": {"allow": list(SUBAGENT_READ_ONLY_TOOLS)}},
        )
        self.assertNotIn("nbhd_send_to_user", SUBAGENT_READ_ONLY_TOOLS)
        self.assertNotIn("nbhd_generate_image", SUBAGENT_READ_ONLY_TOOLS)
        self.assertNotIn("publish_portfolio_image", SUBAGENT_READ_ONLY_TOOLS)
        self.assertEqual(
            config["agents"]["defaults"]["subagents"],
            {
                "maxConcurrent": 2,
                "maxChildrenPerAgent": 2,
                "maxSpawnDepth": 1,
                "runTimeoutSeconds": 600,
                "announceTimeoutMs": 120000,
                "archiveAfterMinutes": 60,
                "model": TIER_TASK_DEFAULTS["starter"]["background_tasks"],
                "delegationMode": "suggest",
            },
        )
        plugins = config["plugins"]
        self.assertIn("nbhd-subagent-bridge", plugins["allow"])
        self.assertEqual(
            next(path for path in plugins["load"]["paths"] if path.endswith("subagent-bridge")),
            "/opt/nbhd/plugins/nbhd-journal-tools/subagent-bridge",
        )
        self.assertEqual(
            plugins["entries"]["nbhd-subagent-bridge"],
            {
                "enabled": True,
                "hooks": {
                    "allowConversationAccess": True,
                    "timeoutMs": 30000,
                },
            },
        )
        self.assertEqual(
            plugins["entries"]["nbhd-usage-reporter"],
            {
                "enabled": True,
                "hooks": {"allowConversationAccess": True},
                "config": {"helperOnly": True},
            },
        )

    def test_gate_helper_is_fail_closed_and_case_insensitive(self):
        with override_settings(SUBAGENT_TENANT_IDS=""):
            self.assertFalse(subagents_enabled(self.tenant))
        with override_settings(SUBAGENT_TENANT_IDS=str(self.tenant.id).upper()):
            self.assertTrue(subagents_enabled(self.tenant))

    def test_python_and_javascript_helper_allowlists_are_equal(self):
        source = Path("runtime/openclaw/plugins/nbhd-routing-context/index.js").read_text()
        match = re.search(
            r"SUBAGENT_READ_ONLY_TOOL_IDS\s*=\s*new Set\(\[(.*?)\]\);",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        javascript_allowlist = tuple(re.findall(r'"([a-z0-9_-]+)"', match.group(1)))
        self.assertEqual(javascript_allowlist, SUBAGENT_READ_ONLY_TOOLS)


class SubagentConfigValidatorTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Subagent Validation", telegram_chat_id=908002)

    def _enabled_config(self):
        with override_settings(SUBAGENT_TENANT_IDS=str(self.tenant.id)):
            return generate_openclaw_config(self.tenant)

    def test_validator_accepts_enabled_nested_blocks(self):
        config = self._enabled_config()
        self.assertEqual(
            [issue for issue in validate_openclaw_config(config, strict=True) if issue.severity == "error"],
            [],
        )
        assert_config_writable(config)

    def test_validator_rejects_bad_subagent_knob_and_tool_allow_shape(self):
        config = self._enabled_config()
        broken = deepcopy(config)
        broken["agents"]["defaults"]["subagents"]["maxSpawnDepth"] = "one"
        broken["tools"]["subagents"]["tools"]["allow"] = "web_search"
        errors = [issue.path for issue in validate_openclaw_config(broken, strict=True) if issue.severity == "error"]
        self.assertIn("agents.defaults.subagents.maxSpawnDepth", errors)
        self.assertIn("tools.subagents.tools.allow", errors)
