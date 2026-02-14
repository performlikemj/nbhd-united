"""Tests for subscriber agent tool policy."""
from __future__ import annotations

from django.test import TestCase

from apps.orchestrator.tool_policy import (
    BLOCKED_TOOLS,
    SUBSCRIBER_TOOLS,
    get_allowed_tools,
    get_blocked_tools,
    generate_tool_config,
)


class TestToolPolicy(TestCase):
    """Verify tool policy enforces safe boundaries for subscriber agents."""

    def test_gateway_always_blocked(self):
        """Gateway tool must NEVER be available — prevents runtime takeover."""
        assert "gateway" in BLOCKED_TOOLS
        assert "gateway" not in get_allowed_tools("basic")
        assert "gateway" not in get_allowed_tools("plus")

    def test_cron_always_blocked(self):
        """Cron must be blocked — platform manages schedules, not users."""
        assert "cron" in BLOCKED_TOOLS

    def test_session_tools_blocked(self):
        """Session management tools expose cross-tenant info."""
        for tool in ["sessions_spawn", "sessions_send", "sessions_list",
                      "sessions_history", "session_status", "agents_list"]:
            assert tool in BLOCKED_TOOLS, f"{tool} should be blocked"

    def test_no_overlap_between_blocked_and_allowed(self):
        """A tool can't be both blocked and allowed."""
        allowed_names = set(SUBSCRIBER_TOOLS.keys())
        overlap = allowed_names & BLOCKED_TOOLS
        assert not overlap, f"Tools in both allowed and blocked: {overlap}"

    def test_basic_tier_tools(self):
        """Basic tier gets core tools only."""
        allowed = get_allowed_tools("basic")
        assert "web_search" in allowed
        assert "memory_search" in allowed
        assert "read" in allowed
        assert "write" in allowed
        assert "message" in allowed
        # Plus-only tools excluded
        assert "exec" not in allowed
        assert "browser" not in allowed

    def test_plus_tier_includes_basic(self):
        """Plus tier includes all basic tools plus extras."""
        basic = set(get_allowed_tools("basic"))
        plus = set(get_allowed_tools("plus"))
        assert basic.issubset(plus), f"Basic tools missing from plus: {basic - plus}"
        assert "exec" in plus
        assert "browser" in plus

    def test_generate_config_disables_gateway(self):
        """Generated config must explicitly disable gateway."""
        config = generate_tool_config("basic")
        assert config["tools"]["gateway"]["enabled"] is False

    def test_generate_config_disables_exec_for_basic(self):
        """Basic tier must not have exec enabled."""
        config = generate_tool_config("basic")
        assert config["tools"]["exec"]["enabled"] is False

    def test_generate_config_enables_exec_with_sandbox_for_plus(self):
        """Plus tier gets exec but sandboxed."""
        config = generate_tool_config("plus")
        assert config["tools"]["exec"]["enabled"] is True
        assert config["tools"]["exec"]["sandbox"] is True
        assert config["tools"]["exec"]["networkAccess"] is False
        assert "openclaw" in config["tools"]["exec"]["blockedCommands"]

    def test_blocked_tools_list_is_stable(self):
        """Blocked list should never shrink (catch accidental removals)."""
        # Minimum set that must always be blocked
        minimum_blocked = {
            "gateway", "cron", "sessions_spawn", "sessions_send",
            "sessions_list", "sessions_history",
        }
        assert minimum_blocked.issubset(BLOCKED_TOOLS)

    def test_unknown_tier_defaults_to_basic(self):
        """Unknown tier should behave like basic (no extras)."""
        allowed = get_allowed_tools("unknown_tier")
        assert "exec" not in allowed
        assert "browser" not in allowed
        # But basic tools still work
        assert "web_search" in allowed
