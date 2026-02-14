"""Tests for OpenClaw subscriber tool policy generation."""
from django.test import TestCase

from apps.orchestrator.tool_policy import (
    BASIC_ALLOW,
    DENIED_TOOLS,
    PLUS_ALLOW,
    generate_tool_config,
    get_allowed_tools,
)


class ToolPolicyTest(TestCase):
    def test_basic_allowlist_has_expected_groups(self):
        allowed = get_allowed_tools("basic")
        self.assertEqual(allowed, list(BASIC_ALLOW))
        self.assertNotIn("group:browser", allowed)
        self.assertNotIn("exec", allowed)

    def test_plus_allowlist_extends_basic(self):
        allowed = get_allowed_tools("plus")
        self.assertEqual(allowed, list(PLUS_ALLOW))
        self.assertIn("group:browser", allowed)
        self.assertIn("exec", allowed)

    def test_unknown_tier_defaults_to_basic(self):
        self.assertEqual(get_allowed_tools("unknown"), list(BASIC_ALLOW))

    def test_policy_denies_runtime_management_tools(self):
        config = generate_tool_config("basic")
        denied = config["deny"]
        for tool in DENIED_TOOLS:
            self.assertIn(tool, denied)

    def test_policy_disables_elevated_tools_for_subscribers(self):
        config = generate_tool_config("plus")
        self.assertEqual(config["elevated"], {"enabled": False})

    def test_policy_uses_documented_keys_only(self):
        config = generate_tool_config("plus")
        self.assertNotIn("agent_tool_policy", config)
        self.assertNotIn("exec", config)
        self.assertIn("allow", config)
        self.assertIn("deny", config)
        self.assertIn("elevated", config)
