"""OpenClaw tool policy for subscriber tenants.

Policy intentionally uses documented config keys:
- tools.allow
- tools.deny
- tools.elevated
"""

from __future__ import annotations

from typing import Any

# Deny runtime-management, cross-session controls, direct messaging,
# and tools with no surface in the Telegram-only subscriber environment.
#
# Why deny instead of just not allowing?  We use group:openclaw as the
# allow-list base (covers web, memory, cron, media, etc.), so individual
# deny entries carve out what subscribers should NOT touch.
#
# NOTE on messaging: tenant containers have no Telegram bot token —
# direct channel delivery always fails.  All outbound messages MUST go
# through nbhd_send_to_user (plugin), which proxies via the central
# Django bot.
DENIED_TOOLS: tuple[str, ...] = (
    # Runtime management / cross-session (unchanged from pre-2026.4.15)
    "gateway",
    "sessions_spawn",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "session_status",
    "sessions_yield",
    "subagents",
    "agents_list",
    # Direct messaging — must use nbhd_send_to_user plugin instead
    "message",
    # No browser or canvas surface in Telegram-only containers
    "browser",
    "canvas",
    # Infrastructure / remote execution — not for subscribers
    "nodes",
    "code_execution",
    # Media generation — keep image_generate + tts, deny the rest
    "music_generate",
    "video_generate",
)

# Starter tier: group:openclaw covers web, memory, cron, media, and
# plan tools.  group:plugins covers NBHD journal/google/finance tools.
#
# Deny list above carves out what subscribers shouldn't access.
# Deny takes precedence over allow in OpenClaw's tool policy resolver.
#
# Key additions over pre-2026.4.15 policy:
#   - memory_search, memory_get (via group:openclaw) — better recall
#   - update_plan (via group:openclaw) — structured multi-step tracking
#   - image_generate (via group:openclaw) — image creation
#
# NOTE: group:fs (read/write/edit/apply_patch) and exec/process are NOT
# in group:openclaw — they require the "coding" profile and are absent
# from our subscriber containers.  No deny entry needed.
STARTER_ALLOW: tuple[str, ...] = (
    "group:openclaw",
    "group:plugins",
)


def get_allowed_tools(tier: str = "starter") -> list[str]:
    """Return documented allow-list entries for a subscriber tier."""
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
