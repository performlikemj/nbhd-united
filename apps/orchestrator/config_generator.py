"""Generate OpenClaw config from tenant parameters.

Based on actual OpenClaw config schema — see openclaw.json reference.
"""
from __future__ import annotations

import json
import zoneinfo
from datetime import datetime
from typing import Any

from django.conf import settings

from apps.orchestrator.tool_policy import generate_tool_config
from apps.tenants.models import Tenant


def _inject_date_context(prompt: str, tenant: "Tenant") -> str:
    """Prepend current date/time context to a cron prompt.

    Cheaper models (M2.5, Kimi) struggle with date math. Injecting the
    current date server-side eliminates an entire class of errors:
    wrong day-of-week, "tomorrow" when event is 2 days away, etc.
    """
    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
    try:
        tz = zoneinfo.ZoneInfo(user_tz)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    now = datetime.now(tz)
    date_line = (
        f"Current date and time: {now.strftime('%A, %B %d, %Y at %H:%M')} ({user_tz})\n"
        f"When mentioning future events, compute exact days: "
        f"event_date minus {now.strftime('%Y-%m-%d')} = X days from now. "
        f"Never say 'tomorrow' unless the math confirms exactly 1 day away.\n\n"
    )
    return date_line + prompt

_MORNING_BRIEFING_PROMPT_TEMPLATE = (
    "Good morning! Create today's morning briefing. This is a cron (isolated) session — "
    "you cannot have a back-and-forth conversation. You must do everything in ONE turn.\n\n"
    "⚠️ NEWS DATE RULE: Before including any news item, check its publication date. "
    "Only include articles published in the last 24 hours. "
    "Never say 'yesterday' or 'today' about a story unless you've confirmed the date. "
    "Instead, always include the actual date: 'Man United sacked their manager (Jan 12)' — "
    "not 'Man United sacked their manager yesterday.' "
    "Stale news presented as current is worse than no news.\n\n"
    "Steps:\n"
    "1. Get weather using web_fetch with the pre-built Open-Meteo URL below. "
    "Do NOT use curl or exec — you don't have shell access. Use the web_fetch tool.\n"
    "   Weather URL: {weather_url}\n"
    "   WMO weather codes: 0=clear, 1-3=partly cloudy/overcast, 45-48=fog, "
    "51-55=drizzle, 61-65=rain, 71-75=snow, 80-82=rain showers, 95=thunderstorm.\n"
    "2. Check their calendar for today's events and upcoming 48hrs\n"
    "3. Check for important unread emails or messages\n"
    "4. Load recent journal context — what happened yesterday, any carry-over tasks?\n"
    "5. Load the user's goals (`nbhd_document_get` with kind='goal', slug='goals') for active goals context.\n"
    "6. Load the user's tasks document (`nbhd_document_get` with kind='tasks', slug='tasks'). "
    "Check which tasks are open (`- [ ]`) vs completed (`- [x]`). Only reference open tasks.\n"
    "7. Check news/topics the user follows (if configured) — use freshness filters (past 24h) "
    "and always verify publication dates before including\n\n"
    "8. VERIFICATION — before listing any carry-over item from yesterday:\n"
    "   - Load the tasks document — is it still marked open (`- [ ]`)?\n"
    "   - Check if the user addressed it in yesterday's evening check-in\n"
    "   - If the user said 'done' or 'drop it' in any conversation, do NOT carry it over\n"
    "   - Only list genuinely open items\n\n"
    "9. Fill in today's daily note sections:\n\n"
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
    "### Open Tasks\n"
    "- List incomplete tasks (unchecked `- [ ]` items) from the tasks document\n"
    "- Skip this if the tasks document is empty or has no open items\n\n"
    "### Top 3 Priorities\n"
    "- Based on goals, calendar, open tasks, and what makes sense for the day\n\n"
    "### Quick Wins\n"
    "- Small things that can be knocked out easily\n\n"
    "10. Send the user exactly ONE message via `nbhd_send_to_user`. Keep it concise:\n"
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

def _build_morning_briefing_prompt(tenant) -> str:
    """Build the morning briefing prompt with a pre-resolved weather URL.

    Uses stored user coordinates if available, falls back to timezone-based
    approximate coordinates.
    """
    from apps.orchestrator.weather import build_weather_url, build_weather_url_from_coords
    user = tenant.user
    user_tz = str(getattr(user, "timezone", "") or "UTC")

    # Prefer stored coordinates (set by user via nbhd_update_profile)
    lat = getattr(user, "location_lat", None)
    lon = getattr(user, "location_lon", None)
    if lat is not None and lon is not None:
        weather_url = build_weather_url_from_coords(lat, lon, user_tz)
    else:
        weather_url = build_weather_url(user_tz)

    return _MORNING_BRIEFING_PROMPT_TEMPLATE.format(weather_url=weather_url)


_EVENING_CHECKIN_PROMPT = (
    "It's evening check-in time. This is a cron (isolated) session — you cannot have a "
    "back-and-forth conversation. You must do everything in ONE turn.\n\n"
    "Steps:\n"
    "1. Load today's full daily note (`nbhd_daily_note_get` with today's date). "
    "Read the morning-report section — note the 'Top 3 Priorities' and 'Open Tasks'.\n"
    "2. Load today's journal context (`nbhd_journal_context`) to see what the user did today.\n"
    "3. Load the user's goals document (`nbhd_document_get` with kind='goal', slug='goals').\n"
    "4. Load the user's tasks document (`nbhd_document_get` with kind='tasks', slug='tasks'). "
    "Check which tasks are open (`- [ ]`) vs completed (`- [x]`).\n"
    "5. VERIFICATION — before listing any item as 'not done':\n"
    "   - Confirm it appears as `- [ ]` (unchecked) in the tasks document right now\n"
    "   - Confirm it was actually planned for today (check morning priorities)\n"
    "   - If a task was completed during conversation but not checked off, mark it complete first\n"
    "   - Do NOT list items the user explicitly dropped or said 'done' to in conversation\n"
    "   - Do NOT list items that were never planned for today\n\n"
    "6. Review today's conversations for things the user learned — decisions made, "
    "surprises, things that worked or didn't, patterns or realisations, tradeoffs considered. "
    "For each notable insight, call `nbhd_lesson_suggest` with the lesson text, context, and "
    "source_type='conversation'. Aim for 1-3 high-quality lessons per day if the conversations "
    "warrant it. Do not force lessons from routine small talk.\n"
    "7. Fill in the 'evening-check-in' section of today's daily note "
    "(`nbhd_daily_note_set_section` with section='evening-check-in') using this structure:\n"
    "### What got done today?\n"
    "- Cross-reference morning priorities with tasks document — note completed items.\n"
    "- ✅ Item (brief description)\n\n"
    "### Goal progress\n"
    "- Note any active goals that saw progress today, or flag ones that are stalling\n"
    "- Skip this section if there are no active goals\n\n"
    "### What didn't get done? Why?\n"
    "- ONLY list items that were planned for today AND are still open in the tasks document\n"
    "- Do NOT list items the user dropped, completed, or deferred\n"
    "- ❌ Item — reason (only if you know from context)\n\n"
    "### Plan for tomorrow (top 3)\n"
    "1. Top priority\n"
    "2. Second priority\n"
    "3. Third priority\n\n"
    "### Energy/mood (1-10)\n"
    "- ? (leave as ? — the user fills this in)\n\n"
    "Use the local user date when writing with date arguments to avoid timezone drift.\n"
    "Fill in what you know from the day's conversations. Leave gaps for what you don't know.\n\n"
    "8. Send the user exactly ONE message via `nbhd_send_to_user`. Keep it short and casual:\n"
    "- Brief recap of their day (2-3 lines max)\n"
    "- If any active goals saw progress, mention it (one line)\n"
    "- One prompt: 'Anything to add or adjust before tomorrow?'\n"
    "- If you suggested lessons, mention it briefly\n\n"
    "**IMPORTANT: Send exactly ONE message. Do not send multiple messages.**\n"
)

_WEEK_AHEAD_REVIEW_PROMPT = (
    "It's Monday morning. Run the Week Ahead Review. This is a cron (isolated) session — "
    "you cannot have a back-and-forth conversation. You must do everything in ONE turn.\n\n"
    "Steps:\n"
    "1. Load journal context (`nbhd_journal_context`) and recent memory files\n"
    "2. Check the calendar for the upcoming 7 days (`nbhd_calendar_list_events`)\n"
    "3. Load the user's goals (`nbhd_document_get` with kind='goal', slug='goals')\n"
    "4. Load the user's tasks document (`nbhd_document_get` with kind='tasks', slug='tasks') for open items.\n"
    "5. List all active cron jobs (`cron list`)\n"
    "6. For each cron job, check: does this make sense given the user's week?\n"
    "   - If the user is traveling, skip or redirect location-based crons\n"
    "   - If the user has a packed schedule, consider adjusting timing\n"
    "   - If everything looks fine, note 'no changes needed'\n"
    "7. Review the tasks document for stale items:\n"
    "   - Any task that has been open for more than a week → mention it to the user\n"
    "   - Suggest: 'still relevant, or should we remove it?'\n"
    "   - Keep the stale task list short (top 3 oldest) to avoid overwhelm\n"
    "8. Log decisions in `memory/week-ahead/` with a brief note\n"
    "9. Send the user exactly ONE message via `nbhd_send_to_user`:\n"
    "   - Calendar highlights for the week (2-3 lines)\n"
    "   - Active goals status (1-2 lines)\n"
    "   - Any cron adjustments needed (or 'all good, no changes')\n"
    "   - If nothing conflicts, keep it short: 'All good for this week.'\n\n"
    "**IMPORTANT: Send exactly ONE message. Do not send multiple messages.**\n"
)

_HEARTBEAT_CHECKIN_PROMPT = (
    "You received a scheduled check-in. This is a cron (isolated) session — "
    "you cannot have a back-and-forth conversation. You must do everything in ONE turn.\n\n"
    "**Step 1 — Load today's daily note first.**\n"
    "Call `nbhd_daily_note_get` for today. Read ALL sections — morning-report, "
    "heartbeat-log, evening-check-in, and any other content. This is your ground truth "
    "for what has already been communicated or handled today.\n\n"
    "**Step 2 — Scan for anything that needs attention (in priority order):**\n"
    "1. Memory files — anything you noted to follow up on?\n"
    "2. Calendar — any events in the next 2-3 hours? (`nbhd_calendar_list_events`)\n"
    "3. Recent journal context — anything unfinished? (`nbhd_journal_context`)\n"
    "4. Pending lessons — any waiting for approval? (`nbhd_lessons_pending`)\n\n"
    "**Step 3 — Cross-reference against the daily note.**\n"
    "For each item that seems worth mentioning:\n"
    "- Is it already in the morning-report section? → skip it\n"
    "- Is it already in the heartbeat-log section? → skip it\n"
    "- Was it marked done or addressed anywhere in the note? → skip it\n"
    "- Is it genuinely new information the user hasn't seen today? → keep it\n\n"
    "**Step 4 — Act.**\n"
    "If nothing survives the cross-reference: reply `HEARTBEAT_OK`\n\n"
    "If something genuinely new needs attention:\n"
    "a. Send the user exactly ONE brief message via `nbhd_send_to_user`.\n"
    "b. Then append a one-line summary to the daily note under heading 'Heartbeat Log' "
    "via `nbhd_daily_note_append` (format: `- HH:MM — <what you nudged about>`). "
    "This prevents the next heartbeat from repeating the same nudge.\n\n"
    "**IMPORTANT: Do NOT message unless you have something genuinely NEW to say. "
    "Do NOT send multiple messages. Quality over quantity.**\n"
)

_BACKGROUND_TASKS_PROMPT = (
    "Background maintenance run. This is a cron (isolated) session — "
    "you cannot have a back-and-forth conversation. You must do everything in ONE turn.\n\n"
    "Steps:\n"
    "1. Load recent journal context\n"
    "2. Load the user's tasks document (`nbhd_document_get` with kind='tasks', slug='tasks')\n"
    "3. Review long-term memory and recent daily notes\n"
    "4. Curate long-term memory if there are new patterns, preferences, or insights\n"
    "5. Check recent daily notes and the tasks document for any unaddressed user requests or open tasks\n"
    "6. If you find pending items or unaddressed requests:\n"
    "   a. FIRST load the target document (`nbhd_document_get`) and check for similar existing entries.\n"
    "      Do NOT add a goal, task, or idea that already exists — even if worded slightly differently.\n"
    "      If an existing entry covers the same intent, update it in place instead of adding a duplicate.\n"
    "   b. Then route genuinely NEW items to the right document:\n"
    "   - Action items → tasks document (`nbhd_document_set` with kind='tasks', slug='tasks')\n"
    "   - Goals or aspirations → goals document (`nbhd_document_set` with kind='goal', slug='goals')\n"
    "   - Ideas or brainstorms → ideas document (`nbhd_document_set` with kind='ideas', slug='ideas')\n"
    "   - Lasting patterns or preferences → memory (`nbhd_memory_update`)\n"
    "   The morning briefing reads all of these and will surface them naturally.\n"
    "7. Check the lessons constellation — if there are new approved lessons, the clusters "
    "and positions may need refreshing. The system handles this automatically.\n\n"
    "**Do NOT message the user. Do NOT call nbhd_send_to_user. This is a silent background run.**\n"
    "**Do NOT write to tomorrow's daily note. Update tasks, memory, or ideas documents instead — "
    "the morning briefing will read those and surface anything relevant.**\n"
)

# Model mapping by tier
TIER_MODELS: dict[str, dict[str, str]] = {
    "starter": {"primary": "openrouter/minimax/minimax-m2.5"},
    "premium": {"primary": "anthropic/claude-opus-4.6"},
    "byok": {"primary": "anthropic/claude-opus-4.6"},  # fallback, overridden by user config
}

TIER_MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "starter": {
        "openrouter/minimax/minimax-m2.5": {"alias": "minimax"},
    },
    "premium": {
        "anthropic/claude-sonnet-4.6": {"alias": "sonnet"},
        "anthropic/claude-opus-4.6": {"alias": "opus"},
    },
    "byok": {},  # populated dynamically from user's config
}

WHISPER_DEFAULT_MODEL = {"provider": "openai", "model": "gpt-4o-mini-transcribe"}

# Heartbeat model — always cheap, regardless of tenant tier
HEARTBEAT_MODEL = "openrouter/minimax/minimax-m2.5"


def _heartbeat_cron_expr(start_hour: int, window_hours: int) -> str:
    """Compute cron hour expression for a heartbeat window.

    Handles midnight wrapping (e.g. start=22, window=6 → '0,1,2,3,22,23').
    """
    hours = [(start_hour + i) % 24 for i in range(window_hours)]
    return f"0 {','.join(str(h) for h in sorted(hours))} * * *"


def _build_heartbeat_cron(tenant: Tenant) -> dict | None:
    """Build heartbeat cron job definition for a tenant.

    Returns None if heartbeat is disabled.
    """
    if not tenant.heartbeat_enabled:
        return None

    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
    cron_expr = _heartbeat_cron_expr(
        tenant.heartbeat_start_hour,
        tenant.heartbeat_window_hours,
    )

    return {
        "name": "Heartbeat Check-in",
        "schedule": {"kind": "cron", "expr": cron_expr, "tz": user_tz},
        "sessionTarget": "isolated",
        "model": HEARTBEAT_MODEL,
        "payload": {
            "kind": "agentTurn",
            "message": _HEARTBEAT_CHECKIN_PROMPT,
        },
        "delivery": {"mode": "none"},
        "enabled": True,
    }



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

    jobs = [
        {
            "name": "Morning Briefing",
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": user_tz},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _inject_date_context(
                    _build_morning_briefing_prompt(tenant), tenant
                ),
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
                "message": _inject_date_context(_EVENING_CHECKIN_PROMPT, tenant),
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
                "message": _inject_date_context(_WEEK_AHEAD_REVIEW_PROMPT, tenant),
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
        # NOTE: Nightly Extraction is NOT an OpenClaw cron job — it's a
        # Django endpoint (/api/v1/journal/extract/) triggered via QStash.
        # OpenClaw's cron system only supports "agentTurn" payloads, not
        # webhooks.  See apps/journal/extraction_views.py.
    ]

    # Heartbeat cron — hourly during user's chosen window, cheap model
    heartbeat_job = _build_heartbeat_cron(tenant)
    if heartbeat_job is not None:
        jobs.append(heartbeat_job)

    return jobs



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
                "envelopeTimezone": "user",
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

        # Messaging channels — the central Django router handles actual
        # Telegram/LINE I/O, but we declare the channels here so the agent
        # knows its surface capabilities (inline buttons, etc.).
        # No bot tokens are set — the container never connects directly.
        "channels": {
            "telegram": {
                "enabled": True,
                "capabilities": ["inlineButtons"],
            },
            "line": {
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
    # models routed through OpenRouter (e.g. openrouter/minimax/minimax-m2.5).

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

    # Google Workspace (gws CLI) — enable skills when user has connected Google
    try:
        from apps.integrations.models import Integration

        has_google = Integration.objects.filter(
            tenant=tenant,
            provider="google",
            status=Integration.Status.ACTIVE,
        ).exists()

        if has_google:
            # Set env var pointing to gws credentials on the file share
            config.setdefault("env", {})["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = (
                "/workspace/gws-credentials.json"
            )

            # GWS skills — read-only safe for all tiers
            gws_read_skills = [
                "gws-shared", "gws-gmail-triage", "gws-calendar-agenda",
            ]
            # GWS skills with destructive capabilities — Premium/BYOK only
            gws_write_skills = [
                "gws-gmail", "gws-gmail-send",
                "gws-calendar", "gws-calendar-insert",
                "gws-drive", "gws-tasks",
            ]

            if tier == "starter":
                gws_skill_names = gws_read_skills
            else:
                gws_skill_names = gws_read_skills + gws_write_skills

            skills_section = config.setdefault("skills", {})
            skills_section["load"] = skills_section.get("load", {})
            extra_dirs = skills_section["load"].setdefault("extraDirs", [])
            for skill_name in gws_skill_names:
                skill_path = f"/opt/nbhd/skills/{skill_name}"
                if skill_path not in extra_dirs:
                    extra_dirs.append(skill_path)

            # Action gate skill — loaded for all tiers with GWS access
            # On Starter: returns educational block message
            # On Premium/BYOK: sends confirmation prompt to user
            gate_skill_path = "/opt/nbhd/skills/nbhd-action-gate"
            if gate_skill_path not in extra_dirs:
                extra_dirs.append(gate_skill_path)

            # Set env vars the gate script needs to call Django
            env = config.setdefault("env", {})
            env["NBHD_TENANT_ID"] = str(tenant.id)
            env["NBHD_API_BASE_URL"] = api_base or ""
    except Exception:
        pass  # Don't break config generation if integration check fails

    return config


def config_to_json(config: dict[str, Any]) -> str:
    """Serialize config to JSON string."""
    return json.dumps(config, indent=2)
