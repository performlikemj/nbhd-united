"""OpenClaw tool policy for subscriber tenants.

Policy intentionally uses documented config keys:
- tools.allow
- tools.deny
- tools.elevated
"""
from __future__ import annotations

from typing import Any

# Always deny runtime-management and cross-session controls for subscribers.
DENIED_TOOLS: tuple[str, ...] = (
    "group:automation",  # includes gateway + cron controls
    "gateway",
    "cron",
    "sessions_spawn",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "session_status",
    "agents_list",
)

# Starter tier: non-destructive helper surface only.
STARTER_ALLOW: tuple[str, ...] = (
    "group:network",
    "group:memory",
    "group:files",
    "group:messaging",
    "group:tts",
    "group:image",
)

# Premium tier adds browser automation and sandboxed exec capability.
PREMIUM_ALLOW: tuple[str, ...] = STARTER_ALLOW + (
    "group:browser",
    "exec",
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
