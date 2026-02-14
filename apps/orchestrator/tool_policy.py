"""Tool policy for subscriber agents.

Defines which OpenClaw tools are available to subscriber agents vs. the
platform admin. Subscriber agents must NEVER have access to runtime
management tools (gateway updates, config changes, restarts).

The generated policy feeds into the OpenClaw config's tools/agents section,
restricting what the agent can do.
"""
from __future__ import annotations

from typing import Any


# ─── Danger Zone ────────────────────────────────────────────────────────
# These tools let an agent modify the OpenClaw runtime itself.
# A subscriber saying "update yourself" would update the actual container,
# potentially breaking the service for everyone on the same host.
#
# NEVER grant these to subscriber agents.
BLOCKED_TOOLS = frozenset({
    "gateway",          # restart, config.apply, config.patch, update.run
    "cron",             # scheduled jobs — platform manages these, not users
    "sessions_spawn",   # sub-agent spawning — cost/resource risk
    "sessions_send",    # cross-session messaging — isolation risk
    "sessions_list",    # can see other sessions on the gateway
    "sessions_history", # can read other sessions' history
    "session_status",   # exposes model/config internals
    "agents_list",      # exposes platform agent config
})

# Tools subscribers CAN use, gated by tier or integration status.
# Each entry: tool_name -> {tier, requires_integration?, description}
SUBSCRIBER_TOOLS: dict[str, dict[str, Any]] = {
    # ── Always available ──
    "web_search": {
        "tier": "basic",
        "description": "Search the web",
    },
    "web_fetch": {
        "tier": "basic",
        "description": "Fetch and read web pages",
    },
    "memory_search": {
        "tier": "basic",
        "description": "Search agent memory files",
    },
    "memory_get": {
        "tier": "basic",
        "description": "Read agent memory snippets",
    },
    "read": {
        "tier": "basic",
        "description": "Read workspace files",
    },
    "write": {
        "tier": "basic",
        "description": "Write workspace files",
    },
    "edit": {
        "tier": "basic",
        "description": "Edit workspace files",
    },
    "message": {
        "tier": "basic",
        "description": "Send messages via configured channel",
    },
    "tts": {
        "tier": "basic",
        "description": "Text-to-speech",
    },

    # ── Requires integration ──
    "browser": {
        "tier": "plus",
        "description": "Browser automation",
    },
    "exec": {
        "tier": "plus",
        "description": "Run sandboxed shell commands",
        # NOTE: When enabled, must run in restricted sandbox (no network,
        # no access outside workspace). See sandbox_policy below.
    },
    "image": {
        "tier": "basic",
        "description": "Analyze images with vision model",
    },
}

# Exec sandbox restrictions when exec IS enabled (plus tier).
# These map to OpenClaw config's tools.exec section.
EXEC_SANDBOX_POLICY: dict[str, Any] = {
    "sandbox": True,           # Run in sandboxed mode
    "networkAccess": False,    # No outbound network from exec
    "allowedPaths": [          # Only workspace access
        "/home/node/.openclaw/workspace",
    ],
    "blockedCommands": [       # Prevent escape attempts
        "curl", "wget", "nc", "ncat", "ssh", "scp",
        "docker", "podman",
        "openclaw",            # Can't manage the runtime
        "npm", "npx",          # Can't install packages
        "pip", "pip3",
    ],
    "timeout": 30,             # Max 30s per command
}


def get_allowed_tools(tier: str = "basic") -> list[str]:
    """Return list of tool names available for a given tier."""
    allowed = []
    for tool_name, config in SUBSCRIBER_TOOLS.items():
        tool_tier = config["tier"]
        # basic tools available to all, plus tools only to plus
        if tool_tier == "basic" or (tool_tier == "plus" and tier == "plus"):
            allowed.append(tool_name)
    return allowed


def get_blocked_tools() -> list[str]:
    """Return list of tools that must NEVER be available to subscribers."""
    return sorted(BLOCKED_TOOLS)


def generate_tool_config(tier: str = "basic") -> dict[str, Any]:
    """Generate the tools section for an OpenClaw subscriber config.

    This produces the config that goes into openclaw.json under "tools"
    and "agents.defaults" to restrict what the agent can do.
    """
    allowed = get_allowed_tools(tier)

    config: dict[str, Any] = {
        "tools": {
            "web": {
                "search": {"enabled": "web_search" in allowed},
                "fetch": {"enabled": "web_fetch" in allowed},
            },
            # Explicitly disable dangerous tools
            "gateway": {"enabled": False},
        },
        "agent_tool_policy": {
            # OpenClaw reads this to filter available tools
            "allowed": allowed,
            "blocked": list(BLOCKED_TOOLS),
        },
    }

    # Add exec sandbox if exec is allowed
    if "exec" in allowed:
        config["tools"]["exec"] = {
            "enabled": True,
            **EXEC_SANDBOX_POLICY,
        }
    else:
        config["tools"]["exec"] = {"enabled": False}

    return config
