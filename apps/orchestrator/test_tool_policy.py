"""Tests for OpenClaw subscriber tool policy generation."""
from django.test import TestCase

from apps.orchestrator.tool_policy import (
    DENIED_TOOLS,
    PREMIUM_ALLOW,
    STARTER_ALLOW,
    generate_tool_config,
    get_allowed_tools,
)


class ToolPolicyTest(TestCase):
    def test_starter_allowlist_has_expected_groups(self):
        allowed = get_allowed_tools("starter")
        self.assertEqual(allowed, list(STARTER_ALLOW))
        self.assertNotIn("group:browser", allowed)
        self.assertNotIn("exec", allowed)

    def test_premium_allowlist_extends_starter(self):
        allowed = get_allowed_tools("premium")
        self.assertEqual(allowed, list(PREMIUM_ALLOW))
        self.assertIn("group:browser", allowed)
        self.assertIn("exec", allowed)

    def test_byok_gets_premium_tools(self):
        self.assertEqual(get_allowed_tools("byok"), list(PREMIUM_ALLOW))

    def test_unknown_tier_defaults_to_starter(self):
        self.assertEqual(get_allowed_tools("unknown"), list(STARTER_ALLOW))

    def test_policy_denies_runtime_management_tools(self):
        config = generate_tool_config("starter")
        denied = config["deny"]
        for tool in DENIED_TOOLS:
            self.assertIn(tool, denied)

    def test_policy_disables_elevated_tools_for_subscribers(self):
        config = generate_tool_config("premium")
        self.assertEqual(config["elevated"], {"enabled": False})

    def test_policy_uses_documented_keys_only(self):
        config = generate_tool_config("premium")
        self.assertNotIn("agent_tool_policy", config)
        self.assertNotIn("exec", config)
        self.assertIn("allow", config)
        self.assertIn("deny", config)
        self.assertIn("elevated", config)
