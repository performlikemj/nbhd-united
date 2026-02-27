"""OpenClaw tool policy for subscriber tenants.

Policy intentionally uses documented config keys:
- tools.allow
- tools.deny
- tools.elevated
"""
from __future__ import annotations

from typing import Any

# Deny runtime-management and cross-session controls for subscribers.
# Cron tools are intentionally ALLOWED so users can manage scheduled tasks
# via the agent or the settings UI.
DENIED_TOOLS: tuple[str, ...] = (
    "gateway",
    "sessions_spawn",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "session_status",
    "agents_list",
)

# Starter tier: non-destructive helper surface only.
# Group names must match OpenClaw docs: group:web, group:fs, group:memory,
# group:messaging, group:automation.  "tts" and "image" are standalone tools.
#
# NOTE: group:fs and group:memory are intentionally EXCLUDED.
# Subscribers should not interact with raw workspace files.
# All persistence goes through NBHD journal tools (group:plugins)
# which write to the Django database — visible on the journal UI.
#
# NOTE: group:messaging is intentionally EXCLUDED.
# Tenant containers have no Telegram bot token — direct channel delivery
# always fails.  All outbound messages (cron jobs, proactive sends) MUST
# go through the plugin tool nbhd_send_to_user, which proxies via the
# central Django bot.  Blocking group:messaging here forces the agent to
# use that path regardless of how the cron job was created.
STARTER_ALLOW: tuple[str, ...] = (
    "group:web",
    "group:plugins",
    "tts",
    "image",
    "cron",
)

# Premium tier adds browser automation and sandboxed exec capability.
PREMIUM_ALLOW: tuple[str, ...] = STARTER_ALLOW + (
    "group:ui",
)

# Legacy aliases for backward compatibility in tests
BASIC_ALLOW = STARTER_ALLOW
PLUS_ALLOW = PREMIUM_ALLOW


def get_allowed_tools(tier: str = "starter") -> list[str]:
    """Return documented allow-list entries for a subscriber tier."""
    normalized = (tier or "starter").lower()
    if normalized in ("premium", "byok"):
        return list(PREMIUM_ALLOW)
    return list(STARTER_ALLOW)


def get_denied_tools() -> list[str]:
    """Return the deny-list used for all subscriber tiers."""
    return list(DENIED_TOOLS)


def generate_tool_config(tier: str = "starter") -> dict[str, Any]:
    """Generate the OpenClaw `tools` config block for subscriber tenants."""
    return {
        "allow": get_allowed_tools(tier),
        "deny": get_denied_tools(),
        # Prevent host-elevated execution for subscriber agents.
        "elevated": {
            "enabled": False,
        },
        # Keep web search explicitly enabled for deterministic behavior.
        "web": {
            "search": {
                "enabled": True,
            },
        },
    }
