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

# Basic tier: non-destructive helper surface only.
BASIC_ALLOW: tuple[str, ...] = (
    "group:network",
    "group:memory",
    "group:files",
    "group:messaging",
    "group:tts",
    "group:image",
)

# Plus tier adds browser automation and sandboxed exec capability.
PLUS_ALLOW: tuple[str, ...] = BASIC_ALLOW + (
    "group:browser",
    "exec",
)


def get_allowed_tools(tier: str = "basic") -> list[str]:
    """Return documented allow-list entries for a subscriber tier."""
    normalized = (tier or "basic").lower()
    if normalized == "plus":
        return list(PLUS_ALLOW)
    return list(BASIC_ALLOW)


def get_denied_tools() -> list[str]:
    """Return the deny-list used for all subscriber tiers."""
    return list(DENIED_TOOLS)


def generate_tool_config(tier: str = "basic") -> dict[str, Any]:
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
