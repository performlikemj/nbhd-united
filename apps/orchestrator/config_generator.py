"""Generate OpenClaw config from tenant parameters.

Based on actual OpenClaw config schema — see openclaw.json reference.
"""
from __future__ import annotations

import json
import zoneinfo
from datetime import datetime
from typing import Any

from django.conf import settings

from apps.billing.constants import GEMMA_MODEL, KIMI_MODEL, MINIMAX_MODEL
from apps.orchestrator.tool_policy import generate_tool_config
from apps.tenants.models import Tenant


_CRON_CONTEXT_PREAMBLE = (
    "**MANDATORY — do this BEFORE following the instructions below:**\n"
    "1. Load today's daily note (`nbhd_daily_note_get`). Read EVERY section — "
    "morning-report, heartbeat-log, evening-check-in, and any others. "
    "This tells you what the user has already been told today.\n"
    "2. Load the user's tasks (`nbhd_document_get` kind='tasks', slug='tasks') "
    "and goals (`nbhd_document_get` kind='goal', slug='goals').\n"
    "3. Before writing or sending anything, check each item against the daily note:\n"
    "   - Was it mentioned earlier AND nothing has changed since? → skip it\n"
    "   - Was it mentioned earlier BUT the status has changed (completed, "
    "updated, escalated, new deadline)? → include the UPDATE, not the original info\n"
    "   - Is it brand new — not in any section of today's note? → include it\n"
    "   If nothing new or updated survives this check, that is a VALID outcome — "
    "say so briefly or reply HEARTBEAT_OK (for heartbeats) rather than "
    "padding with stale content.\n\n"
)


# Marker used by `_wrap_message_with_phase2` and `update_system_cron_prompts` to
# detect that a job's message already contains the Phase 2 sync block. The
# wrapper text below MUST contain this exact substring.
PHASE2_SYNC_MARKER = "FINAL STEP — conditional sync to the main session"


def _phase2_sync_block(job_name: str) -> str:
    """Instructions appended to any foreground cron prompt.

    Tells the agent to create a one-shot cron after Phase 1 finishes — but
    ONLY if the run actually sent the user a message. Silent runs (e.g.
    Heartbeat replying HEARTBEAT_OK) skip the sync. The cron uses a
    date-specific expression so it can only match once per year, and the
    systemEvent text includes a self-removal instruction so the main session
    cleans up on receipt.
    """
    return (
        "\n\n---\n"
        f"**{PHASE2_SYNC_MARKER}:**\n"
        "**Guard:** Did you send the user a message via `nbhd_send_to_user` during this "
        "run? If NO (you returned silently, replied HEARTBEAT_OK, or decided nothing was "
        "new), STOP HERE — do not create any sync cron. The main session only needs to "
        "know about user-visible activity.\n\n"
        "If YES, create a one-shot cron that injects a brief summary into the main session "
        "so the assistant knows what just happened:\n\n"
        "1. Compose a 2-3 sentence summary: what sections you wrote, what you sent the "
        "user, anything notable to surface later. This is for the main session's CONTEXT, "
        "not a user message.\n"
        "2. Compute a cron expression that fires ~2 minutes from now using the date/time "
        "from the preamble. Format: `<minute> <hour> <day-of-month> <month> *`. Add 2 to "
        "the current minute; wrap to next hour if it overflows 59. The expression is "
        "date-specific so it can only match once.\n"
        "3. Call `cron add` with:\n"
        f"   - name: `_sync:{job_name}`\n"
        "   - schedule: {kind: cron, expr: <computed>, tz: <user timezone>}\n"
        "   - sessionTarget: main\n"
        "   - wakeMode: now\n"
        "   - payload.kind: systemEvent\n"
        f"   - payload.text: \"[Sync — {job_name}] <your 2-3 sentence summary>. "
        f"After noting this, run: cron remove _sync:{job_name}\"\n"
        "4. If `cron add` fails, accept it — Phase 1 work already completed. Do NOT retry "
        "and do NOT message the user again.\n"
    )


def _build_cron_message(prompt: str, job_name: str, foreground: bool, tenant: "Tenant") -> str:
    """Compose a cron job's message: date preamble + prompt + (optional) Phase 2 sync.

    Centralizes the message-building so seed jobs and tests stay consistent.
    """
    base = _prepare_cron_prompt(prompt, tenant)
    if foreground:
        return base + _phase2_sync_block(job_name)
    return base


def _prepare_cron_prompt(prompt: str, tenant: "Tenant") -> str:
    """Prepend date context and shared preamble to a cron prompt.

    Every cron job gets:
    1. Current date/time (cheap models struggle with date math)
    2. Shared preamble: load daily note + tasks + goals first, cross-reference
       before acting — prevents repeating information across cron sessions.
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
    return date_line + _CRON_CONTEXT_PREAMBLE + prompt

_MORNING_BRIEFING_PROMPT_TEMPLATE = (
    "Good morning! Create today's morning briefing. This runs as a scheduled task. "
    "Execute every step below in order. The journal-writing steps (step 10) are MANDATORY "
    "— you MUST complete them BEFORE sending the user message in step 11. "
    "Do not skip the daily note section writes.\n\n"
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
    "7. Search approved lessons (`nbhd_lesson_search`) for anything relevant to today's "
    "calendar events, open tasks, or active goals. If a past lesson applies to something "
    "planned today, note it for the focus section.\n"
    "8. Check news/topics the user follows (if configured) — use freshness filters (past 24h) "
    "and always verify publication dates before including\n\n"
    "9. VERIFICATION — before listing any carry-over item from yesterday:\n"
    "   - Load the tasks document — is it still marked open (`- [ ]`)?\n"
    "   - Check if the user addressed it in yesterday's evening check-in\n"
    "   - If the user said 'done' or 'drop it' in any conversation, do NOT carry it over\n"
    "   - Only list genuinely open items\n\n"
    "10. REQUIRED: Fill in today's daily note sections by calling "
    "`nbhd_daily_note_set_section` for EACH of the sections below. This MUST happen "
    "before step 11 — do not skip these calls. The Journal app reads these sections, "
    "so if they are empty the user will see nothing in their Journal tomorrow.\n\n"
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
    "### Relevant Lessons\n"
    "- Past lessons from the constellation that apply to today's plans\n"
    "- Skip this section if no lessons are relevant\n\n"
    "11. Send the user exactly ONE message via `nbhd_send_to_user`. Keep it concise:\n"
    "- Weather + what to wear (1 line)\n"
    "- Top priority for the day (1 line)\n"
    "- Anything time-sensitive (1-2 lines)\n"
    "- Full details are in the journal\n\n"
    "When writing daily note sections, include the local target date if supported by your tool call. "
    "Use YYYY-MM-DD in the user's timezone context when passing `date` explicitly (avoid UTC drift).\n\n"
    "Note: These are default sections. The user may customize or remove them — "
    "only fill in sections that exist in their template.\n\n"
    "**IMPORTANT: Send exactly ONE user-facing message via `nbhd_send_to_user`. "
    "After that message is sent, proceed to the FINAL STEP described below.**\n"
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
    # Quantize to ~11km resolution (city-level) to avoid leaking precise location
    lat = getattr(user, "location_lat", None)
    lon = getattr(user, "location_lon", None)
    if lat is not None and lon is not None:
        lat = round(lat, 1)
        lon = round(lon, 1)
        weather_url = build_weather_url_from_coords(lat, lon, user_tz)
    else:
        weather_url = build_weather_url(user_tz)

    return _MORNING_BRIEFING_PROMPT_TEMPLATE.format(weather_url=weather_url)


_EVENING_CHECKIN_PROMPT = (
    "It's evening check-in time. This runs as a scheduled task. Execute every step "
    "below in order. The journal-writing step (step 5) is MANDATORY — you MUST complete "
    "it BEFORE sending the user message in step 6. Do not skip the daily note write.\n\n"
    "Steps:\n"
    "1. Review the daily note, tasks, and goals loaded above. "
    "Note the morning-report 'Top 3 Priorities' and 'Open Tasks'. "
    "Also read any heartbeat-log entries — do NOT repeat what heartbeats already told the user.\n"
    "2. Load today's journal context (`nbhd_journal_context`) to see what the user did today.\n"
    "3. VERIFICATION — before listing any item as 'not done':\n"
    "   - Confirm it appears as `- [ ]` (unchecked) in the tasks document right now\n"
    "   - Confirm it was actually planned for today (check morning priorities)\n"
    "   - If a task was completed during conversation but not checked off, mark it complete first\n"
    "   - Do NOT list items the user explicitly dropped or said 'done' to in conversation\n"
    "   - Do NOT list items that were never planned for today\n\n"
    "4. Review today's conversations for things the user learned — decisions made, "
    "surprises, things that worked or didn't, patterns or realisations, tradeoffs considered. "
    "For each notable insight, call `nbhd_lesson_suggest` with the lesson text, context, and "
    "source_type='conversation'. Aim for 1-3 high-quality lessons per day if the conversations "
    "warrant it. Do not force lessons from routine small talk.\n"
    "5. REQUIRED: Fill in the 'evening-check-in' section of today's daily note by "
    "calling `nbhd_daily_note_set_section` with section='evening-check-in'. This MUST "
    "happen before step 6 — the Journal app reads this section, so if it is empty the "
    "user will see nothing in their Journal tomorrow. Use this structure:\n"
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
    "6. Send the user exactly ONE message via `nbhd_send_to_user`. Keep it short and casual:\n"
    "- Brief recap of their day (2-3 lines max)\n"
    "- If any active goals saw progress, mention it (one line)\n"
    "- One prompt: 'Anything to add or adjust before tomorrow?'\n"
    "- If you suggested lessons, mention it briefly\n\n"
    "**IMPORTANT: Send exactly ONE user-facing message via `nbhd_send_to_user`. "
    "After that message is sent, proceed to the FINAL STEP described below.**\n"
)

_WEEK_AHEAD_REVIEW_PROMPT = (
    "It's Monday morning. Run the Week Ahead Review. This runs as a scheduled task — "
    "execute every step below in order. Complete all the analysis and memory writes "
    "before sending the user message in step 10.\n\n"
    "Steps:\n"
    "1. Load journal context (`nbhd_journal_context`) and recent memory files\n"
    "2. Check the calendar for the upcoming 7 days (`nbhd_calendar_list_events`)\n"
    "3. Review the tasks and goals loaded above. Check which tasks are open vs completed.\n"
    "4. List all active cron jobs (`cron list`)\n"
    "5. For each cron job, check: does this make sense given the user's week?\n"
    "   - If the user is traveling, skip or redirect location-based crons\n"
    "   - If the user has a packed schedule, consider adjusting timing\n"
    "   - If everything looks fine, note 'no changes needed'\n"
    "6. Review the tasks document for stale items:\n"
    "   - Any task that has been open for more than a week → mention it to the user\n"
    "   - Suggest: 'still relevant, or should we remove it?'\n"
    "   - Keep the stale task list short (top 3 oldest) to avoid overwhelm\n"
    "7. Log decisions in `memory/week-ahead/` with a brief note\n"
    "8. **Feature check:** Read `docs/platform-guide.md` and follow its instructions. "
    "If it says to suggest features: check memory for `feature_last_suggested` — "
    "if 7+ days ago (or never), pick ONE feature the user hasn't tried. "
    "Check usage signals from the guide. Include a single casual line in your message. "
    "Update memory: `feature_last_suggested: <today>`, `feature_suggested: <name>`. "
    "If it says not to suggest, or everything is explored, skip this.\n"
    "9. **Weekly review fallback:** Check if a weekly review was saved for last week "
    "(use `skills/nbhd-managed/weekly-review/SKILL.md`). If the user already reflected "
    "on Sunday evening, a review will exist — skip this step. If no review exists, "
    "auto-generate one from the past week's journal entries, mood data, and goal progress. "
    "Use a 'meh' rating as default if there's not enough signal to judge.\n"
    "10. Send the user exactly ONE message via `nbhd_send_to_user`:\n"
    "   - Calendar highlights for the week (2-3 lines)\n"
    "   - Active goals status (1-2 lines)\n"
    "   - Any cron adjustments needed (or 'all good, no changes')\n"
    "   - If nothing conflicts, keep it short: 'All good for this week.'\n\n"
    "**IMPORTANT: Send exactly ONE user-facing message via `nbhd_send_to_user`. "
    "After that message is sent, proceed to the FINAL STEP described below.**\n"
)

_HEARTBEAT_CHECKIN_PROMPT = (
    "You received a scheduled check-in. This is a cron (isolated) session — "
    "you cannot have a back-and-forth conversation. You must do everything in ONE turn.\n\n"
    "**Step 1 — Scan for anything that needs attention (in priority order):**\n"
    "Use the daily note, tasks, and goals loaded above as your ground truth.\n"
    "1. Memory files — anything you noted to follow up on?\n"
    "2. Calendar — any events in the next 2-3 hours? (`nbhd_calendar_list_events`)\n"
    "3. Recent journal context — anything unfinished? (`nbhd_journal_context`)\n"
    "4. Pending lessons — any waiting for approval? (`nbhd_lessons_pending`)\n\n"
    "**Step 2 — Cross-reference against the daily note.**\n"
    "For each item that seems worth mentioning:\n"
    "- Is it already in the morning-report section? → skip it\n"
    "- Is it already in the heartbeat-log section? → skip it\n"
    "- Was it marked done or addressed anywhere in the note? → skip it\n"
    "- Is it genuinely new information the user hasn't seen today? → keep it\n\n"
    "**Step 3 — Act.**\n"
    "If nothing survives the cross-reference: reply `HEARTBEAT_OK` and STOP. "
    "Do NOT proceed to the FINAL STEP — silent runs skip the sync.\n\n"
    "If something genuinely new needs attention:\n"
    "a. Send the user exactly ONE brief message via `nbhd_send_to_user`.\n"
    "b. Then append a one-line summary to the daily note under heading 'Heartbeat Log' "
    "via `nbhd_daily_note_append` (format: `- HH:MM — <what you nudged about>`). "
    "This prevents the next heartbeat from repeating the same nudge.\n"
    "c. Then proceed to the FINAL STEP described below.\n\n"
    "**IMPORTANT: Do NOT message unless you have something genuinely NEW to say. "
    "Do NOT send multiple messages. Quality over quantity.**\n"
)

_WEEKLY_REFLECTION_PROMPT = (
    "It's Sunday evening — time for the weekly reflection. This runs in the main session "
    "so the user can reply and shape the reflection with you.\n\n"
    "Steps:\n"
    "1. Load journal context for the past 7 days (`nbhd_journal_context`)\n"
    "2. Load goals (`nbhd_document_get` with kind='goal', slug='goals') and tasks\n"
    "3. Review what happened this week: conversations, journal entries, mood, goal progress\n"
    "4. Send the user exactly ONE message via `nbhd_send_to_user` that:\n"
    "   - Opens with a warm, brief recap of what you noticed this week (2-3 lines)\n"
    "   - Asks: how would they rate the week — thumbs up, meh, or thumbs down?\n"
    "   - Asks: what was the biggest win?\n"
    "   - Keep it conversational and short — don't list everything, just the highlights\n\n"
    "The user may respond with their thoughts. When they do, use "
    "`skills/nbhd-managed/weekly-review/SKILL.md` to save the weekly review, "
    "combining their input with what you already know from journal data.\n\n"
    "If the user doesn't respond, that's OK — the Monday morning review will "
    "auto-generate a summary from available data.\n\n"
    "**Send exactly ONE user-facing message via `nbhd_send_to_user`. Keep it casual "
    "and inviting, not a data dump. After that message is sent, proceed to the "
    "FINAL STEP described below.**\n"
)

_PROJECT_CHECKIN_PROMPT = (
    "Project check-in. This is a cron (isolated) session — but you CAN have a "
    "back-and-forth with the user via `nbhd_send_to_user`.\n\n"
    "Read `rules/voice-journal.md` for the full journal routing protocol.\n\n"
    "Steps:\n"
    "1. Load today's daily note (`nbhd_daily_note_get`) — check what's already been logged today\n"
    "2. Load ALL project documents (`nbhd_document_get` with kind='project') to see what's being tracked\n"
    "3. Load the tasks document (`nbhd_document_get` with kind='tasks', slug='tasks')\n"
    "4. Load the goals document (`nbhd_document_get` with kind='goal', slug='goals')\n"
    "5. Compare: which tracked projects have updates today vs which have nothing logged\n"
    "6. If the daily note already has comprehensive updates for all tracked projects "
    "(e.g. from a voice journal earlier), skip the check-in entirely — do NOT message the user.\n"
    "7. For projects with no update today, message the user casually via `nbhd_send_to_user`:\n"
    "   - \"Hey, haven't heard about [project] today — anything happening or taking a break from it?\"\n"
    "   - Group questions naturally, don't send one message per project\n"
    "8. If the user responds with updates, route them to the right journal locations:\n"
    "   - Project-specific updates → the project's document (`nbhd_document_set` kind='project')\n"
    "   - Tasks → tasks document\n"
    "   - General notes → daily note via `nbhd_daily_note_append`\n"
    "9. Keep the tone casual and supportive — this is a friend checking in, not a standup meeting\n"
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
    "starter": {"primary": MINIMAX_MODEL},
}

TIER_MODEL_CONFIGS: dict[str, dict[str, Any]] = {
    "starter": {
        MINIMAX_MODEL: {"alias": "minimax"},
        KIMI_MODEL: {"alias": "kimi"},
        GEMMA_MODEL: {"alias": "gemma"},
    },
}

WHISPER_DEFAULT_MODEL = {"provider": "openai", "model": "gpt-4o-mini-transcribe"}

# Heartbeat model — always cheap, regardless of tenant tier
HEARTBEAT_MODEL = MINIMAX_MODEL


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
            # Heartbeat is foreground=true: the Phase 2 sync block contains a
            # guard that skips the sync on HEARTBEAT_OK runs, so silent hours
            # stay silent for both user and main session.
            "message": _build_cron_message(
                _HEARTBEAT_CHECKIN_PROMPT, "Heartbeat Check-in", foreground=True, tenant=tenant,
            ),
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

    # All scheduled tasks run isolated (sessionTarget=isolated, payload.kind=agentTurn).
    # The "foreground" flag controls whether the Phase 2 sync block is appended:
    #   foreground=True  → conditional sync to main session if the run sent a user message
    #   foreground=False → never sync (used only for guaranteed-silent jobs)
    jobs = [
        {
            "name": "Morning Briefing",
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": user_tz},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _build_cron_message(
                    _build_morning_briefing_prompt(tenant),
                    "Morning Briefing", foreground=True, tenant=tenant,
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
                "message": _build_cron_message(
                    _EVENING_CHECKIN_PROMPT,
                    "Evening Check-in", foreground=True, tenant=tenant,
                ),
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        },
        {
            "name": "Weekly Reflection",
            "schedule": {"kind": "cron", "expr": "0 20 * * 0", "tz": user_tz},
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _build_cron_message(
                    _WEEKLY_REFLECTION_PROMPT,
                    "Weekly Reflection", foreground=True, tenant=tenant,
                ),
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
                "message": _build_cron_message(
                    _WEEK_AHEAD_REVIEW_PROMPT,
                    "Week Ahead Review", foreground=True, tenant=tenant,
                ),
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        },
        {
            "name": "Project Check-in",
            "schedule": {
                "kind": "cron",
                "expr": "0 {} * * 1-5".format(
                    (tenant.heartbeat_start_hour + tenant.heartbeat_window_hours // 2) % 24
                ),
                "tz": user_tz,
            },
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _build_cron_message(
                    _PROJECT_CHECKIN_PROMPT,
                    "Project Check-in", foreground=True, tenant=tenant,
                ),
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
                # foreground=False: prompt explicitly says "do NOT message the user",
                # so the sync wrapper would be dead text. Skip it to save tokens.
                "message": _build_cron_message(
                    _BACKGROUND_TASKS_PROMPT,
                    "Background Tasks", foreground=False, tenant=tenant,
                ),
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        },
        # NOTE: Nightly Extraction is NOT an OpenClaw cron job — it's a
        # Django endpoint (/api/v1/journal/extract/) triggered via QStash.
        # See apps/journal/extraction_views.py.
    ]

    # Heartbeat cron — hourly during user's chosen window, cheap model
    heartbeat_job = _build_heartbeat_cron(tenant)
    if heartbeat_job is not None:
        jobs.append(heartbeat_job)

    # Apply per-task model overrides from tenant preferences
    _TASK_SLUG_MAP = {
        "Morning Briefing": "morning_briefing",
        "Evening Check-in": "evening_checkin",
        "Weekly Reflection": "weekly_reflection",
        "Week Ahead Review": "week_review",
        "Background Tasks": "background_tasks",
        "Project Check-in": "project_checkin",
        "Heartbeat Check-in": "heartbeat",
    }
    prefs = getattr(tenant, "task_model_preferences", None) or {}
    if prefs:
        for job in jobs:
            slug = _TASK_SLUG_MAP.get(job["name"], "")
            model = prefs.get(slug)
            if model:
                job["model"] = model

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

    # Allow user to override primary model within their tier
    if tenant.preferred_model and tenant.preferred_model in model_entries:
        models_config = {**models_config, "primary": tenant.preferred_model}

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

    # Finance plugin — conditionally loaded when tenant has finance enabled
    if getattr(tenant, "finance_enabled", False):
        _plugin_defs.append((
            str(getattr(settings, "OPENCLAW_FINANCE_PLUGIN_ID", "nbhd-finance-tools") or "").strip(),
            str(getattr(settings, "OPENCLAW_FINANCE_PLUGIN_PATH", "/opt/nbhd/plugins/nbhd-finance-tools") or "").strip(),
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
                "llm": {
                    # OpenClaw 2026.4.5 introduced a 60s default idle-token
                    # watchdog that aborts the LLM stream if no token arrives
                    # within 60s.  Slower models (minimax-m2.7) on heavy
                    # prompts routinely exceed that, causing cron runs to
                    # timeout and cascading into container crashes via the
                    # chmod EPERM in the task-registry sweep.  Set to 5 min
                    # so slow cold-starts and heavy tool-calling prompts
                    # have room.
                    "idleTimeoutSeconds": 300,
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
            # Override DEFAULT_GATEWAY_HTTP_TOOL_DENY — cron and gateway
            # are blocked by default on the HTTP endpoint. Allow them so
            # Django can manage cron jobs and trigger config hot-reloads.
            "tools": {
                "allow": ["cron", "gateway"],
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
    # models routed through OpenRouter (e.g. MINIMAX_MODEL).

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

            # GWS skills — read-only for now
            gws_skill_names = [
                "gws-shared", "gws-gmail-triage", "gws-calendar-agenda",
            ]

            skills_section = config.setdefault("skills", {})
            skills_section["load"] = skills_section.get("load", {})
            extra_dirs = skills_section["load"].setdefault("extraDirs", [])
            for skill_name in gws_skill_names:
                skill_path = f"/opt/nbhd/skills/{skill_name}"
                if skill_path not in extra_dirs:
                    extra_dirs.append(skill_path)

            # Action gate skill — loaded for all users with GWS access
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
