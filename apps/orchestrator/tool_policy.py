"""OpenClaw tool policy for subscriber tenants.

Policy intentionally uses documented config keys:
- tools.allow
- tools.deny
- tools.elevated

Version-aware: tool groups changed in OpenClaw 2026.4.15
(group:automation folded into group:openclaw).
"""

from __future__ import annotations

from typing import Any


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse 'YYYY.M.D' version string into a comparable tuple."""
    return tuple(int(x) for x in v.split("."))


# ── 2026.4.5 policy (original) ──────────────────────────────────────

_DENIED_TOOLS_2026_4_5: tuple[str, ...] = (
    "gateway",
    "sessions_spawn",
    "sessions_send",
    "sessions_list",
    "sessions_history",
    "session_status",
    "agents_list",
)

_STARTER_ALLOW_2026_4_5: tuple[str, ...] = (
    "group:web",
    "group:plugins",
    "group:automation",
    "tts",
    "image",
)

# ── 2026.4.15 policy ────────────────────────────────────────────────
# group:automation folded into group:openclaw; expanded deny list
# for tools with no surface in Telegram-only containers.

_DENIED_TOOLS_2026_4_15: tuple[str, ...] = _DENIED_TOOLS_2026_4_5 + (
    "sessions_yield",
    "subagents",
    "message",
    "browser",
    "canvas",
    "nodes",
    "code_execution",
    "music_generate",
    "video_generate",
)

_STARTER_ALLOW_2026_4_15: tuple[str, ...] = (
    "group:openclaw",
    "group:plugins",
)

# ── Version registry (newest first) ─────────────────────────────────

_POLICY_VERSIONS: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    ("2026.4.15", _STARTER_ALLOW_2026_4_15, _DENIED_TOOLS_2026_4_15),
    ("2026.4.5", _STARTER_ALLOW_2026_4_5, _DENIED_TOOLS_2026_4_5),
]

# ── Backward-compat aliases (imported by existing tests) ────────────

DENIED_TOOLS = _DENIED_TOOLS_2026_4_5
STARTER_ALLOW = _STARTER_ALLOW_2026_4_5


def _resolve_policy(version: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (allow, deny) for the given OpenClaw version."""
    v = _parse_version(version)
    for entry_version, allow, deny in _POLICY_VERSIONS:
        if v >= _parse_version(entry_version):
            return allow, deny
    # Fallback to oldest known policy
    return _POLICY_VERSIONS[-1][1], _POLICY_VERSIONS[-1][2]


def get_allowed_tools(tier: str = "starter", version: str = "2026.4.21") -> list[str]:
    """Return documented allow-list entries for a subscriber tier."""
    allow, _ = _resolve_policy(version)
    return list(allow)


def get_denied_tools(version: str = "2026.4.21") -> list[str]:
    """Return the deny-list for the given OpenClaw version."""
    _, deny = _resolve_policy(version)
    return list(deny)


def generate_tool_config(tier: str = "starter", version: str = "2026.4.21") -> dict[str, Any]:
    """Generate the OpenClaw `tools` config block for subscriber tenants."""
    return {
        "allow": get_allowed_tools(tier, version=version),
        "deny": get_denied_tools(version=version),
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
