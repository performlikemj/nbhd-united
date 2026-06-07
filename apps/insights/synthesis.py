"""Weekly Gravity reflection synthesis — Phase 4 proactive observation.

Once a week the platform looks at the last 7 days of a tenant's Gravity
data and writes a single synthesis observation into the assistant's memory.
This happens on the Django side, *not* through the per-tenant
OpenClaw runtime, so:

1. The token cost is billed to the platform (``record_usage(is_system=True)``),
   not the user's monthly cost budget.
2. The container doesn't need to wake — the synthesis runs even for
   hibernated tenants.
3. There's no message sent to the user. The reflection lands as both
   an ``AssistantInsight`` row (the assistant's memory) and a
   ``Document(kind=WEEKLY)`` (rendered as a Weekly Pulse card on
   Horizons). The user discovers it when they next open Horizons or
   ask the agent ``what have you noticed about me lately?``.

Phase 3 prerequisite reminder: voice register is per-topic and may be
overridden by ``UserVoicePref``. The synthesis prompt carries the
relevant pref so the reflection's tone matches the user's preference
for that pillar/topic. Without Phase 3 landed, this would feel pushy —
see CONTINUITY_assistant_baseline.md Phase 4 exit-criteria.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from django.utils import timezone

from apps.billing.constants import MINIMAX_MODEL
from apps.billing.services import record_usage
from apps.common.openrouter import chat_completion
from apps.insights.markers import extract_and_record_insights
from apps.insights.models import AssistantInsight, PillarSnapshot, UserVoicePref
from apps.insights.pillars import Pillar
from apps.journal.models import Document
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Reasoning-shaped synthesis (one short reflection per tenant per week);
# DeepSeek V4 Pro replaced Kimi K2.6 here — same workload (long-context
# reasoning over a week of data), materially cheaper output rate.
SYNTHESIS_MODEL = "openrouter/deepseek/deepseek-v4-pro"

# Fallback chain if DeepSeek is unreachable on OpenRouter — MiniMax is the
# cheapest capable alternative. A degraded reflection beats a silently skipped
# weekly observation.
SYNTHESIS_MODELS = [SYNTHESIS_MODEL, MINIMAX_MODEL]

# Hard ceilings so a single tenant can't unexpectedly balloon platform spend.
_MAX_INPUT_CONTEXT_CHARS = 12000
_MAX_OUTPUT_TOKENS = 600


# ── Domain ────────────────────────────────────────────────────────────


@dataclass
class WeeklyReflectionResult:
    """Outcome of one synthesis run. ``skipped`` is non-empty when nothing was written."""

    tenant_id: str
    skipped: str = ""
    insight_id: str | None = None
    document_id: str | None = None
    iso_week: str | None = None


# ── Prompt ────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You are the assistant's reflection writer for one user. Your job is to look at the
last week's Gravity (finance) data and write ONE short reflection observation that
the assistant should remember about this user going forward.

The reflection has two parts in one short reply:

1. Two to four sentences of prose summarizing the week — what changed, what the
   user is steering toward or away from. Specific, not generic.
2. Exactly one inline marker wrapping the pattern observation worth remembering:

   [[insight:topic_slug]]observation about this user[[/insight]]

   - `topic_slug` is one of: dining, debt, savings, subscriptions, discretionary,
     fixed_expenses, income, large_purchases. Pick the topic the observation is
     about; if nothing fits, fall back to discretionary.
   - The wrapped statement should be about *this user* (not generic advice), should
     be falsifiable (they can confirm or correct), and should be a single observation.

Voice register comes from the user's stated preference. If the prefs say `gentle`,
phrase the observation as a question / hypothesis. If `direct`, state it plainly.

Do NOT include a salutation or sign-off. Do NOT propose actions in this reflection;
the assistant has separate paths for that. Do NOT include chart markers or other markup.
If the week has no signal worth reflecting on (no data, or completely flat), respond
with the literal string `NO_REFLECTION` and nothing else.\
"""


# ── Public entrypoint ─────────────────────────────────────────────────


def generate_weekly_reflection(tenant: Tenant, *, now: datetime | None = None) -> WeeklyReflectionResult:
    """Run synthesis for one tenant. Idempotent per ISO week.

    Returns a result indicating what happened. ``skipped`` is set when nothing
    was written and explains why (already-ran-this-week, no-data, llm-error,
    no-reflection, etc.). Never raises — callers (cron dispatcher) iterate
    over many tenants and shouldn't be blocked by one failure.
    """
    now = now or timezone.now()
    iso_year, iso_week, _ = now.isocalendar()
    iso_week_str = f"{iso_year}-W{iso_week:02d}"
    result = WeeklyReflectionResult(tenant_id=str(tenant.id), iso_week=iso_week_str)

    if not getattr(tenant, "finance_active", False):
        result.skipped = "finance_disabled"
        return result

    if _already_ran_this_week(tenant, iso_week_str):
        result.skipped = "already_ran"
        return result

    if _voice_pref_silent(tenant):
        result.skipped = "volume_silent"
        return result

    context = _build_context(tenant, now=now)
    if not context["has_data"]:
        result.skipped = "no_data"
        return result

    try:
        reply_text, usage = _call_synthesis_llm(context)
    except Exception:
        logger.exception("weekly_reflection: LLM call failed for tenant %s", str(tenant.id)[:8])
        result.skipped = "llm_error"
        return result

    # Bill to platform, not tenant.
    try:
        record_usage(
            tenant=tenant,
            event_type="weekly_reflection",
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model_used=SYNTHESIS_MODEL,
            is_system=True,
        )
    except Exception:
        # Usage recording is non-fatal; the synthesis output is what matters.
        logger.exception("weekly_reflection: usage record failed for tenant %s", str(tenant.id)[:8])

    reply_text = (reply_text or "").strip()
    if not reply_text or reply_text == "NO_REFLECTION":
        result.skipped = "no_reflection"
        return result

    # Extract insight marker → AssistantInsight row + strip marker tokens
    # from the prose. Re-uses PR #645's extractor so the source-of-truth
    # for insight recording stays in one place.
    cleaned_text = extract_and_record_insights(reply_text, tenant=tenant, pillar=Pillar.GRAVITY.value)

    # The most recent open insight created in this run is the one this
    # reflection birthed. (We don't pass the slug through; resolve it from
    # the latest open row for this tenant scoped to gravity.)
    latest = (
        AssistantInsight.objects.filter(
            tenant=tenant,
            pillar=Pillar.GRAVITY.value,
            status=AssistantInsight.Status.OPEN,
        )
        .order_by("-created_at")
        .first()
    )
    if latest is not None:
        result.insight_id = str(latest.id)

    # Render the reflection prose as a Document(kind=WEEKLY) so it shows up
    # in Horizons' Weekly Pulse via the existing HorizonsWeeklyDocument fallback
    # path — zero frontend component change.
    monday = _iso_week_monday(iso_year, iso_week)
    doc, _ = Document.objects.update_or_create(
        tenant=tenant,
        kind=Document.Kind.WEEKLY,
        slug=str(monday),
        defaults={
            "title": f"Weekly Reflection — {iso_week_str}",
            "markdown": cleaned_text,
        },
    )
    result.document_id = str(doc.id)
    return result


# ── Helpers ───────────────────────────────────────────────────────────


def _already_ran_this_week(tenant: Tenant, iso_week_str: str) -> bool:
    """Has a weekly-reflection Document already been written for this ISO week?

    The Document slug encodes the ISO Monday so a re-run would update_or_create
    over the existing row — but we want to skip the LLM call entirely on a
    duplicate-fire of the hourly dispatcher. Cheap lookup.
    """
    iso_year_str, _, week_num_str = iso_week_str.partition("-W")
    try:
        iso_year = int(iso_year_str)
        week_num = int(week_num_str)
    except ValueError:
        return False
    monday = _iso_week_monday(iso_year, week_num)
    return Document.objects.filter(
        tenant=tenant,
        kind=Document.Kind.WEEKLY,
        slug=str(monday),
    ).exists()


def _voice_pref_silent(tenant: Tenant) -> bool:
    """``UserVoicePref.volume == SILENT`` at pillar scope skips proactive synthesis."""
    pref = UserVoicePref.objects.filter(
        tenant=tenant,
        pillar=Pillar.GRAVITY.value,
        topic__isnull=True,
    ).first()
    if pref is None:
        return False
    return pref.volume == UserVoicePref.Volume.SILENT


def _iso_week_monday(iso_year: int, iso_week: int) -> date:
    """Return the date of Monday for the given ISO (year, week)."""
    # %G %V %u is the ISO equivalent of %Y %W %w.
    return datetime.strptime(f"{iso_year}-{iso_week}-1", "%G-%V-%u").date()


def _build_context(tenant: Tenant, *, now: datetime) -> dict[str, Any]:
    """Gather the last week's Gravity signal for the synthesis prompt."""
    week_ago = now - timedelta(days=7)
    four_weeks_ago = now - timedelta(days=28)

    recent_snapshots = list(
        PillarSnapshot.objects.filter(
            tenant=tenant,
            pillar=Pillar.GRAVITY.value,
            ts__gte=four_weeks_ago,
        )
        .order_by("-ts")
        .values("ts", "payload")[:6]
    )
    recent_insights = list(
        AssistantInsight.objects.filter(
            tenant=tenant,
            pillar=Pillar.GRAVITY.value,
            status__in=[AssistantInsight.Status.OPEN, AssistantInsight.Status.CONFIRMED],
            created_at__gte=four_weeks_ago,
        )
        .order_by("-created_at")
        .select_related("topic")[:8]
    )
    pillar_pref = UserVoicePref.objects.filter(
        tenant=tenant,
        pillar=Pillar.GRAVITY.value,
        topic__isnull=True,
    ).first()

    has_data = bool(recent_snapshots) or bool(recent_insights)
    return {
        "has_data": has_data,
        "week_ago": week_ago.date().isoformat(),
        "now": now.date().isoformat(),
        "snapshots": recent_snapshots,
        "insights": [
            {
                "topic": ins.topic.display_name if ins.topic else "",
                "statement": ins.statement,
                "status": ins.status,
            }
            for ins in recent_insights
        ],
        "voice_pref": {
            "tone": pillar_pref.tone if pillar_pref else UserVoicePref.Tone.GENTLE.value,
            "volume": pillar_pref.volume if pillar_pref else UserVoicePref.Volume.WEEKLY.value,
        },
    }


def _format_context_for_prompt(context: dict[str, Any]) -> str:
    """Stringify the context dict for the user-message body of the LLM call."""
    lines: list[str] = []
    lines.append(f"Voice prefs: tone={context['voice_pref']['tone']} volume={context['voice_pref']['volume']}")
    lines.append(f"Reflection window: {context['week_ago']} through {context['now']}")
    if context["snapshots"]:
        lines.append("\nRecent weekly snapshots (most recent first):")
        for snap in context["snapshots"]:
            ts = snap["ts"].date().isoformat() if hasattr(snap["ts"], "date") else str(snap["ts"])
            totals = (snap.get("payload") or {}).get("totals", {})
            lines.append(f"- {ts}: {totals}")
    if context["insights"]:
        lines.append("\nOpen/confirmed insights from the past few weeks (for context, do not repeat):")
        for ins in context["insights"]:
            lines.append(f"- ({ins['status']}, {ins['topic']}): {ins['statement']}")
    blob = "\n".join(lines)
    if len(blob) > _MAX_INPUT_CONTEXT_CHARS:
        blob = blob[:_MAX_INPUT_CONTEXT_CHARS] + "\n…(truncated)"
    return blob


def _call_synthesis_llm(context: dict[str, Any]) -> tuple[str, dict]:
    """Call OpenRouter for the synthesis. Returns (text, usage_dict).

    Tries DeepSeek first, then MiniMax (see SYNTHESIS_MODELS) so a single-model
    OpenRouter outage doesn't silently drop the weekly reflection.
    """
    user_message = _format_context_for_prompt(context)
    data, _model_used = chat_completion(
        SYNTHESIS_MODELS,
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        max_tokens=_MAX_OUTPUT_TOKENS,
        temperature=0.4,
        timeout=45,
    )
    text = (data["choices"][0]["message"]["content"] or "").strip()
    usage = data.get("usage", {}) or {}
    return text, usage
