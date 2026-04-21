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
        self.assertNotIn("group:ui", allowed)
        self.assertNotIn("group:runtime", allowed)
        # Workspace file tools excluded — persistence via journal plugins only
        self.assertNotIn("group:fs", allowed)
        self.assertNotIn("group:memory", allowed)
        self.assertIn("group:plugins", allowed)

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
        self.assertNotIn("group:automation", denied)

    def test_policy_uses_documented_keys_only(self):
        config = generate_tool_config("starter")
        self.assertNotIn("agent_tool_policy", config)
        self.assertNotIn("exec", config)
        self.assertIn("allow", config)
        self.assertIn("deny", config)
        self.assertIn("elevated", config)


class VersionAwareToolPolicyTest(TestCase):
    """Tool policy must vary by OpenClaw version."""

    def test_2026_4_5_returns_original_allow(self):
        allowed = get_allowed_tools("starter", version="2026.4.5")
        self.assertIn("group:web", allowed)
        self.assertIn("group:plugins", allowed)
        self.assertIn("group:automation", allowed)
        self.assertIn("tts", allowed)
        self.assertIn("image", allowed)
        self.assertNotIn("group:openclaw", allowed)

    def test_2026_4_15_returns_new_allow(self):
        allowed = get_allowed_tools("starter", version="2026.4.15")
        self.assertIn("group:openclaw", allowed)
        self.assertIn("group:plugins", allowed)
        self.assertNotIn("group:web", allowed)
        self.assertNotIn("group:automation", allowed)

    def test_2026_4_15_denies_expanded_list(self):
        from apps.orchestrator.tool_policy import get_denied_tools

        denied = get_denied_tools(version="2026.4.15")
        for tool in ("sessions_yield", "subagents", "message", "browser",
                      "canvas", "nodes", "code_execution", "music_generate",
                      "video_generate"):
            self.assertIn(tool, denied)
        # Original denies still present
        self.assertIn("gateway", denied)
        self.assertIn("sessions_spawn", denied)

    def test_future_version_uses_latest_policy(self):
        allowed = get_allowed_tools("starter", version="2026.5.1")
        self.assertIn("group:openclaw", allowed)

    def test_backward_compat_module_constants(self):
        self.assertIsInstance(DENIED_TOOLS, tuple)
        self.assertIsInstance(STARTER_ALLOW, tuple)
        self.assertIn("gateway", DENIED_TOOLS)
        self.assertIn("group:web", STARTER_ALLOW)
