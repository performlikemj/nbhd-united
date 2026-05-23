"""Generate OpenClaw config from tenant parameters.

Based on actual OpenClaw config schema — see openclaw.json reference.
"""

from __future__ import annotations

import json
import zoneinfo
from datetime import datetime
from typing import Any

from django.conf import settings

from apps.billing.constants import (
    ANTHROPIC_SONNET_MODEL,
    DEEPSEEK_MODEL,
    GEMMA_MODEL,
    MINIMAX_MODEL,
)
from apps.orchestrator.tool_policy import OPENCLAW_CURRENT_VERSION, generate_tool_config
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
    "padding with stale content.\n"
    "4. Glance at the **Agenda — Open threads with this user** section in "
    "USER.md. If any open thread (an untouched feature introduction, a "
    "planned workout coming up, a financial plan in motion) naturally "
    "fits the moment — based on tone of the daily note, what the user "
    "just signaled, the prescribed task — weave it in lightly. If "
    "nothing fits, stay focused. Surface at most one or two threads, "
    "chosen for fit, never coverage. Never enumerate the agenda to "
    "the user.\n"
    "5. **GROUNDING CONTRACT** — every quantitative claim you make to the user "
    "(counts, amounts, dates, streaks, totals, durations, percentages, "
    '"X days ago") MUST come from a tool-call result returned in THIS turn. '
    "Do not infer from USER.md. Do not recall from prior context. Do not "
    "average or extrapolate. If a query returns zero rows, say so plainly — "
    "don't pad with stale figures. Use the per-domain query tool for any "
    "number you state: `nbhd_gravity_query` for finance (debt, payments, "
    "payoff). USER.md is identity (goals, persona, voice) — not current state.\n\n"
)


# Marker used by `_wrap_message_with_phase2` and `update_system_cron_prompts` to
# detect that a job's message already contains the Phase 2 sync block. The
# wrapper text below MUST contain this exact substring.
PHASE2_SYNC_MARKER = "FINAL STEP — conditional sync to the main session"


def _phase2_sync_block(job_name: str) -> str:
    """Instructions appended to any foreground cron prompt.

    Tells the agent to call ``nbhd_cron_phase2_summary`` after Phase 1
    finishes — but ONLY if the run actually sent the user a message via
    ``nbhd_send_to_user``. Silent runs (Heartbeat HEARTBEAT_OK, no-op
    decisions) skip the call entirely; absence of the tool invocation IS
    the verdict that nothing user-visible happened.

    Django owns the rest: the tool's runtime handler computes the cron
    expression, composes the systemEvent payload, sets sessionTarget=main,
    and registers the one-shot ``_sync:<job_name>`` cron with the OpenClaw
    gateway. The agent only contributes the summary text.

    See ``apps/integrations/runtime_views.py::RuntimeCronPhase2SummaryView``
    for the server-side contract.
    """
    return (
        "\n\n---\n"
        f"**{PHASE2_SYNC_MARKER}:**\n"
        "**Guard:** Did you send the user a message via `nbhd_send_to_user` during this "
        "run? If NO (you returned silently, replied HEARTBEAT_OK, or decided nothing was "
        "new), STOP HERE — do not call any sync tool. The main session only needs to "
        "know about user-visible activity.\n\n"
        "If YES, invoke the `nbhd_cron_phase2_summary` tool with:\n"
        f'   - `job_name`: `"{job_name}"`\n'
        "   - `summary`: a 2-3 sentence recap — what sections you wrote, what you sent "
        "the user, anything notable to surface later. This is for the main session's "
        "CONTEXT, not a user message.\n\n"
        "**This is a TOOL invocation, not a chat message.** Do NOT typeset, paraphrase, "
        "or emit the parameters as text. Do NOT pass them through `nbhd_send_to_user`. "
        "Just invoke `nbhd_cron_phase2_summary` directly. Backend handles cron "
        "expression math, payload composition, and the underscore-prefixed sync name — "
        "you only provide the summary.\n\n"
        "If the tool call fails or the tool is unavailable, accept it — Phase 1 work "
        "already completed. Do NOT retry, do NOT message the user, and do NOT send the "
        "parameters above as a chat message.\n"
    )


def _build_cron_message(
    prompt: str,
    job_name: str,
    foreground: bool,
    tenant: Tenant,
) -> str:
    """Compose a cron job's message: date preamble + shared preamble + prompt + (optional) Phase 2 sync.

    Centralizes the message-building so seed jobs and tests stay consistent.

    Pre-loaded user state (goals, tasks, lessons, profile) is no longer baked
    into the cron message itself — it lives in ``workspace/USER.md`` and is
    auto-loaded by OpenClaw on every agent turn. See
    ``apps.orchestrator.workspace_envelope`` for the merge logic and refresh
    triggers.

    The trailing ``.strip()`` mirrors OpenClaw's ``coercePayload`` (see
    ``openclaw-tools-*.js`` ``normalizeOptionalString`` → ``value?.trim()``):
    OC strips leading/trailing whitespace on store, so any newline tail
    here would create a single-byte mismatch on the next ``cron.list`` and
    cause ``update_system_cron_prompts`` to recreate every system cron on
    every wake. See ``project_openclaw_cron_payload_shape.md`` for the
    full saga and the audit step to run on the next OpenClaw bump.
    """
    base = _prepare_cron_prompt(prompt, tenant)
    full = base + _phase2_sync_block(job_name) if foreground else base
    return full.strip()


# When ``experimental_typed_journal_lifecycle`` is True on the tenant, these
# substitutions rewrite the shared cron prompts to direct the agent at the
# typed Goal/Task lifecycle tools instead of free-form ``Document(kind=goal|
# tasks)`` markdown writes. Keyed off literal substrings present in the
# prompts so we keep one source of truth per prompt rather than duplicating
# every cron-prompt block into legacy + typed variants. Verified covered by
# ``TypedLifecycleSwapsTest``.
_TYPED_LIFECYCLE_SWAPS: tuple[tuple[str, str], ...] = (
    # ── Write-side: tasks ───────────────────────────────────────────────
    (
        "`nbhd_document_append` (kind='tasks', slug='tasks')",
        "`nbhd_task_create` (typed lifecycle — captures status + due_date as a queryable row; "
        "use `nbhd_task_complete` to mark done later)",
    ),
    (
        "`nbhd_document_set` with kind='tasks', slug='tasks'",
        "`nbhd_task_create` for new actionable items (or `nbhd_task_update`/`nbhd_task_complete` "
        "for existing tasks). Do not write goal/task content into Document anymore",
    ),
    # ── Write-side: goals ───────────────────────────────────────────────
    (
        "`nbhd_document_append` (kind='goal', slug='goals')",
        "`nbhd_goal_create` for new goals or `nbhd_goal_update` to update an existing goal "
        "(use `nbhd_goal_achieve` / `nbhd_goal_abandon` for lifecycle changes)",
    ),
    (
        "`nbhd_document_set` with kind='goal', slug='goals'",
        "`nbhd_goal_create` for new goals (or `nbhd_goal_update`/`nbhd_goal_achieve`/"
        "`nbhd_goal_abandon` for existing). Do not write goal content into Document anymore",
    ),
    # ── Read-side: tasks ────────────────────────────────────────────────
    (
        "`nbhd_document_get` with kind='tasks', slug='tasks'",
        "`nbhd_task_list({status: 'open'})` (typed — preferred). Legacy task markdown "
        "in `nbhd_document_get(kind='tasks')` may still hold historical content during transition",
    ),
    # ── Read-side: goals ────────────────────────────────────────────────
    (
        "`nbhd_document_get` with kind='goal', slug='goals'",
        "`nbhd_goal_list({status: 'active'})` (typed — preferred). Legacy goal markdown "
        "in `nbhd_document_get(kind='goal')` may still hold historical content during transition",
    ),
    (
        "`nbhd_document_get` kind='goal'",
        "`nbhd_goal_list` (typed — preferred). Legacy `nbhd_document_get(kind='goal')` for transition",
    ),
    (
        "`nbhd_document_get` kind='tasks'",
        "`nbhd_task_list` (typed — preferred). Legacy `nbhd_document_get(kind='tasks')` for transition",
    ),
)


def _apply_typed_lifecycle_swaps(prompt: str, tenant: Tenant) -> str:
    """Rewrite cron prompts to direct the typed-lifecycle agent at typed tools.

    No-op when the tenant flag is off — keeps the fleet behavior unchanged
    while the canary observes the typed lifecycle. Order matters: longer
    patterns first so we don't partial-match on shorter ones.
    """
    if not getattr(tenant, "experimental_typed_journal_lifecycle", False):
        return prompt
    out = prompt
    for old, new in _TYPED_LIFECYCLE_SWAPS:
        out = out.replace(old, new)
    return out


def _prepare_cron_prompt(prompt: str, tenant: Tenant) -> str:
    """Prepend date context and shared preamble to a cron prompt.

    The embedded ``Current date and time:`` line is a **snapshot** taken
    when this cron payload was last reconciled into OpenClaw — it can be
    hours or days old by fire time. The live authoritative value is in
    ``workspace/USER.md`` (``_Current local time: ..._``), which OpenClaw
    re-reads on every agent turn. The snapshot here is kept only so the
    "do future-date math" math instruction has a concrete anchor and so
    cheap models that never load USER.md still have some time signal.

    The reconciler in ``cron_drift.strip_date_line`` strips this preamble
    before diffing, so daily drift of the snapshot doesn't churn the
    cron-prompt store; staleness is bounded instead by the periodic
    ``refresh_user_md_fleet`` push that re-renders USER.md fleet-wide.

    The string prefix ``Current date and time:`` is load-bearing — do not
    change it without also updating ``cron_drift.strip_date_line`` (and
    accepting a one-time fleet-wide cron recreate during the cutover).
    """
    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
    try:
        tz = zoneinfo.ZoneInfo(user_tz)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    now = datetime.now(tz)
    date_line = (
        f"Current date and time: {now.strftime('%A, %B %d, %Y at %H:%M')} ({user_tz}) "
        f"— SNAPSHOT taken when this cron payload was last reconciled, may be stale.\n"
        f"For live time, read `_Current local time: ..._` from USER.md "
        f"(loaded fresh on every agent turn). Prefer USER.md over the snapshot above "
        f"whenever you reason about 'today', 'this morning', 'earlier today', or "
        f"whether the user has already done something today. Before claiming the user "
        f"has or hasn't done X today, verify against today's daily note and journal "
        f"entries — don't infer activity from the snapshot date.\n"
        f"When mentioning future events, compute exact days from USER.md's live date "
        f"(fall back to {now.strftime('%Y-%m-%d')} only if USER.md isn't loaded): "
        f"event_date minus today = X days from now. "
        f"Never say 'tomorrow' unless the math confirms exactly 1 day away.\n\n"
    )
    return _apply_typed_lifecycle_swaps(date_line + _CRON_CONTEXT_PREAMBLE + prompt, tenant)


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
    "   Read `hourly.time`, `hourly.precipitation_probability`, `hourly.precipitation`, "
    "`hourly.temperature_2m`, `hourly.weather_code`, and `hourly.wind_speed_10m`.\n"
    "   IMPORTANT — distinguish past from future using the current time (from the date/time "
    "line above). Only flag thresholds for hours AHEAD of now. If precipitation or storms "
    "occurred in hours before now, describe them as past — 'rained earlier this morning', "
    "'cleared up overnight' — never say 'rainy day' for rain that already ended.\n"
    "   Mention intraday timing ONLY when one of these thresholds fires for upcoming hours:\n"
    "   - precipitation_probability ≥ 40% for 2+ consecutive future hours → flag the window, "
    "peak %, and local hour (e.g. 'rain ~13:00-16:00, peak 70% at 14:00')\n"
    "   - any future hour with weather_code ≥ 95 (thunderstorm) → always mention the hour\n"
    "   - temperature_2m swing ≥ 10°F (≈ 5.5°C) across remaining hours today → flag it\n"
    "   - wind_speed_10m crossing 20 mph (≈ 32 km/h) in a future hour when earlier hours "
    "were calm → flag it\n"
    "   If none fire, the day is stable — write a single summary line and omit the Intraday "
    "block. Do NOT enumerate every hour. 'Sunny all day' does not become "
    "'9am sun, 10am sun, 11am sun'.\n"
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
    "**Today:** temp range, conditions, what to wear\n"
    "**Intraday:** (include ONLY if a threshold from step 1 fires) one or two bullet lines "
    "flagging the window — e.g. `- Rain window ~13:00–16:00 (70% at 14:00, tapering after)`, "
    "`- Temp drops from 68°F at noon to 48°F by 18:00 — jacket if out late`. Omit this line "
    "entirely on stable days.\n"
    "**Tomorrow:** brief forecast; flag any thunderstorm or heavy-rain window by hour\n\n"
    "Examples of good output (follow the same shape):\n"
    "  Stable day (journal):\n"
    "    **Today:** 68–74°F, partly cloudy. Light layers.\n"
    "    **Tomorrow:** Similar, slightly warmer.\n"
    "  Stable day (user message): `Partly cloudy, 68–74°F — light layers.`\n"
    "  Rain window (journal):\n"
    "    **Today:** 62–70°F, rain developing midday.\n"
    "    **Intraday:** - Rain ~13:00–16:00 (peak 70% at 14:00). Dry after 17:00.\n"
    "    **Tomorrow:** Clearing, 64–72°F.\n"
    "  Rain window (user message): `Rain in Osaka ~1pm, clearing by 4pm — grab an umbrella.`\n"
    "  Variable day (journal):\n"
    "    **Today:** 55–78°F, thunderstorm risk late afternoon.\n"
    "    **Intraday:** - Warm through noon, front arrives ~16:00 with thunder (code 95). "
    "Temp drops ~15°F by evening.\n"
    "    **Tomorrow:** Cooler, 52–61°F, showers easing.\n"
    "  Variable day (user message): `Storms around 4pm, then 15°F drop — jacket for anything after dinner.`\n\n"
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
    "- Weather + what to wear (1 line). If an intraday threshold fired, add one short clause "
    "naming the window — e.g. 'Rain in Osaka ~1pm, clearing by 4pm — umbrella'. "
    "Otherwise keep it one line.\n"
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
    "5b. PROACTIVE energy-mood capture (replaces the old 'leave as ?' passive rule). "
    "The goal is to actually GET this data, not just record what surfaced.\n"
    "    **Today's slot.** Read the existing `energy-mood` section from today's daily "
    "note (loaded in the preamble). If the user already shared a value (any non-`?`, "
    "non-empty entry), use it as-is and call `nbhd_daily_note_set_section` "
    "section='energy-mood' to confirm. Skip the rest of 5b.\n"
    "    If the value is `?`, empty, or missing, decide whether to ask in the step-6 "
    "message. Ask ONLY when the user has been reachable today — there must be at least "
    "one inbound chat message or user-authored daily-note entry visible in the "
    "preamble or `nbhd_journal_context` from today. If the user has been silent today "
    "(no inbound traffic, voice journals, or replies), do NOT ask; call "
    "`nbhd_daily_note_set_section` with value='?' and skip to 5c.\n"
    "    If you decide to ask, do NOT write the section yet — leave it as `?`. The user's "
    "answer (if it comes) will be captured next time we sync. The energy ask itself "
    "is composed inline in step 6 below — do not send a separate message.\n"
    "5c. YESTERDAY follow-up — at most one retry, never further back. From "
    "`nbhd_journal_context` (or by re-reading the daily-note via "
    "`nbhd_daily_note_get` with yesterday's date), inspect yesterday's "
    "`energy-mood` section. If yesterday's value is `?` or empty AND you decided "
    "in 5b to ask the user about today, you may append a soft secondary clause to "
    "the step-6 message: 'and yesterday was a blank — any rough sense in hindsight?'. "
    "If yesterday is filled, or you decided NOT to ask in 5b, do not chase it. Never "
    "look further back than one day.\n\n"
    "Use the local user date when writing with date arguments to avoid timezone drift.\n"
    "Fill in what you know from the day's conversations. Leave gaps for what you don't know.\n\n"
    "6. Send the user exactly ONE message via `nbhd_send_to_user`. Keep it short and casual:\n"
    "- Brief recap of their day (2-3 lines max)\n"
    "- If any active goals saw progress, mention it (one line)\n"
    "- If you suggested lessons, mention it briefly\n"
    "- If 5b decided to ask: append ONE short clause asking about today's energy, "
    "  e.g. 'Quick one — where did energy land today, 1-10?'. One sentence, no emoji.\n"
    "- If 5c is also appending: include the soft yesterday clause too "
    "  ('and yesterday was a blank — any rough sense in hindsight?').\n"
    "- If neither 5b nor 5c is asking: close with 'Anything to add or adjust before tomorrow?'\n\n"
    "**IMPORTANT: Send exactly ONE user-facing message via `nbhd_send_to_user`. "
    "After that message is sent, proceed to the FINAL STEP described below.**\n"
)

_PERSONAL_QUESTION_PROMPT = (
    "Personal-question cron. Pick ONE thoughtful, contextual question that "
    "deepens the user's long-term memory document — the kind of question that "
    "catches the stuff they'd never think to volunteer. This runs as a "
    "scheduled task, single turn — execute every step in order.\n\n"
    "Steps:\n"
    "1. Load context: call `nbhd_journal_context` (returns last 7 days of "
    "daily notes plus the long-term memory document). The memory doc has "
    "sections `## Preferences`, `## Goals`, `## Decisions`, `## Lessons "
    "Learned`, and `## People & Context`.\n"
    "2. YESTERDAY-QUESTION CHECK (do this first). Look in yesterday's daily "
    "note for a marker line written by step 6: `Personal-question asked: "
    "<question>`. If you find one, decide:\n"
    "   - Did the user reply on that topic at any point yesterday or today? "
    "Search recent notes and memory for any related update. If YES, the "
    "question landed — pick a NEW gap in step 3.\n"
    "   - If NO reply AND the user wasn't clearly silent yesterday (look for "
    "evening-check-in entries, voice journals, normal chat traffic) AND "
    "fewer than 2 unanswered personal questions sit in the rolling 7-day "
    "window, re-ask the SAME question with slightly softer phrasing. Skip "
    "to step 4.\n"
    "   - Otherwise (user was silent, question has decayed in relevance, or "
    "2 unanswered questions are already pending) drop it quietly and pick "
    "a new gap in step 3.\n"
    "3. FIND A GAP. Scan memory + recent notes for ONE of these in priority "
    "order:\n"
    "   a. A NAMED person, place, or thing the user mentioned recently (in "
    "`## People & Context` or in the last 7 days of notes) with no "
    "follow-up detail. Example: memory says 'daughter into stained glass' "
    "but no context — ask 'how'd she get into that?'.\n"
    "   b. An active goal in `## Goals` with no recorded WHY.\n"
    "   c. A decision in `## Decisions` with no recorded tradeoff.\n"
    "   d. A lesson in `## Lessons Learned` with no recorded origin story.\n"
    "   e. A preference in `## Preferences` whose ROOT isn't captured.\n"
    "   AVOID: generic favorites ('what's your favorite color'), anything "
    "already answered in memory, anything you've asked in the last 14 days. "
    "The question must be specific to THIS user, drawn from THEIR existing "
    "data.\n"
    "4. Compose ONE short question. Constraints: a single sentence, no "
    "emoji, mobile-friendly, conversational, references the specific thing. "
    "Good: 'You mentioned your daughter is into stained glass — how'd she "
    "get into that?'. Bad: 'What hobbies does your family have?'.\n"
    "5. REACHABILITY GATE. If the last 7 days of notes show NO user-"
    "authored content (no voice journals, no chat replies, no evening-"
    "check-in entries written by the user), the user is silent — STOP. "
    "Reply with the literal token `PERSONAL_QUESTION_SKIPPED` and do NOT "
    "send. The Phase 2 sync guard will skip the sync block too.\n"
    "6. Send the question via `nbhd_send_to_user` (exactly one message). "
    "Then call `nbhd_daily_note_append` for today with the line "
    "`Personal-question asked: <verbatim question>` so tomorrow's run can "
    "detect it. Use today's date (the local user date from the preamble).\n"
    "7. WHEN THE ANSWER ARRIVES (in a later main-session reply, not in this "
    "cron turn): the main session is responsible for routing it. The "
    "convention is to call `nbhd_memory_update`, slot the new info under "
    "the most-fitting section header — usually `## People & Context` for "
    "relationship details, `## Preferences` for habits, `## Goals` for "
    "ambitions, `## Decisions` for choices, `## Lessons Learned` for "
    "hard-won insights — and write back the full updated markdown. Keep "
    "edits tight: one or two lines, not a paragraph. This step is "
    "documentation for the main session, NOT something this cron turn "
    "performs.\n\n"
    "**IMPORTANT: Send at most ONE user-facing message via "
    "`nbhd_send_to_user`. If step 5 short-circuits, send NO message and "
    "reply `PERSONAL_QUESTION_SKIPPED`. After your message is sent (or "
    "skipped), proceed to the FINAL STEP described below.**\n"
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
    "**Step 0 — File any new external work sessions (YardTalk, etc.).**\n"
    "Call `nbhd_sessions_pending`. If `count` is 0, skip the rest of this step.\n"
    "For each session returned, distill its content into the existing primitives BEFORE moving to Step 1:\n"
    "- Append accomplishments + progress to the daily note for the session's date via `nbhd_daily_note_append`.\n"
    "- Add each concrete `next_steps` item as a task via `nbhd_document_append` (kind='tasks', slug='tasks').\n"
    "- If the session references a goal, update it via `nbhd_document_append` (kind='goal', slug='goals').\n"
    "- Pull anything worth carrying forward into long-term memory via `nbhd_memory_update`.\n"
    "After writing, call `nbhd_session_mark_processed` with a short processed_summary recording what you wrote. "
    "If a session is a stub (<30s), a duplicate of something already filed, or has no actionable content, "
    "call `nbhd_session_mark_processed` with `{skipped: true, skip_reason: '<reason>'}` instead.\n"
    "Distillation is SILENT — do NOT send the user a message about it. Continue to Step 1.\n\n"
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
    '   - "Hey, haven\'t heard about [project] today — anything happening or taking a break from it?"\n'
    "   - Group questions naturally, don't send one message per project\n"
    "8. If the user responds with updates, route them to the right journal locations:\n"
    "   - Project-specific updates → the project's document (`nbhd_document_set` kind='project')\n"
    "   - Tasks → tasks document\n"
    "   - General notes → daily note via `nbhd_daily_note_append`\n"
    "9. Keep the tone casual and supportive — this is a friend checking in, not a standup meeting\n"
)

_FUEL_WORKOUT_PREP_PROMPT = (
    "Fuel background workout prep. This is a silent cron — "
    "do NOT message the user. Do NOT call nbhd_send_to_user.\n\n"
    "Your job is to write today's workout context into the daily note so that "
    "every other session (morning briefing, user conversations, evening check-in) "
    "can see it without needing special Fuel instructions. **All future Fuel "
    "sessions today MUST read what you write — there is one locked plan per day, "
    "and you are creating it.**\n\n"
    "Steps:\n"
    "1. Call `nbhd_fuel_audit` (preferred). If unavailable, fall back to "
    "`nbhd_fuel_summary`. The audit returns today_plan, next_14d_workouts, "
    "fuel_crons, and conflicts.\n"
    "2. **Conflict gate.** If `conflicts.duplicate_fires` is non-empty, STOP. "
    "Write a brief `fuel` section noting the duplicates and that the user should "
    "review them. Do NOT add new crons. Do NOT proceed to step 4.\n"
    "3. **Idempotence gate.** If `today_plan.exists` is true (a previous run "
    "today already wrote the section), DO NOT rewrite it with a different plan. "
    "You may refresh sleep/yesterday lines if they are stale, but the **Today's "
    "workout** line must match what's already locked. Re-read `today_plan.raw_section` "
    "and treat it as authoritative.\n"
    "4. Check yesterday's planned workouts — if any were not logged as done, "
    "note them as missed.\n"
    "5. Write the `fuel` section to today's daily note "
    "(`nbhd_daily_note_set_section` with `section_slug` = `fuel`). **If the tool "
    "call returns an error or times out, treat the write as FAILED — do NOT "
    "claim the section was written, and do NOT call nbhd_send_to_user with a "
    "success message.**\n\n"
    "The fuel section should be brief (4-6 lines) and contain:\n"
    "- **Today's workout** — activity, category, estimated duration. "
    'If no workout is planned today, write "Rest day."\n'
    "- **Plan progress** — plan name, sessions completed vs total.\n"
    "- **Last night's sleep** — duration and quality if available. "
    "If sleep was short (<6h) or poor (quality ≤2), add a recovery note "
    '(e.g. "consider lighter session or mobility work").\n'
    "- **Yesterday** — completed, missed, or rest.\n\n"
    "Example:\n"
    "```\n"
    "**Today:** Push Day — Chest & Shoulders (strength, ~60 min)\n"
    "**Plan:** 4-Week Strength Builder — 8/12 sessions done\n"
    "**Sleep:** 7.5h, quality 4/5 — recovery looks good\n"
    "**Yesterday:** Pull Day ✓ completed\n"
    "```\n\n"
    "If there is no active plan AND no today_plan, skip silently — do not write "
    "the fuel section.\n\n"
    "**Do NOT message the user. Do NOT call nbhd_send_to_user. "
    "Do NOT write to tomorrow's daily note. This is a silent background run.**\n"
)


# NOTE: foreground (user-conversation) Fuel rule lives in the plugin tool
# description for `nbhd_fuel_audit` itself — the agent reads tool descriptions
# every turn during tool selection, so that's the most reliable injection
# point. See runtime/openclaw/plugins/nbhd-fuel-tools/index.js.


_GRAVITY_WEEKLY_PROMPT = (
    "Sunday-evening Gravity check-in. The user has finance tracking enabled.\n\n"
    "Pull the data you need via `nbhd_gravity_query` — never assert finance "
    "numbers from USER.md or memory. The four queries that cover this "
    "check-in:\n"
    "  1. This week's payments:\n"
    '     {"resource": "transactions", "window": {"kind": "last_n_days", "value": 7}}\n'
    "  2. Current active debts + balances:\n"
    '     {"resource": "accounts", "filter": {"is_debt": true}}\n'
    "  3. Active payoff plan (strategy, target date, monthly budget):\n"
    '     {"resource": "plan"}\n'
    '  4. Total debt for the opener ("debt down vs. last week", etc.):\n'
    '     {"resource": "accounts", "filter": {"is_debt": true}, '
    '"aggregate": "sum", "aggregate_field": "current_balance"}\n\n'
    "Decide top-priority debt yourself from query (3) plus (2): avalanche → "
    "highest interest_rate; snowball → lowest current_balance; hybrid → "
    "your judgment. Decide upcoming due dates from (2)'s due_day field vs. "
    "today's date in tenant tz.\n\n"
    "Send the user exactly ONE message via `nbhd_send_to_user` that:\n"
    "  - Opens with a brief acknowledgement of progress this week (debt down, "
    "payment made, milestone hit) OR a flag if something needs attention "
    "(missed payment, due date in next 7 days)\n"
    "  - Surfaces the top-priority debt + the next concrete payment they "
    "should make\n"
    "  - If a due date falls within the coming week, name it explicitly\n"
    "  - Keeps it conversational and short — 4-6 lines max, no data dump\n\n"
    "GROUNDING: every number you write in the message must come from a query "
    "result returned in this turn. If query (1) returns row_count=0, say "
    "'no payments logged this week' — don't pad with last week's figures. "
    "If a value looks wrong against what the user just told you, prefer the "
    "query result and ask the user to confirm.\n\n"
    "Cross-reference:\n"
    "  - If goals (`nbhd_document_get` kind='goal') contains a finance goal "
    "(debt-free target, savings target), tie progress to it.\n"
    "  - If recent journal mentions money stress / windfall, acknowledge it.\n"
    "  - If a planned workout or other commitment competes with payment "
    "timing this week, name the trade-off honestly.\n\n"
    "If queries (1) and (2) show no material change since last week (no "
    "payments, balances unchanged, no due dates approaching), it is OK to "
    "send a shorter check-in: 'Quiet week on the Gravity side — you're on "
    "track with [strategy from query (3)]. Anything to adjust?' Don't pad "
    "with stale content.\n\n"
    "**Send exactly ONE user-facing message via `nbhd_send_to_user`. After "
    "that message is sent, proceed to the FINAL STEP described below.**\n"
)


_FUEL_PREP_HOUR = {
    "morning": 6,  # 6:00am — before a morning workout
    "afternoon": 11,  # 11:00am — before an afternoon workout
    "evening": 16,  # 4:00pm — before an evening workout
}
_FUEL_PREP_DEFAULT_HOUR = 6  # fallback if preferred_time not set


def build_fuel_workout_cron(tenant: Tenant, plan, preferred_time: str = "") -> dict | None:
    """Build a background Fuel workout-prep cron tied to an active plan.

    Fires on training days at a time derived from the user's preferred_time
    (from FuelProfile). The cron is independent of all other scheduled tasks.
    Returns None if the plan has no valid schedule.
    """
    schedule_json = plan.schedule_json or {}
    if not schedule_json:
        return None

    # Convert plan weekday indices (0=Mon..6=Sun) to cron (0=Sun, 1=Mon..6=Sat)
    cron_days = []
    for day_str in schedule_json:
        try:
            plan_day = int(day_str)
            if 0 <= plan_day <= 6:
                cron_days.append((plan_day + 1) % 7)
        except (TypeError, ValueError):
            continue

    if not cron_days:
        return None

    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
    day_expr = ",".join(str(d) for d in sorted(cron_days))
    prep_hour = _FUEL_PREP_HOUR.get(preferred_time, _FUEL_PREP_DEFAULT_HOUR)
    cron_expr = f"0 {prep_hour} * * {day_expr}"

    return {
        "name": f"_fuel:{plan.name}",
        "schedule": {"kind": "cron", "expr": cron_expr, "tz": user_tz},
        "sessionTarget": "isolated",
        "payload": {
            "kind": "agentTurn",
            "message": _build_cron_message(
                _FUEL_WORKOUT_PREP_PROMPT,
                f"_fuel:{plan.name}",
                foreground=False,
                tenant=tenant,
            ),
        },
        "delivery": {"mode": "none"},
        "enabled": True,
    }


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
        DEEPSEEK_MODEL: {"alias": "deepseek"},
        GEMMA_MODEL: {"alias": "gemma"},
    },
}

# Per-task model defaults — stamp these onto specific cron jobs when the
# tenant hasn't set a `task_model_preferences` override. Crons not in this
# map inherit the tier primary (or the user's `preferred_model` override).
#
# The split is workload-driven, not tier-driven. DeepSeek V4 Pro for
# reasoning-shaped jobs (long context in, long output out, judgment about
# what's worth surfacing). Crons absent from this map (Personal Question,
# Background Tasks) inherit the chat primary so short one-shot prompts
# stay on the cheap-input model. Heartbeat is reasoning-shaped too but
# pinned separately via HEARTBEAT_MODEL so user `preferred_model` can't
# redirect a platform-initiated turn onto a BYO subscription.
#
# Keys mirror the values of `_TASK_SLUG_MAP` defined later in this file.
TIER_TASK_DEFAULTS: dict[str, dict[str, str]] = {
    "starter": {
        "morning_briefing": DEEPSEEK_MODEL,
        "evening_checkin": DEEPSEEK_MODEL,
        "weekly_reflection": DEEPSEEK_MODEL,
        "week_review": DEEPSEEK_MODEL,
        "project_checkin": DEEPSEEK_MODEL,
        "gravity_weekly_checkin": DEEPSEEK_MODEL,
    },
}


def _byo_model_extras(tenant: Tenant) -> dict[str, dict[str, Any]]:
    """Extra model entries the tenant can select via BYO subscription.

    Mirrors `TIER_MODEL_CONFIGS` shape (model_id → {"alias": ...}). Returns
    an empty dict when `tenant.byo_models_enabled` is False or no
    non-error BYO credential exists.

    Phase 1 exposes Claude Sonnet 4.6 and Claude Opus 4.7 via the Anthropic
    Claude CLI subscription path. Both use the canonical `anthropic/<model>`
    prefix; CLI routing is activated by the `anthropic:claude-cli` auth
    profile that `runtime/openclaw/entrypoint.sh` registers at boot.
    """
    from apps.billing.constants import ANTHROPIC_OPUS_MODEL as _OPUS_MODEL

    if not getattr(tenant, "byo_models_enabled", False):
        return {}

    # Late import — config_generator is imported during Django setup,
    # before app registries are fully loaded.
    from apps.byo_models.models import BYOCredential

    extras: dict[str, dict[str, Any]] = {}

    has_anthropic_cli = (
        tenant.byo_credentials.filter(
            provider=BYOCredential.Provider.ANTHROPIC,
            mode=BYOCredential.Mode.CLI_SUBSCRIPTION,
        )
        .exclude(status=BYOCredential.Status.ERROR)
        .exists()
    )
    if has_anthropic_cli:
        extras[ANTHROPIC_SONNET_MODEL] = {"alias": "claude-sonnet"}
        extras[_OPUS_MODEL] = {"alias": "claude-opus"}

    return extras


WHISPER_DEFAULT_MODEL = {"provider": "openai", "model": "gpt-4o-mini-transcribe"}

# Heartbeat model — pinned to DeepSeek V4 Pro so heartbeat judgment runs
# on a reasoning model regardless of the tenant's `preferred_model`. The
# pin also guarantees platform-initiated turns never burn a BYO Anthropic
# tenant's CLI subscription — repointing the constant preserves that
# invariant (still a non-BYO model). See `_HEARTBEAT_CHECKIN_PROMPT` for
# the judgment that lives in this turn — cross-referencing the morning
# briefing + heartbeat-log to decide whether anything is genuinely new.
HEARTBEAT_MODEL = DEEPSEEK_MODEL


def _heartbeat_cron_expr(start_hour: int, window_hours: int) -> str:
    """Compute cron hour expression for a heartbeat window.

    Handles midnight wrapping (e.g. start=22, window=6 → '0,1,2,3,22,23').
    """
    hours = [(start_hour + i) % 24 for i in range(window_hours)]
    return f"0 {','.join(str(h) for h in sorted(hours))} * * *"


def _build_heartbeat_defaults(tenant: Tenant) -> dict:
    """Build the ``agents.defaults.heartbeat`` block.

    Two shapes:

    - **Built-in heartbeat off** (default): ``{"every": "0m"}`` — disables
      OpenClaw's built-in periodic agent turn. Cron-based heartbeat is
      what fires user-facing check-ins; see ``_build_heartbeat_cron``.
    - **Built-in heartbeat on** (canary flag): every-1h periodic turn
      that runs inside the user's active hours and delivers any due
      inferred commitments. See docs/gateway/heartbeat and
      docs/concepts/commitments.

    The model is pinned to ``HEARTBEAT_MODEL`` so the gateway never burns
    a BYO Anthropic tenant's CLI subscription on heartbeat turns — those
    are platform-initiated, not user-requested.

    Active hours derive from the tenant's existing heartbeat-window
    fields (``heartbeat_start_hour`` / ``heartbeat_window_hours``) so we
    don't duplicate timezone-window configuration across two heartbeat
    surfaces. Timezone is the tenant's local TZ.
    """
    if not tenant.experimental_built_in_heartbeat:
        # Heartbeat disabled at the OpenClaw level. Cron-based heartbeat
        # (see _build_heartbeat_cron) fires our own check-in flow.
        return {"every": "0m"}

    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
    start_hour = tenant.heartbeat_start_hour
    end_hour = (start_hour + tenant.heartbeat_window_hours) % 24
    return {
        "every": "1h",
        "target": "last",
        "model": HEARTBEAT_MODEL,
        "directPolicy": "allow",
        "lightContext": True,
        "isolatedSession": True,
        "skipWhenBusy": True,
        "activeHours": {
            "start": f"{start_hour:02d}:00",
            "end": f"{end_hour:02d}:00",
            "timezone": user_tz,
        },
    }


def _build_commitments_config(tenant: Tenant) -> dict:
    """Build the top-level ``commitments`` block.

    Commitments only deliver through OpenClaw's built-in heartbeat. If
    the built-in heartbeat is off the extractor would still run after
    every agent reply but the inferred follow-ups would never surface —
    pure background cost with no user-facing benefit. So we gate
    commitments on the same flag as the built-in heartbeat.
    """
    if not tenant.experimental_built_in_heartbeat:
        return {"enabled": False}
    return {"enabled": True, "maxPerDay": 3}


def _build_memory_flush_block(tenant: Tenant) -> dict:
    """Build the ``agents.defaults.compaction.memoryFlush`` block.

    Gated on ``experimental_typed_journal_lifecycle``:

      - **Flag on (canary)**: prompt the agent to write goals/tasks through
        the typed lifecycle tools (``nbhd_goal_create``, ``nbhd_task_create``,
        etc.) and to keep memory/daily notes free of variable values that
        live in other systems (Gravity, Fuel). This is the source-of-truth-
        respecting variant that produced the original loan-staleness fix.
      - **Flag off (default fleet)**: legacy prompt referencing
        ``nbhd_memory_update`` / ``nbhd_daily_note_append`` only. Safe for
        tenants whose OpenClaw image doesn't yet have the typed tools.

    Both variants set ``enabled=True`` and ``softThresholdTokens=4000``.
    """
    if tenant.experimental_typed_journal_lifecycle:
        return {
            "enabled": True,
            "softThresholdTokens": 4000,
            "systemPrompt": (
                "Session nearing compaction. Save important context now, using the "
                "right surface for each kind of thing:\n"
                "- Goals (intentions with a target outcome): nbhd_goal_create / nbhd_goal_update / "
                "nbhd_goal_achieve / nbhd_goal_abandon\n"
                "- Tasks (actionable items with a status): nbhd_task_create / nbhd_task_complete / "
                "nbhd_task_skip / nbhd_task_defer\n"
                "- Durable facts about the user that have no other source of truth "
                "(preferences, principles, identity, learned patterns): nbhd_memory_update\n"
                "- Narrative reflection on today: nbhd_daily_note_append\n"
                "\n"
                "Do NOT capture current values (balances, totals, weights, counts, payment "
                "statuses, dates of specific events) into memory or daily notes — these "
                "live in their tracking systems (Gravity, Fuel, etc.) and should be queried "
                "fresh via the relevant pillar tool. Memory is for things no other system owns."
            ),
            "prompt": (
                "Review this conversation. Promote new goals via nbhd_goal_create, new tasks "
                "via nbhd_task_create, completed tasks via nbhd_task_complete. Capture "
                "genuinely durable user facts via nbhd_memory_update. Save narrative "
                "reflections via nbhd_daily_note_append. Do not record current values — "
                "query the source systems for those. Reply with NO_REPLY when done."
            ),
        }

    # Legacy variant — current fleet default. No mention of typed tools so
    # stale tenants (older OpenClaw image) don't get prompted to call tools
    # they don't have.
    return {
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
    }


def _build_memory_core_plugin_entry(tenant: Tenant) -> dict | None:
    """Build the ``plugins.entries["memory-core"]`` value, or None.

    Today this entry is only emitted when dreaming is on — the rest of
    memory-core's defaults are fine and don't need explicit config.
    Returns None when:

      - ``experimental_dreaming_enabled`` is False (every tenant today)
      - Or dreaming is on but memory-core itself isn't (dreaming IS the
        consolidation layer of memory-core; toggling without the engine
        is meaningless). Logged warning + skip.

    When both flags are True, returns the verified shape from
    ``docs/concepts/dreaming.md`` "Enable dreaming" tab. Phase cadence
    defaults to ``0 3 * * *`` (3am local) so it doesn't compete with
    the user's morning briefing or heartbeat window. We don't tune
    ``frequency`` explicitly today — observe canary first.
    """
    if not tenant.experimental_dreaming_enabled:
        return None

    if not tenant.experimental_memory_core_enabled:
        import logging

        logging.getLogger(__name__).warning(
            "Tenant %s has experimental_dreaming_enabled=True but "
            "experimental_memory_core_enabled=False — dreaming requires "
            "memory-core as its consolidation backend. Skipping memory-core "
            "plugin entry. Enable memory-core first.",
            str(tenant.id)[:8],
        )
        return None

    return {
        "enabled": True,
        "config": {
            "dreaming": {
                "enabled": True,
            },
        },
    }


def _build_active_memory_plugin_entry(tenant: Tenant) -> dict | None:
    """Build the ``plugins.entries.active-memory`` value, or None.

    Returns None when:

      - ``experimental_active_memory_enabled`` is False (every tenant
        today); the plugin entry is omitted entirely so OpenClaw treats
        the plugin as absent.
      - The flag is True but ``experimental_memory_core_enabled`` is
        False — active-memory calls ``memory_search`` internally and
        without memory-core that's a no-op at best, a runtime error at
        worst. We log a warning and skip; canary should observe the
        intended setup explicitly.

    When both flags are True, returns the validated config dict
    documented at ``docs/concepts/active-memory.md`` and verified
    against ``dist/extensions/active-memory/openclaw.plugin.json``:

      - ``allowedChatTypes: ["direct"]`` — LINE + Telegram DMs are
        ``direct`` per OpenClaw's session-type taxonomy
      - ``queryMode: "recent"`` — last few user/assistant turns plus
        current message; balanced speed vs grounding (per the doc's
        recommended starter setup)
      - ``promptStyle: "balanced"`` — default for ``recent`` mode
      - ``timeoutMs: 15000`` — hard cap on the recall sub-agent. The
        circuit breaker (default 3 consecutive timeouts) skips recall
        if it keeps failing, so the main reply path isn't dragged.
      - ``setupGraceTimeoutMs: 30000`` — extra budget for the first
        recall after a gateway restart while model warm-up and the
        embedding index load are still in flight. Documented as the
        v2026.5.2 cold-start grace; without it the first heartbeat
        after a restart is likely to time out.

    Model is left unset so the plugin inherits the agent's session
    model. The doc recommends pinning a fast model (cerebras /
    gemini-flash) for latency-sensitive recall — TODO once we have a
    cerebras provider configured.
    """
    if not tenant.experimental_active_memory_enabled:
        return None

    if not tenant.experimental_memory_core_enabled:
        import logging

        logging.getLogger(__name__).warning(
            "Tenant %s has experimental_active_memory_enabled=True but "
            "experimental_memory_core_enabled=False — active-memory plugin "
            "requires memory-core as its recall backend. Skipping plugin "
            "entry. Enable memory-core first or clear the active-memory flag.",
            str(tenant.id)[:8],
        )
        return None

    return {
        "enabled": True,
        "config": {
            "enabled": True,
            "agents": ["main"],
            "allowedChatTypes": ["direct"],
            "queryMode": "recent",
            "promptStyle": "balanced",
            "timeoutMs": 15000,
            "setupGraceTimeoutMs": 30000,
            "maxSummaryChars": 220,
            "persistTranscripts": False,
            "logging": False,
        },
    }


def _build_memory_search_config(tenant: Tenant) -> dict:
    """Build the ``agents.defaults.memorySearch`` block.

    Two shapes:

    - **Off** (default): ``{"enabled": False}`` — preserves PR #525
      semantics. Search routes through ``nbhd_journal_search`` over
      Postgres; OpenClaw's memory_search/memory_get tools are denied at
      the tool-policy layer for older OpenClaw versions.
    - **On** (canary flag): full memory-core engine. SQLite index lives
      at ``/home/node/.openclaw/index/memory/{agentId}.sqlite`` — that
      path is mounted via an ``index-cache`` EmptyDir volume (see
      ``azure_client.py``) so a container kill mid-write can't corrupt
      the file on SMB (the bug that motivated #525). The markdown files
      the index points at — ``MEMORY.md``, ``memory/YYYY-MM-DD.md`` —
      still live on the workspace share, where they belong.

    The ``{agentId}`` token is interpolated by OpenClaw at index time
    (``dist/memory-search-DbWvVOpI.js:37`` —
    ``raw.replaceAll("{agentId}", agentId)``). Our agent id is "main"
    so the resolved path is ``…/index/memory/main.sqlite``.

    Trigram FTS5 tokenizer because the workspace contains mixed
    English / Japanese (JST tenants) and short technical tokens like
    "69kg" or "RPE 8" — trigram handles both noticeably better than
    the default unicode61.
    """
    if not tenant.experimental_memory_core_enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "store": {
            "path": "/home/node/.openclaw/index/memory/{agentId}.sqlite",
            "fts": {"tokenizer": "trigram"},
        },
    }


def _build_heartbeat_cron(tenant: Tenant) -> dict | None:
    """Build heartbeat cron job definition for a tenant.

    Returns None if heartbeat is disabled, or if the tenant is on the
    experimental built-in heartbeat path — both heartbeats firing in the
    same activeHours window would deliver duplicate / overlapping
    messages to the user.
    """
    if not tenant.heartbeat_enabled or tenant.experimental_built_in_heartbeat:
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
                _HEARTBEAT_CHECKIN_PROMPT,
                "Heartbeat Check-in",
                foreground=True,
                tenant=tenant,
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
                    "Morning Briefing",
                    foreground=True,
                    tenant=tenant,
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
                    "Evening Check-in",
                    foreground=True,
                    tenant=tenant,
                ),
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        },
        {
            "name": "Personal Question",
            "schedule": {
                "kind": "cron",
                "expr": f"0 {tenant.heartbeat_start_hour % 24} * * *",
                "tz": user_tz,
            },
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _build_cron_message(
                    _PERSONAL_QUESTION_PROMPT,
                    "Personal Question",
                    foreground=True,
                    tenant=tenant,
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
                    "Weekly Reflection",
                    foreground=True,
                    tenant=tenant,
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
                    "Week Ahead Review",
                    foreground=True,
                    tenant=tenant,
                ),
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        },
        {
            "name": "Project Check-in",
            "schedule": {
                "kind": "cron",
                "expr": f"0 {(tenant.heartbeat_start_hour + tenant.heartbeat_window_hours // 2) % 24} * * 1-5",
                "tz": user_tz,
            },
            "sessionTarget": "isolated",
            "payload": {
                "kind": "agentTurn",
                "message": _build_cron_message(
                    _PROJECT_CHECKIN_PROMPT,
                    "Project Check-in",
                    foreground=True,
                    tenant=tenant,
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
                    "Background Tasks",
                    foreground=False,
                    tenant=tenant,
                ),
            },
            "delivery": {"mode": "none"},
            "enabled": True,
        },
        # NOTE: Nightly Extraction is NOT an OpenClaw cron job — it's a
        # Django endpoint (/api/v1/journal/extract/) triggered via QStash.
        # See apps/journal/extraction_views.py.
    ]

    # Gravity Weekly Check-in — Sunday 19:00 user TZ when finance is enabled.
    # Different hour from Weekly Reflection (20:00) so they don't collide.
    if getattr(tenant, "finance_enabled", False):
        jobs.append(
            {
                "name": "Gravity Weekly Check-in",
                "schedule": {"kind": "cron", "expr": "0 19 * * 0", "tz": user_tz},
                "sessionTarget": "isolated",
                "payload": {
                    "kind": "agentTurn",
                    "message": _build_cron_message(
                        _GRAVITY_WEEKLY_PROMPT,
                        "Gravity Weekly Check-in",
                        foreground=True,
                        tenant=tenant,
                    ),
                },
                "delivery": {"mode": "none"},
                "enabled": True,
            }
        )

    # Heartbeat cron — hourly during user's chosen window, cheap model
    heartbeat_job = _build_heartbeat_cron(tenant)
    if heartbeat_job is not None:
        jobs.append(heartbeat_job)

    # Fuel workout-prep cron — background, fires on training days at the
    # user's preferred workout time. Independent of all other crons.
    #
    # Suppressed when the tenant is on the new per-session flow
    # (FuelProfile.use_session_scheduling=True): in that case ``_fuel:*``
    # crons are derived from Workout.scheduled_at by
    # ``apps/orchestrator/fuel_cron.py::regenerate_fuel_crons``.
    if getattr(tenant, "fuel_enabled", False):
        from apps.fuel.models import FuelProfile, WorkoutPlan

        use_session_scheduling = False
        pref_time = ""
        try:
            profile = FuelProfile.objects.get(tenant=tenant)
            use_session_scheduling = profile.use_session_scheduling
            pref_time = profile.preferred_time
        except FuelProfile.DoesNotExist:
            pass

        if not use_session_scheduling:
            active_plan = WorkoutPlan.objects.filter(tenant=tenant, status="active").order_by("-created_at").first()
            if active_plan:
                fuel_job = build_fuel_workout_cron(tenant, active_plan, preferred_time=pref_time)
                if fuel_job:
                    jobs.append(fuel_job)

    # Apply per-task model overrides from tenant preferences. Only stamp
    # models that are actually allowed for this tenant (tier allowlist
    # plus BYO extras) — a stale preference from a lapsed BYO setup
    # (e.g. ``anthropic-cli/claude-sonnet-4-6`` on a starter tier with no
    # BYO credentials) would otherwise produce a cron whose preflight
    # rejects with ``payload.model '...' rejected by agents.defaults.models
    # allowlist`` and silently never fires. Canary 2026-05-14 reproduction:
    # task_model_preferences carried three stale anthropic-cli entries
    # left over from BYO, killing Morning Briefing / Evening Check-in /
    # Week Ahead Review until this guard landed.
    _TASK_SLUG_MAP = {
        "Morning Briefing": "morning_briefing",
        "Evening Check-in": "evening_checkin",
        "Personal Question": "personal_question",
        "Weekly Reflection": "weekly_reflection",
        "Week Ahead Review": "week_review",
        "Background Tasks": "background_tasks",
        "Project Check-in": "project_checkin",
        "Gravity Weekly Check-in": "gravity_weekly_checkin",
        "Heartbeat Check-in": "heartbeat",
    }
    # Resolve model per cron job: user-set `task_model_preferences` wins,
    # then `TIER_TASK_DEFAULTS` for reasoning-shaped crons, then inherit
    # the chat primary (no stamp). Allowlist guard stays — same lapsed-BYO
    # preflight failure mode the original `if prefs` block was guarding.
    tier = tenant.model_tier or "starter"
    allowed_models = set(TIER_MODEL_CONFIGS.get(tier, TIER_MODEL_CONFIGS["starter"]))
    allowed_models.update(_byo_model_extras(tenant))
    prefs = getattr(tenant, "task_model_preferences", None) or {}
    task_defaults = TIER_TASK_DEFAULTS.get(tier, {})
    for job in jobs:
        slug = _TASK_SLUG_MAP.get(job["name"], "")
        if not slug:
            continue
        # User preference wins if it's in the allowlist; otherwise fall
        # through to the tier default. A stale-but-set pref pointing at a
        # disallowed model (e.g. anthropic-cli/... left over from a torn-
        # down BYO setup) still gets dropped, but the cron lands on the
        # tier default rather than silently un-stamped.
        candidate = prefs.get(slug)
        if not candidate or candidate not in allowed_models:
            candidate = task_defaults.get(slug)
        if candidate and candidate in allowed_models:
            job["model"] = candidate

    return jobs


def _build_tools_section(tier: str, version: str = OPENCLAW_CURRENT_VERSION) -> dict[str, Any]:
    """Build documented OpenClaw tools policy for subscriber tier."""
    tools = generate_tool_config(tier, version=version)
    tools["media"] = {
        "audio": {
            "enabled": True,
            "models": [WHISPER_DEFAULT_MODEL],
        },
    }
    # Tool-call loop detection. Off by default upstream — we turn it on as
    # cheap defense against runaway tool ping-pong (the cron-reconciler
    # remove/add storm we saw in the 2026-05-14 logs is a candidate
    # pattern). Token-level degeneration is handled separately by the
    # nbhd-routing-context plugin's before_agent_finalize hook; this is
    # the tool-call abstraction layer. Defaults match the upstream
    # documentation: warning at 10, critical block at 20.
    tools["loopDetection"] = {
        "enabled": True,
    }
    return tools


def _build_channels_config(tenant: Tenant) -> dict[str, Any]:
    """Build channels config based on which messaging channels the tenant has linked.

    Only enables channels the user has actually connected (has a chat/user ID).
    Falls back to the preferred_channel if nothing is linked yet (pre-connection
    provisioning), so the assistant still knows which surface to expect.
    """
    user = tenant.user
    channels: dict[str, Any] = {}

    if getattr(user, "telegram_chat_id", None):
        channels["telegram"] = {"enabled": True}
    if getattr(user, "line_user_id", None):
        channels["line"] = {"enabled": True}

    # Fallback: if no channel linked yet, enable the preferred channel so the
    # assistant can format messages for the expected surface during onboarding.
    if not channels:
        preferred = getattr(user, "preferred_channel", "telegram") or "telegram"
        channels[preferred] = {"enabled": True}

    return channels


def _build_logging_config() -> dict[str, Any]:
    """Tenant-content redaction patterns for OpenClaw's built-in `redactToolDetail`.

    This is layer 1 of the redaction stack — patterns here run inside
    `formatToolParamPreview`/`redactToolDetail` (openclaw@2026.5.7
    `dist/redact-*.js` :: `resolveConfigRedaction` → `resolvePatterns`),
    redacting JSON values inside the `raw_params={...}` / `effective_params=
    {...}` blocks of `[tools] X failed: ...` error lines BEFORE they reach
    stderr. The `runtime/openclaw/redact-stdout.js` sidecar is layer 2,
    catching anything that bypasses this layer (notably bare assistant
    reply text on stdout, since the gateway fast path skips
    `enableConsoleCapture` — see memory
    project_openclaw_gateway_skips_console_capture.md).

    Upstream behavior to know:
      - `resolvePatterns(value)` returns `value` if non-empty, else
        `DEFAULT_REDACT_PATTERNS`. It does NOT merge — providing our own
        list fully replaces the upstream auth/token defaults for this path.
        We include a maintained subset of those defaults below so auth
        tokens stay masked.
      - Patterns are JS regex strings; OpenClaw compiles them via
        `compileSafeRegex` with `gi` flags. The matched group is masked
        via `maskToken` (first 6 + … + last 4 chars) when the value is
        ≥18 chars, else `***`.

    Verify on every OpenClaw version bump: re-pull
    `npm pack openclaw@<v>` and diff `DEFAULT_REDACT_PATTERNS` in
    `dist/redact-*.js` against the auth subset below. See memory
    reference_openclaw_source_extraction.md for the extraction recipe.
    """
    return {
        "redactSensitive": "tools",
        "redactPatterns": [
            # ── NBHD content-field redaction (the actual leak being closed) ──
            # Matches "message":"value" / "text":"value" / etc inside the
            # tool-call raw_params or effective_params JSON. Captures the
            # value group so it's the part that gets masked.
            r'"(?:message|text|content|prompt|response|reply|body|caption|user_text|userText|assistantText)"\s*:\s*"((?:[^"\\]|\\.)*)"',
            # ── Auth/secret subset from openclaw@2026.5.7 DEFAULT_REDACT_PATTERNS ──
            # Maintained here because providing redactPatterns replaces the
            # upstream defaults for the `redactToolDetail` path. Only the
            # high-signal patterns are kept (tokens, bearer, well-known
            # provider key prefixes); the long-tail provider patterns
            # (npm_, hf_, r8_, etc.) are dropped to keep this list short —
            # the layer-2 sidecar still catches anything that slips past.
            r'"(?:apiKey|token|secret|password|passwd|accessToken|refreshToken)"\s*:\s*"([^"]+)"',
            r"Authorization\s*[:=]\s*Bearer\s+([A-Za-z0-9._\-+=]+)",
            r"\bBearer\s+([A-Za-z0-9._\-+=]{18,})\b",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----",
            r"\b(sk-[A-Za-z0-9_-]{8,})\b",
            r"\b(ghp_[A-Za-z0-9]{20,})\b",
            r"\b(github_pat_[A-Za-z0-9_]{20,})\b",
            r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b",
            r"\b(AIza[0-9A-Za-z\-_]{20,})\b",
            r"\bbot(\d{6,}:[A-Za-z0-9_-]{20,})\b",
        ],
    }


def generate_openclaw_config(tenant: Tenant) -> dict[str, Any]:
    """Generate a complete openclaw.json for a tenant's container.

    This is the config that gets written to the container's
    ~/.openclaw/openclaw.json (or mounted via Azure Files).
    """
    chat_id = tenant.user.telegram_chat_id  # may be None before Telegram linking
    tier = tenant.model_tier or "starter"
    oc_version = getattr(tenant, "openclaw_version", OPENCLAW_CURRENT_VERSION) or OPENCLAW_CURRENT_VERSION
    models_config = TIER_MODELS.get(tier, TIER_MODELS["starter"])
    model_entries = TIER_MODEL_CONFIGS.get(tier, TIER_MODEL_CONFIGS["starter"])

    # BYO subscription extras (e.g. anthropic/claude-sonnet-4-6) — extend
    # the tier's allowed-model dict so the user's preferred_model can
    # land on a BYO model below.
    byo_extras = _byo_model_extras(tenant)
    if byo_extras:
        model_entries = {**model_entries, **byo_extras}

    # Allow user to override primary model within their tier (or BYO extras).
    if tenant.preferred_model and tenant.preferred_model in model_entries:
        models_config = {**models_config, "primary": tenant.preferred_model}

    # Silent-fallback guard for BYO models. When the resolved primary is a
    # BYO model (e.g. anthropic/claude-sonnet-4-6 routed through the user's
    # own Claude subscription), a billing failure on that account must NOT
    # fall through to MiniMax — the user has paid Anthropic specifically to
    # use Claude, and they expect to either get Claude or a clear error
    # they can act on. With `fallbacks: []` OpenClaw 2026.4.25's
    # `runWithModelFallback` raises the original billing error directly
    # (see `throwFallbackFailureSummary`: when there's only one candidate
    # the lastError is rethrown as-is), which the assistant then surfaces
    # to the user via the channel router.
    primary_model = models_config["primary"]
    primary_is_byo = bool(byo_extras) and primary_model in byo_extras
    if primary_is_byo:
        fallbacks_list: list[str] = []
    else:
        fallbacks_list = [m for m in model_entries if m != primary_model]

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
        # Settings plugin — primary-model read + switch (nbhd_get_preferred_model_state,
        # nbhd_set_preferred_model). Unconditional in production via base.py default
        # so every tenant can ask its assistant about models and route switch
        # requests through the same tier gate as the dashboard. Tests disable by
        # setting OPENCLAW_SETTINGS_PLUGIN_ID="".
        (
            str(getattr(settings, "OPENCLAW_SETTINGS_PLUGIN_ID", "") or "").strip(),
            str(getattr(settings, "OPENCLAW_SETTINGS_PLUGIN_PATH", "") or "").strip(),
        ),
        # Routing-context plugin — degenerate output guard
        # (before_agent_finalize + message_sending). Unconditional in
        # production via base.py default. The before_prompt_build
        # workspace-catalogue injection was removed 2026-05-20 along with
        # workspace-based chat routing — see
        # docs/implementation/remove-workspace-chat-routing.md. Tests
        # disable the plugin by setting OPENCLAW_ROUTING_CONTEXT_PLUGIN_ID="".
        (
            str(getattr(settings, "OPENCLAW_ROUTING_CONTEXT_PLUGIN_ID", "") or "").strip(),
            str(getattr(settings, "OPENCLAW_ROUTING_CONTEXT_PLUGIN_PATH", "") or "").strip(),
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
        _plugin_defs.append(
            (
                str(getattr(settings, "OPENCLAW_REDDIT_PLUGIN_ID", "nbhd-reddit-tools") or "").strip(),
                str(
                    getattr(settings, "OPENCLAW_REDDIT_PLUGIN_PATH", "/opt/nbhd/plugins/nbhd-reddit-tools") or ""
                ).strip(),
            )
        )

    # Finance plugin — conditionally loaded when tenant has finance enabled
    if getattr(tenant, "finance_enabled", False):
        _plugin_defs.append(
            (
                str(getattr(settings, "OPENCLAW_FINANCE_PLUGIN_ID", "nbhd-finance-tools") or "").strip(),
                str(
                    getattr(settings, "OPENCLAW_FINANCE_PLUGIN_PATH", "/opt/nbhd/plugins/nbhd-finance-tools") or ""
                ).strip(),
            )
        )

    # Fuel plugin — conditionally loaded when tenant has fuel enabled
    if getattr(tenant, "fuel_enabled", False):
        _plugin_defs.append(
            (
                str(getattr(settings, "OPENCLAW_FUEL_PLUGIN_ID", "nbhd-fuel-tools") or "").strip(),
                str(getattr(settings, "OPENCLAW_FUEL_PLUGIN_PATH", "/opt/nbhd/plugins/nbhd-fuel-tools") or "").strip(),
            )
        )

    # Insights plugin — trajectory tools (history/drill/compare) over pillar
    # snapshots. Phase 1 only emits Gravity snapshots, so we gate on
    # finance_enabled. Expand to all tenants once Fuel/Core snapshot
    # pipelines ship.
    if getattr(tenant, "finance_enabled", False):
        _plugin_defs.append(
            (
                str(getattr(settings, "OPENCLAW_INSIGHTS_PLUGIN_ID", "nbhd-insights-tools") or "").strip(),
                str(
                    getattr(settings, "OPENCLAW_INSIGHTS_PLUGIN_PATH", "/opt/nbhd/plugins/nbhd-insights-tools") or ""
                ).strip(),
            )
        )

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
                    "fallbacks": fallbacks_list,
                },
                "models": model_entries,
                "workspace": "/home/node/.openclaw/workspace",
                "userTimezone": str(getattr(tenant.user, "timezone", "") or "UTC"),
                "envelopeTimezone": "user",
                # Workspace bootstrap budget — OpenClaw injects USER.md / AGENTS.md /
                # SOUL.md etc. into the system prompt on every turn. Defaults are
                # 12 000 chars per file and 60 000 chars total. USER.md alone runs
                # past the default for any tenant with the Phase 2 insights
                # observation-mode prompt enabled (the static rule block is ~6 KB,
                # tenant state pushes it past 15 KB) — the tail (Privacy Placeholders,
                # Recent journal, Fuel/Gravity state) is silently dropped before
                # injection, causing degraded replies. 18 000 / 80 000 restores the
                # full envelope with margin. Long term: move the static rule text
                # out of USER.md into AGENTS.md or the system-prompt block (Phase C1).
                "bootstrapMaxChars": 18000,
                "bootstrapTotalMaxChars": 80000,
                "compaction": {
                    "mode": "safeguard",
                    "memoryFlush": _build_memory_flush_block(tenant),
                },
                "memorySearch": _build_memory_search_config(tenant),
                "heartbeat": _build_heartbeat_defaults(tenant),
                "maxConcurrent": 2,
                "subagents": {
                    "maxConcurrent": 2,
                    "model": models_config["primary"],
                },
            },
        },
        # Messaging channels — only enable the channel(s) the tenant has
        # actually linked.  The central Django router handles Telegram/LINE
        # I/O; no bot tokens are set.  Enabling an unlinked channel causes
        # plugin validation errors in OpenClaw >= 2026.4.21 (e.g. "telegram
        # missing register/activate export").
        "channels": _build_channels_config(tenant),
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
        "tools": _build_tools_section(tier, version=oc_version),
        # Messages
        "messages": {
            "ackReactionScope": "group-mentions",
        },
        # Cron runtime settings (job definitions seeded via Gateway API)
        "cron": {"enabled": True},
        # Session reset policy — bound the blast radius of any stuck
        # session state. Idle reset at 4h is short enough to recover
        # within a day yet long enough not to trash a mid-conversation
        # context. Originally introduced as a guard against stale
        # `tenant.active_workspace` (per the 2026-05-14 incident); the
        # workspace concept has since been removed from chat routing
        # (see docs/implementation/remove-workspace-chat-routing.md) but
        # the bound remains useful for any other source of session drift.
        "session": {
            "reset": {
                "mode": "idle",
                "idleMinutes": 240,
            },
        },
        # Inferred commitments — hidden background extraction pass after each
        # agent reply notices conversation-bound open loops ("I have an
        # interview tomorrow", "I was up all night") and stores them for
        # heartbeat delivery. See docs/concepts/commitments. Only useful
        # when the built-in heartbeat is on (delivery happens through it),
        # so gate on the same flag.
        "commitments": _build_commitments_config(tenant),
        # Layer-1 log redaction — see _build_logging_config docstring.
        "logging": _build_logging_config(),
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
                pid: ({"enabled": True, "config": {"tier": tier}} if pid == image_gen_id else {"enabled": True})
                for pid, _ in _active_plugins
            },
        }

        # OpenClaw built-in active-memory plugin — bundled with OC core,
        # not an NBHD plugin, so it's not part of _active_plugins and
        # doesn't need plugin_config["allow"] entries (bundledDiscovery
        # in 2026.5+ handles bundled plugins). Just inject into entries
        # if the tenant flag is on.
        active_memory_entry = _build_active_memory_plugin_entry(tenant)
        if active_memory_entry is not None:
            plugin_config["entries"]["active-memory"] = active_memory_entry

        # Same pattern for the memory-core plugin entry: bundled, only
        # emitted when we have config to set on it. Today that's just
        # dreaming; if we end up tuning anything else (FTS tokenizer
        # location, dreaming.frequency, etc.) it'll land in the same
        # entry.
        memory_core_entry = _build_memory_core_plugin_entry(tenant)
        if memory_core_entry is not None:
            plugin_config["entries"]["memory-core"] = memory_core_entry

        paths = [ppath for _, ppath in _active_plugins if ppath]
        if paths:
            plugin_config["load"] = {"paths": paths}

        # OC 5.2 tightened plugins.allow into a hard allowlist that gates
        # bundled-provider discovery too (anthropic, openrouter, memory-core,
        # telegram). We don't allowlist those — they activate via channel/
        # provider auto-discovery. "compat" preserves the pre-5.2 behavior
        # so bundled providers stay loadable without enumeration.
        # See dist/legacy-config-migrations-*.js (bundledDiscovery migration).
        from apps.orchestrator.tool_policy import _parse_version as _pv

        if _pv(oc_version) >= (2026, 5, 0):
            plugin_config["bundledDiscovery"] = "compat"

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
            config.setdefault("env", {})["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] = "/workspace/gws-credentials.json"

            # GWS skills — read-only for now
            gws_skill_names = [
                "gws-shared",
                "gws-gmail-triage",
                "gws-calendar-agenda",
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

    # OpenClaw >= 2026.4.15 auto-generates models.json with the wrong
    # OpenRouter base URL (/v1 instead of /api/v1). Inject the override.
    from apps.orchestrator.tool_policy import _parse_version

    if _parse_version(oc_version) >= (2026, 4, 15):
        models_section = config.setdefault("models", {})
        providers = models_section.setdefault("providers", {})
        providers["openrouter"] = {
            "baseUrl": "https://openrouter.ai/api/v1",
            "models": [],
        }

    # OpenClaw 2026.5.2 retired the 60s default idle-token watchdog config
    # that lived under agents.defaults.llm. Boot fails with "Unrecognized
    # key: llm" if we keep emitting it. The replacement is per-provider
    # timeoutSeconds. Slower OpenRouter models (e.g. minimax-m2.7) on heavy
    # prompts routinely need more than 60s between tokens, so set 300s.
    if _parse_version(oc_version) >= (2026, 5, 0):
        models_section = config.setdefault("models", {})
        providers = models_section.setdefault("providers", {})
        openrouter_provider = providers.setdefault("openrouter", {})
        openrouter_provider["timeoutSeconds"] = 300
    else:
        # Legacy schema for OC < 5.0 — agents.defaults.llm.idleTimeoutSeconds
        # was the only way to override the LLM idle watchdog.
        config["agents"]["defaults"]["llm"] = {"idleTimeoutSeconds": 300}

    # BYO routing: with auth profile `anthropic:claude-cli` registered by
    # `runtime/openclaw/entrypoint.sh` (via `openclaw models auth login
    # --provider anthropic --method cli`), OpenClaw 2026.4.25 routes any
    # `anthropic/<model>` request through the bundled `claude` binary.
    #
    # Override the spawn command with our wrapper so the binary still gets
    # `CLAUDE_CODE_OAUTH_TOKEN` in its env. OpenClaw's claude-cli backend
    # explicitly clears that env var before spawning (see
    # `extensions/anthropic/cli-shared.js#CLAUDE_CLI_CLEAR_ENV`); its
    # assumption is that auth lives in `~/.claude/.credentials.json` from
    # an interactive `claude auth login`. The BYO flow only has a bare
    # access token from `claude setup-token` — works as the env var, not
    # standalone in the file. The wrapper reads the file `entrypoint.sh`
    # writes from CLAUDE_CODE_OAUTH_TOKEN and re-exports the env var
    # before exec'ing claude.
    #
    # Safe for non-BYO tenants: the wrapper is a no-op when the file is
    # absent (it just exec's claude with whatever env it inherits).
    if byo_extras and ANTHROPIC_SONNET_MODEL in byo_extras:
        cli_backends = config["agents"]["defaults"].setdefault("cliBackends", {})
        cli_backends["claude-cli"] = {"command": "/opt/nbhd/claude-with-token.sh"}

    return config


def config_to_json(config: dict[str, Any]) -> str:
    """Serialize config to JSON string."""
    return json.dumps(config, indent=2)
