"""Generate OpenClaw config from tenant parameters.

Based on actual OpenClaw config schema — see openclaw.json reference.
"""
from __future__ import annotations

import json
from typing import Any

from django.conf import settings

from apps.orchestrator.tool_policy import generate_tool_config
from apps.tenants.models import Tenant

# Model mapping by tier
TIER_MODELS: dict[str, dict[str, str]] = {
    "basic": {
        "primary": "anthropic/claude-sonnet-4-20250514",
    },
    "plus": {
        "primary": "anthropic/claude-sonnet-4-20250514",
        # Plus users can also use Opus
    },
}

TIER_MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "basic": {
        "anthropic/claude-sonnet-4-20250514": {"alias": "sonnet"},
    },
    "plus": {
        "anthropic/claude-sonnet-4-20250514": {"alias": "sonnet"},
        "anthropic/claude-opus-4-20250514": {"alias": "opus"},
    },
}


def _build_tools_section(tier: str) -> dict[str, Any]:
    """Build documented OpenClaw tools policy for subscriber tier."""
    return generate_tool_config(tier)


def generate_openclaw_config(tenant: Tenant) -> dict[str, Any]:
    """Generate a complete openclaw.json for a tenant's container.

    This is the config that gets written to the container's
    ~/.openclaw/openclaw.json (or mounted via Azure Files).
    """
    chat_id = tenant.user.telegram_chat_id  # may be None before Telegram linking
    tier = tenant.model_tier or "basic"
    models_config = TIER_MODELS.get(tier, TIER_MODELS["basic"])
    model_entries = TIER_MODEL_CONFIGS.get(tier, TIER_MODEL_CONFIGS["basic"])
    plugin_id = str(getattr(settings, "OPENCLAW_GOOGLE_PLUGIN_ID", "") or "").strip()
    plugin_path = str(getattr(settings, "OPENCLAW_GOOGLE_PLUGIN_PATH", "") or "").strip()

    api_base = str(getattr(settings, "API_BASE_URL", "") or "").strip().rstrip("/")
    webhook_secret = str(getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()

    config: dict[str, Any] = {
        # Auth — uses shared API key injected via env var
        "auth": {
            "profiles": {
                "anthropic:default": {
                    "provider": "anthropic",
                    "mode": "token",
                    # Token read from ANTHROPIC_API_KEY env var automatically
                },
            },
        },

        # Agent defaults
        "agents": {
            "defaults": {
                "model": {
                    "primary": models_config["primary"],
                },
                "models": model_entries,
                "workspace": "/home/node/.openclaw/workspace",
                "userTimezone": str(getattr(tenant.user, "timezone", "") or "UTC"),
                "compaction": {
                    "mode": "safeguard",
                },
                "heartbeat": {
                    # Disabled to save cost — agents are reactive only
                    "every": "0m",
                },
                "maxConcurrent": 2,
                "subagents": {
                    "maxConcurrent": 2,
                    "model": models_config["primary"],
                },
            },
        },

        # Telegram channel — locked to this user's chat_id
        # Webhook mode: OpenClaw serves POST /telegram-webhook on the gateway
        # port so our Django router can forward Telegram updates to it.
        "channels": {
            "telegram": {
                "name": tenant.user.display_name,
                "enabled": True,
                **(
                    {"dmPolicy": "allowlist", "allowFrom": [str(chat_id)]}
                    if chat_id is not None
                    else {"dmPolicy": "disabled"}
                ),
                "groupPolicy": "disabled",
                "streamMode": "partial",
                **(
                    {
                        "webhookUrl": f"{api_base}/api/v1/telegram/webhook/",
                        "webhookHost": "0.0.0.0",
                        "webhookSecret": webhook_secret,
                    }
                    if api_base and webhook_secret
                    else {}
                ),
            },
        },

        # Gateway — local mode, accessible to internal callers in the container network
        "gateway": {
            "port": 18789,
            "mode": "local",
            "bind": "lan",
        },

        # Tools
        "tools": _build_tools_section(tier),

        # Messages
        "messages": {
            "ackReactionScope": "group-mentions",
        },
    }

    if plugin_id:
        plugin_config: dict[str, Any] = {
            "allow": [plugin_id],
            "entries": {
                plugin_id: {
                    "enabled": True,
                },
            },
        }
        if plugin_path:
            plugin_config["load"] = {"paths": [plugin_path]}

        config["plugins"] = plugin_config
        config["tools"]["alsoAllow"] = ["group:plugins"]

    return config


def config_to_json(config: dict[str, Any]) -> str:
    """Serialize config to JSON string."""
    return json.dumps(config, indent=2)
