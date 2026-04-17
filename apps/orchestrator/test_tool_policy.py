"""Tests for OpenClaw subscriber tool policy generation."""

from django.test import TestCase

from apps.orchestrator.tool_policy import (
    DENIED_TOOLS,
    STARTER_ALLOW,
    generate_tool_config,
    get_allowed_tools,
)


class ToolPolicyTest(TestCase):
    def test_starter_allowlist_has_expected_groups(self):
        allowed = get_allowed_tools("starter")
        self.assertEqual(allowed, list(STARTER_ALLOW))
        self.assertIn("group:openclaw", allowed)
        self.assertIn("group:plugins", allowed)
        # Only two entries — everything else controlled via deny list
        self.assertEqual(len(allowed), 2)

    def test_unknown_tier_defaults_to_starter(self):
        self.assertEqual(get_allowed_tools("unknown"), list(STARTER_ALLOW))

    def test_policy_denies_runtime_management_tools(self):
        config = generate_tool_config("starter")
        denied = config["deny"]
        for tool in DENIED_TOOLS:
            self.assertIn(tool, denied)

    def test_policy_disables_elevated_tools_for_subscribers(self):
        config = generate_tool_config("starter")
        self.assertEqual(config["elevated"], {"enabled": False})

    def test_cron_tools_not_denied(self):
        """Cron tools must be allowed so users can manage scheduled tasks."""
        config = generate_tool_config("starter")
        denied = config["deny"]
        self.assertNotIn("cron", denied)

    def test_memory_tools_not_denied(self):
        """Memory tools must be allowed for cross-session recall."""
        config = generate_tool_config("starter")
        denied = config["deny"]
        self.assertNotIn("memory_search", denied)
        self.assertNotIn("memory_get", denied)

    def test_messaging_denied_for_subscribers(self):
        """Direct messaging must be denied — subscribers use nbhd_send_to_user plugin."""
        config = generate_tool_config("starter")
        denied = config["deny"]
        self.assertIn("message", denied)

    def test_policy_uses_documented_keys_only(self):
        config = generate_tool_config("starter")
        self.assertNotIn("agent_tool_policy", config)
        self.assertNotIn("exec", config)
        self.assertIn("allow", config)
        self.assertIn("deny", config)
        self.assertIn("elevated", config)
