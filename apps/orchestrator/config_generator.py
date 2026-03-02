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
    "Good morning! Create today's morning briefing. This is a cron (isolated) session — "
    "you cannot have a back-and-forth conversation. You must do everything in ONE turn.\n\n"
    "⚠️ NEWS DATE RULE: Before including any news item, check its publication date. "
    "Only include articles published in the last 24 hours. "
    "Never say 'yesterday' or 'today' about a story unless you've confirmed the date. "
    "Instead, always include the actual date: 'Man United sacked their manager (Jan 12)' — "
    "not 'Man United sacked their manager yesterday.' "
    "Stale news presented as current is worse than no news.\n\n"
    "Steps:\n"
    "1. Get weather using: curl -s 'wttr.in/{city}?format=%c+%t+%h+%w' for current conditions, "
    "or curl -s 'wttr.in/{city}?format=3' for a quick summary. "
    "For a detailed forecast: curl -s 'wttr.in/{city}?format=v2'. Replace {city} with the user's location.\n"
    "2. Check their calendar for today's events and upcoming 48hrs\n"
    "3. Check for important unread emails or messages\n"
    "4. Load recent journal context — what happened yesterday, any carry-over tasks?\n"
    "5. Load the user's goals (`nbhd_document_get` with kind='goal', slug='goals') for active goals context.\n"
    "6. Check news/topics the user follows (if configured) — use freshness filters (past 24h) "
    "and always verify publication dates before including\n\n"
    "7. Fill in today's daily note sections:\n\n"
    "**morning-report section:**\n"
    "### Overnight Summary\n"
    "- What happened since last check-in (completed tasks, messages, events)\n\n"
    "### Calendar Today\n"
    "- List today's events with times\n\n"
    "### Reminders & Follow-ups\n"
    "- Anything carried over from yesterday, upcoming deadlines, things to remember\n\n"
    "**weather section:**\n"
    "**Today:** temp, conditions, what to wear\n"
    "**Tomorrow:** brief forecast\n\n"
    "**news section:**\n"
    "### Headlines\n"
    "- 2-3 relevant headlines (tech, world, topics they care about)\n\n"
    "### Topics You Follow\n"
    "- Updates on specific interests (sports scores, market moves, etc.)\n\n"
    "**focus section:**\n"
    "### Active Goals\n"
    "- List active goals with a one-line status (progressing / stalled / new)\n"
    "- Skip this if there are no active goals\n\n"
    "### Top 3 Priorities\n"
    "- Based on goals, calendar, carry-over tasks, and what makes sense for the day\n\n"
    "### Quick Wins\n"
    "- Small things that can be knocked out easily\n\n"
    "8. Send the user exactly ONE message via `nbhd_send_to_user`. Keep it concise:\n"
    "- Weather + what to wear (1 line)\n"
    "- Top priority for the day (1 line)\n"
    "- Anything time-sensitive (1-2 lines)\n"
    "- Full details are in the journal\n\n"
    "When writing daily note sections, include the local target date if supported by your tool call. "
    "Use YYYY-MM-DD in the user's timezone context when passing `date` explicitly (avoid UTC drift).\n\n"
    "Note: These are default sections. The user may customize or remove them — "
    "only fill in sections that exist in their template.\n\n"
    "**IMPORTANT: Send exactly ONE message. Do not send multiple messages.**\n"
)

_EVENING_CHECKIN_PROMPT = (
    "It's evening check-in time. This is a cron (isolated) session — you cannot have a "
    "back-and-forth conversation. You must do everything in ONE turn.\n\n"
    "Steps:\n"
    "1. Load today's journal context (`nbhd_journal_context`) to see what the user did today.\n"
    "2. Load the user's goals document (`nbhd_document_get` with kind='goal', slug='goals').\n"
    "3. Review today's conversations for things the user learned. "
    "If you find notable lessons or insights, suggest them via `nbhd_lesson_suggest`.\n"
    "4. Fill in the 'evening-check-in' section of today's daily note "
    "(`nbhd_daily_note_set_section` with section='evening-check-in') using this structure:\n"
    "### What got done today?\n"
    "- ✅ Item (brief description)\n\n"
    "### Goal progress\n"
    "- Note any active goals that saw progress today, or flag ones that are stalling\n"
    "- Skip this section if there are no active goals\n\n"
    "### What didn't get done? Why?\n"
    "- ❌ Item — reason (only if you know from context)\n\n"
    "### Plan for tomorrow (top 3)\n"
    "1. Top priority\n"
    "2. Second priority\n"
    "3. Third priority\n\n"
    "### Energy/mood (1-10)\n"
    "- ? (leave as ? — the user fills this in)\n\n"
    "Use the local user date when writing with date arguments to avoid timezone drift.\n"
    "Fill in what you know from the day's conversations. Leave gaps for what you don't know.\n\n"
    "5. Send the user exactly ONE message via `nbhd_send_to_user`. Keep it short and casual:\n"
    "- Brief recap of their day (2-3 lines max)\n"
    "- If any active goals saw progress, mention it (one line)\n"
    "- One prompt: 'Anything to add or adjust before tomorrow?'\n"
    "- If you suggested lessons, mention it briefly\n\n"
    "**IMPORTANT: Send exactly ONE message. Do not send multiple messages.**\n"
)

_WEEK_AHEAD_REVIEW_PROMPT = (
    "It's Monday morning. Run the Week Ahead Review.\n\n"
    "1. Load journal context (`nbhd_journal_context`) and recent memory files\n"
    "2. Check the calendar for the upcoming 7 days (`nbhd_calendar_list_events`)\n"
    "3. List all active cron jobs (`cron list`)\n"
    "4. For each cron job, check: does this make sense given the user's week?\n"
    "   - If the user is traveling, skip or redirect location-based crons\n"
    "   - If the user has a packed schedule, consider adjusting timing\n"
    "   - If everything looks fine, note 'no changes needed'\n"
    "5. Before making any changes, tell the user what you found and ask.\n"
    "   Example: 'I see you're in Bali this week — want me to skip the local "
    "event search or look up things to do there instead?'\n"
    "6. Log decisions in `memory/week-ahead/` with a brief note\n\n"
    "Be helpful, not noisy. Send your summary to the user via `nbhd_send_to_user`. "
    "If nothing conflicts, just send a quick 'All good for this week, no changes needed.'"
)

_BACKGROUND_TASKS_PROMPT = (
    "Background maintenance run. Perform these tasks silently:\n\n"
    "1. Load recent journal context\n"
    "2. Review long-term memory and recent daily notes\n"
    "3. Curate long-term memory if there are new patterns, preferences, or insights\n"
    "4. Check recent daily notes for any unaddressed user requests or tasks\n"
    "5. If you find pending items, append a reminder to tomorrow's daily note\n"
    "6. Check the lessons constellation — if there are new approved lessons, the clusters and positions may need refreshing. The system handles this automatically.\n\n"
    "Do NOT message the user. This is a silent background run.\n"
    "Log a brief summary of what you did to tomorrow's daily note."
)

# Model mapping by tier
TIER_MODELS: dict[str, dict[str, str]] = {
    "starter": {"primary": "openrouter/moonshotai/kimi-k2.5"},
    "premium": {"primary": "anthropic/claude-sonnet-4-20250514"},
    "byok": {"primary": "anthropic/claude-sonnet-4-20250514"},  # fallback, overridden by user config
}

TIER_MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "starter": {
        "openrouter/moonshotai/kimi-k2.5": {"alias": "kimi"},
    },
    "premium": {
        "anthropic/claude-sonnet-4-20250514": {"alias": "sonnet"},
        "anthropic/claude-opus-4-20250514": {"alias": "opus"},
    },
    "byok": {},  # populated dynamically from user's config
}

WHISPER_DEFAULT_MODEL = {"provider": "openai", "model": "gpt-4o-mini-transcribe"}



def build_cron_seed_jobs(tenant: Tenant) -> list[dict]:
    """Build cron job definitions for seeding via the Gateway API.

    OpenClaw's config schema only accepts runtime settings (``enabled``,
    ``store``, etc.) under the ``cron`` key — job definitions must be
    provisioned through the Gateway ``POST /api/cron/jobs`` endpoint.
    Called by ``seed_cron_jobs()`` in ``services.py``.
    """
    # Use the user's real timezone if set; fall back to UTC only as a last
    # resort.  The agent will ask the user for their timezone on first
    # interaction (see AGENTS.md) and the sync in runtime_views will
    # delete+recreate these jobs with the correct tz at that point.
    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")

    return [
        {
            "name": "Morning Briefing",
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": user_tz},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _MORNING_BRIEFING_PROMPT,
            },
            "delivery": {"mode": "none"},
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
            "delivery": {"mode": "none"},
            "enabled": True,
        },
        {
            "name": "Week Ahead Review",
            "schedule": {"kind": "cron", "expr": "0 8 * * 1", "tz": user_tz},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _WEEK_AHEAD_REVIEW_PROMPT,
            },
            "delivery": {"mode": "none"},
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
        {
            "name": "Nightly Extraction",
            # 30 minutes after Evening Check-in so the check-in has time to
            # write to the daily note before extraction reads it.
            "schedule": {"kind": "cron", "expr": "30 21 * * *", "tz": user_tz},
            "sessionTarget": "none",
            "payload": {
                "kind": "webhook",
                "url": "/api/v1/journal/extract/",
                "body": {"tenant_id": str(tenant.id)},
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        },
    ]



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
        (
            str(
                getattr(settings, "OPENCLAW_USAGE_PLUGIN_ID", "")
                or getattr(settings, "OPENCLAW_USAGE_REPORTER_PLUGIN_ID", "")
            ).strip(),
            str(getattr(settings, "OPENCLAW_USAGE_REPORTER_PLUGIN_PATH", "") or "").strip(),
        ),
        (
            str(getattr(settings, "OPENCLAW_IMAGE_GEN_PLUGIN_ID", "") or "").strip(),
            str(getattr(settings, "OPENCLAW_IMAGE_GEN_PLUGIN_PATH", "") or "").strip(),
        ),
    ]
    # Reddit plugin — conditionally loaded only when tenant has an active Reddit connection
    from apps.integrations.models import Integration as _Integration
    _reddit_connected = _Integration.objects.filter(
        tenant=tenant,
        provider="reddit",
        status=_Integration.Status.ACTIVE,
    ).exists()
    if _reddit_connected:
        _plugin_defs.append((
            str(getattr(settings, "OPENCLAW_REDDIT_PLUGIN_ID", "nbhd-reddit-tools") or "").strip(),
            str(getattr(settings, "OPENCLAW_REDDIT_PLUGIN_PATH", "/opt/nbhd/plugins/nbhd-reddit-tools") or "").strip(),
        ))

    _active_plugins = [(pid, ppath) for pid, ppath in _plugin_defs if pid]

    api_base = str(getattr(settings, "API_BASE_URL", "") or "").strip().rstrip("/")
    webhook_secret = str(getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "") or "").strip()

    config: dict[str, Any] = {
        # Auth — provider tokens read from env vars automatically
        "auth": {
            "profiles": {
                "anthropic:default": {
                    "provider": "anthropic",
                    "mode": "token",
                },
                "openrouter:default": {
                    "provider": "openrouter",
                    "mode": "token",
                    # Token read from OPENROUTER_API_KEY env var automatically
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

        # Telegram channel — the central Django poller handles actual
        # Telegram I/O, but we declare the channel here so the agent knows
        # its surface capabilities (inline buttons, reactions, etc.).
        # No bot token is set — the container never connects to Telegram directly.
        "channels": {
            "telegram": {
                "enabled": True,
                "capabilities": ["inlineButtons"],
            },
        },

        # Gateway — local mode; bind to loopback so internal tool calls
        # (cron, etc.) auto-pair via localhost.  The OpenClaw proxy sidecar
        # (listening on 0.0.0.0:8080) handles external traffic forwarding.
        # Auth token read from NBHD_INTERNAL_API_KEY env var (per-tenant
        # Key Vault secret) so Django can call /tools/invoke for cron CRUD.
        #
        # The OpenAI-compatible /v1/chat/completions endpoint is enabled
        # so the central Telegram poller can forward messages here.
        "gateway": {
            "port": 18789,
            "mode": "local",
            "bind": "loopback",
            "auth": {
                "mode": "token",
                "token": "${NBHD_INTERNAL_API_KEY}",
            },
            "http": {
                "endpoints": {
                    "chatCompletions": {"enabled": True},
                },
            },
        },

        # Tools
        "tools": _build_tools_section(tier),

        # Messages
        "messages": {
            "ackReactionScope": "group-mentions",
        },

        # Cron runtime settings (job definitions seeded via Gateway API)
        "cron": {"enabled": True},
    }

    # Note: BRAVE_API_KEY is injected as a container env var via Key Vault
    # reference (see azure_client.py). OpenClaw reads it automatically.

    # Note: OPENROUTER_API_KEY is injected as a container env var via Key Vault
    # reference (see azure_client.py). OpenClaw reads it automatically for
    # models routed through OpenRouter (e.g. openrouter/moonshotai/kimi-k2.5).

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
        image_gen_id = str(getattr(settings, "OPENCLAW_IMAGE_GEN_PLUGIN_ID", "") or "").strip()
        plugin_config: dict[str, Any] = {
            "allow": [pid for pid, _ in _active_plugins],
            "entries": {
                pid: (
                    {"enabled": True, "config": {"tier": tier}}
                    if pid == image_gen_id
                    else {"enabled": True}
                )
                for pid, _ in _active_plugins
            },
        }
        paths = [ppath for _, ppath in _active_plugins if ppath]
        if paths:
            plugin_config["load"] = {"paths": paths}

        config["plugins"] = plugin_config
        # Merge group:plugins into the existing allow list (not alsoAllow)
        # to avoid the allow/alsoAllow conflict that OpenClaw rejects.
        allow = config["tools"].get("allow", [])
        if "group:plugins" not in allow:
            allow.append("group:plugins")
            config["tools"]["allow"] = allow

    return config


def config_to_json(config: dict[str, Any]) -> str:
    """Serialize config to JSON string."""
    return json.dumps(config, indent=2)
