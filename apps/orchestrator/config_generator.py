"""Generate OpenClaw config from tenant parameters.

Based on actual OpenClaw config schema — see openclaw.json reference.
"""
from __future__ import annotations

import json
from typing import Any

from django.conf import settings

from apps.orchestrator.tool_policy import generate_tool_config
from apps.tenants.models import Tenant

_MORNING_BRIEFING_PROMPT = (
    "Good morning! Create today's morning briefing.\n\n"
    "Use your tools to gather context:\n"
    "1. Get today's weather for the user's location\n"
    "2. Check their calendar for today's events (nbhd_calendar_list_events)\n"
    "3. Check for important unread emails (nbhd_gmail_list_messages)\n"
    "4. Load recent journal context (nbhd_journal_context)\n\n"
    "Then write to today's daily note:\n"
    "- Use nbhd_daily_note_set_section with section_slug='morning-report' for the main briefing\n"
    "- Use nbhd_daily_note_set_section with section_slug='weather' for weather\n"
    "- Use nbhd_daily_note_set_section with section_slug='focus' for today's priorities\n\n"
    "Finally, send a friendly morning summary highlighting the key things for today. "
    "Keep it concise, warm, and actionable."
)

_EVENING_CHECKIN_PROMPT = (
    "It's evening check-in time.\n\n"
    "Send the user a friendly message asking about their day. Prompt them with:\n"
    "- What went well today?\n"
    "- Anything on their mind they want to capture?\n"
    "- Any tasks or notes for tomorrow?\n\n"
    "Keep it casual and warm — like a friend checking in, not a form to fill out.\n"
    "Don't write to the journal yet. Just start the conversation.\n"
    "If they share reflections, log them using nbhd_daily_note_set_section "
    "with section_slug='evening-check-in'."
)

_BACKGROUND_TASKS_PROMPT = (
    "Background maintenance run. Perform these tasks silently:\n\n"
    "1. Load recent context: nbhd_journal_context\n"
    "2. Review long-term memory (nbhd_memory_get) and recent daily notes\n"
    "3. Curate long-term memory if there are new patterns, preferences, or insights "
    "(nbhd_memory_update)\n"
    "4. Check recent daily notes for any unaddressed user requests or tasks\n"
    "5. If you find pending items, append a reminder to tomorrow's daily note "
    "(nbhd_daily_note_append with tomorrow's date)\n\n"
    "Do NOT message the user. This is a silent background run.\n"
    "Reply with a brief internal summary of what you did."
)


# Model mapping by tier
TIER_MODELS: dict[str, dict[str, str]] = {
    "starter": {"primary": "moonshot/kimi-k2.5"},
    "premium": {"primary": "anthropic/claude-sonnet-4-20250514"},
    "byok": {"primary": "anthropic/claude-sonnet-4-20250514"},  # fallback, overridden by user config
}

TIER_MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "starter": {
        "moonshot/kimi-k2.5": {"alias": "kimi"},
    },
    "premium": {
        "anthropic/claude-sonnet-4-20250514": {"alias": "sonnet"},
        "anthropic/claude-opus-4-20250514": {"alias": "opus"},
    },
    "byok": {},  # populated dynamically from user's config
}

WHISPER_DEFAULT_MODEL = {"provider": "openai", "model": "gpt-4o-mini-transcribe"}


def _build_models_providers(tier: str, tenant: Tenant) -> dict:
    """Return models.providers config for non-built-in providers."""
    providers: dict[str, Any] = {}
    if tier == "starter":
        providers["moonshot"] = {
            "baseUrl": "https://api.moonshot.ai/v1",
            "apiKey": "${MOONSHOT_API_KEY}",
            "api": "openai-completions",
            "models": [{"id": "kimi-k2.5", "name": "Kimi K2.5"}],
        }
    return providers


def _build_cron_jobs(tenant: Tenant) -> dict:
    """Build cron jobs for subscriber's OpenClaw config."""
    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")

    jobs = [
        {
            "name": "Morning Briefing",
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": user_tz},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _MORNING_BRIEFING_PROMPT,
            },
            "delivery": {"mode": "announce", "channel": "telegram"},
            "enabled": True,
        },
        {
            "name": "Evening Check-in",
            "schedule": {"kind": "cron", "expr": "0 21 * * *", "tz": user_tz},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _EVENING_CHECKIN_PROMPT,
            },
            "delivery": {"mode": "announce", "channel": "telegram"},
            "enabled": True,
        },
        {
            "name": "Background Tasks",
            "schedule": {"kind": "cron", "expr": "0 2 * * *", "tz": user_tz},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _BACKGROUND_TASKS_PROMPT,
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        },
    ]

    return {"jobs": jobs}


def _build_tools_section(tier: str) -> dict[str, Any]:
    """Build documented OpenClaw tools policy for subscriber tier."""
    tools = generate_tool_config(tier)
    tools["media"] = {
        "audio": {
            "enabled": True,
            "models": [WHISPER_DEFAULT_MODEL],
        },
    }
    return tools


def generate_openclaw_config(tenant: Tenant) -> dict[str, Any]:
    """Generate a complete openclaw.json for a tenant's container.

    This is the config that gets written to the container's
    ~/.openclaw/openclaw.json (or mounted via Azure Files).
    """
    chat_id = tenant.user.telegram_chat_id  # may be None before Telegram linking
    tier = tenant.model_tier or "starter"
    models_config = TIER_MODELS.get(tier, TIER_MODELS["starter"])
    model_entries = TIER_MODEL_CONFIGS.get(tier, TIER_MODEL_CONFIGS["starter"])
    # Collect all configured plugins
    _plugin_defs = [
        (
            str(getattr(settings, "OPENCLAW_GOOGLE_PLUGIN_ID", "") or "").strip(),
            str(getattr(settings, "OPENCLAW_GOOGLE_PLUGIN_PATH", "") or "").strip(),
        ),
        (
            str(getattr(settings, "OPENCLAW_JOURNAL_PLUGIN_ID", "") or "").strip(),
            str(getattr(settings, "OPENCLAW_JOURNAL_PLUGIN_PATH", "") or "").strip(),
        ),
    ]
    _active_plugins = [(pid, ppath) for pid, ppath in _plugin_defs if pid]

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
                    "memoryFlush": {
                        "enabled": True,
                        "softThresholdTokens": 4000,
                        "systemPrompt": (
                            "Session nearing compaction. Save important context now. "
                            "Use nbhd_memory_update for lasting insights about the user. "
                            "Use nbhd_daily_note_append for today's notable events. "
                            "Also write a brief session summary to memory/YYYY-MM-DD.md as a workspace backup."
                        ),
                        "prompt": (
                            "Review this conversation for anything worth remembering. "
                            "Save lasting insights via nbhd_memory_update, today's events via nbhd_daily_note_append, "
                            "and a brief summary to your workspace memory file. "
                            "Reply with NO_REPLY when done."
                        ),
                    },
                },
                "memorySearch": {
                    "enabled": True,
                    # Auto-detects OpenAI for embeddings via OPENAI_API_KEY
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

        # Cron jobs
        "cron": _build_cron_jobs(tenant),
    }

    # Note: BRAVE_API_KEY is injected as a container env var via Key Vault
    # reference (see azure_client.py). OpenClaw reads it automatically.

    providers = _build_models_providers(tier, tenant)
    if providers:
        config["models"] = {"mode": "merge", "providers": providers}

    # BYOK: inject user's own provider config
    if tier == "byok":
        try:
            from apps.tenants.models import UserLLMConfig
            from apps.tenants.crypto import decrypt_api_key

            llm_config = UserLLMConfig.objects.get(user=tenant.user)
            if llm_config.encrypted_api_key:
                api_key = decrypt_api_key(llm_config.encrypted_api_key)
                provider = llm_config.provider
                model_id = llm_config.model_id

                ENV_KEY_MAP = {
                    "openai": "OPENAI_API_KEY",
                    "anthropic": "ANTHROPIC_API_KEY",
                    "groq": "GROQ_API_KEY",
                    "google": "GEMINI_API_KEY",
                    "openrouter": "OPENROUTER_API_KEY",
                    "xai": "XAI_API_KEY",
                }
                env_key = ENV_KEY_MAP.get(provider)
                if env_key:
                    config.setdefault("env", {})[env_key] = api_key

                if model_id:
                    config["agents"]["defaults"]["model"]["primary"] = model_id
                    config["agents"]["defaults"]["models"] = {
                        model_id: {"alias": provider},
                    }
        except UserLLMConfig.DoesNotExist:
            pass  # Fall back to default tier model

    if _active_plugins:
        plugin_config: dict[str, Any] = {
            "allow": [pid for pid, _ in _active_plugins],
            "entries": {
                pid: {"enabled": True}
                for pid, _ in _active_plugins
            },
        }
        paths = [ppath for _, ppath in _active_plugins if ppath]
        if paths:
            plugin_config["load"] = {"paths": paths}

        config["plugins"] = plugin_config
        config["tools"]["alsoAllow"] = ["group:plugins"]

    return config


def config_to_json(config: dict[str, Any]) -> str:
    """Serialize config to JSON string."""
    return json.dumps(config, indent=2)
