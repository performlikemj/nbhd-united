"""USER.md ``Agenda — Open threads`` envelope section.

Phase A of the agenda-aware assistant arc.

The agenda is a *meta-view* over the existing thread-shaped primitives —
tasks, goals, planned workouts, active financial plans, plus the
"feature was enabled but the user hasn't engaged with it yet" implicit
threads. The detail of each thread lives in its own pillar section; the
agenda renders an at-a-glance index plus the threads that no other
section knows about (the untouched intros), framed by guidance about
how the agent should use the view.

This section is deliberately thin:

- **Counts** of open work across pillars, so the agent has a single
  synthesis line instead of having to integrate across five sections.
- **Untouched introductions** — features the user enabled but never
  engaged with (``feature_enabled AND welcomes_sent[feature] is null``).
  Today's welcome cron is the only path that surfaces these, and it's
  fragile (date-pattern recurrence, model-specific callbacks). With the
  agenda visible to every turn, *any* proactive cron — Morning Briefing,
  Heartbeat, Evening Check-in — can naturally weave in an introduction
  when the moment fits.
- **Guidance** for the agent: read first, weave selectively, never
  enumerate. The agenda is a tool for choice, not a script.

What's *not* here in Phase A:

- Engagement-aware priority (last_surfaced_at, response_signals). That's
  Phase B; it requires schema additions on the thread-shaped tables and
  a post-turn extractor that infers engagement from agent transcripts.
- Cross-domain inference ("user mentioned money stress in their journal,
  Gravity intro is dormant — boost it"). Phase C.
- Future-aware commitments ("ask about debt in 2 weeks"). Phase D.

Each phase ships value standalone. Phase A is the foundation everything
else stacks on.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta as _timedelta

from apps.finance.models import PayoffPlan
from apps.fuel.models import FuelGoal, Workout, WorkoutStatus
from apps.journal.models import Document
from apps.orchestrator.envelope_registry import register_section
from apps.tenants.agenda_models import AgendaEngagement
from apps.tenants.models import Tenant

# Features tracked under ``Tenant.welcomes_sent``. Each entry maps the
# JSON key to a human label used in the rendered untouched-intro line.
# Adding a new feature = one tuple here; the renderer picks it up.
_FEATURE_INTROS: tuple[tuple[str, str, str], ...] = (
    # (welcomes_sent key, feature_enabled attr, label rendered to the agent)
    ("fuel", "fuel_enabled", "Fuel — fitness assistant"),
    ("finance", "finance_enabled", "Gravity — finance assistant"),
)

# Engagement kind used when reading AgendaEngagement rows for feature
# intros. Stored as a constant so the renderer + service helpers stay
# in lock-step on the kind string.
_FEATURE_INTRO_KIND = AgendaEngagement.Kind.FEATURE_INTRO


_HEADER_GUIDANCE = (
    "_Read this before composing any proactive turn. Surface what fits "
    "the moment; ignore what doesn't. **Don't enumerate the agenda to "
    "the user** — choose at most one or two threads, and only if they "
    "connect naturally to today's tone, the journal, or the prescribed "
    "task. If nothing fits, skip._"
)


@register_section(
    key="agenda",
    heading="## Agenda — Open threads with this user",
    enabled=lambda t: True,
    refresh_on=(Document, Workout, FuelGoal, PayoffPlan, Tenant),
    order=15,  # right after profile (10), before goals (20)
)
def render_agenda(tenant: Tenant) -> str:
    """Render the agenda meta-view: synthesis + untouched intros + guidance.

    Returns empty string when there's literally nothing on the agenda
    (no open work, no untouched intros). The registry skips empty
    sections automatically.
    """
    parts: list[str] = [_HEADER_GUIDANCE]

    summary = _render_summary(tenant)
    if summary:
        parts.append(summary)

    intros = _render_untouched_intros(tenant)
    if intros:
        parts.append(intros)

    # All-empty state is rare (a tenant with zero open work and every
    # feature already engaged) — render nothing rather than just the
    # header. Saves USER.md bytes for power users.
    if len(parts) == 1:
        return ""

    return "\n\n".join(parts)


def _render_summary(tenant: Tenant) -> str:
    """One-line synthesis of open work counts across pillars.

    Skips zero-counts so the line stays compact. Returns empty string
    when nothing is open.
    """
    counts: list[str] = []

    open_tasks = _count_open_tasks(tenant)
    if open_tasks:
        counts.append(f"**{open_tasks}** open task{'s' if open_tasks != 1 else ''}")

    active_goals = _count_active_goals(tenant)
    if active_goals:
        counts.append(f"**{active_goals}** active goal{'s' if active_goals != 1 else ''}")

    planned_workouts = _count_planned_workouts(tenant, days=7)
    if planned_workouts:
        counts.append(f"**{planned_workouts}** planned workout{'s' if planned_workouts != 1 else ''} (next 7d)")

    active_plans = _count_active_payoff_plans(tenant)
    if active_plans:
        counts.append(f"**{active_plans}** active payoff plan{'s' if active_plans != 1 else ''}")

    if not counts:
        return ""

    return "**Open work:** " + " · ".join(counts) + "."


def _render_untouched_intros(tenant: Tenant) -> str:
    """Features enabled but never engaged with — eligibility-filtered.

    Phase A surfaced any feature whose ``welcomes_sent[key]`` was unset.
    Phase B layers ``AgendaEngagement`` checks on top: a feature whose
    engagement row is in ``ABANDONED`` / ``COMPLETED``, was surfaced
    within the recent cooldown window, or has ``surface_after`` set in
    the future, drops out of the rendered list.

    The combination of both filters is the right thing — ``welcomes_sent``
    is set when delivery succeeds (the platform's source of truth for
    "we did the welcome"), and engagement state captures more nuanced
    signals (a tenant who hits "abandon" on the welcome flow shouldn't
    keep seeing it).
    """
    from apps.tenants.agenda_service import engagements_by_item, is_eligible_now

    welcomes_sent = tenant.welcomes_sent or {}
    engagements = engagements_by_item(tenant, kind=_FEATURE_INTRO_KIND)
    lines: list[str] = []

    for key, enabled_attr, label in _FEATURE_INTROS:
        if not getattr(tenant, enabled_attr, False):
            continue
        if welcomes_sent.get(key):
            continue
        if not is_eligible_now(engagements.get(key)):
            continue
        lines.append(f"- **{label}** — enabled, no engagement yet. Open thread for an organic introduction.")

    if not lines:
        return ""

    return "**Untouched introductions:**\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Counters — small enough to inline; keeping them as functions so they're
# unit-testable and the main render stays declarative.
# ---------------------------------------------------------------------------


def _count_open_tasks(tenant: Tenant) -> int:
    """Open ``- [ ]`` items in the tasks doc.

    Mirrors the filter logic in ``apps.journal.envelope.render_open_tasks``
    so the agenda count and the tasks-pillar list stay coherent. We don't
    import the renderer to avoid circular imports — same parsing rules.
    """
    doc = Document.objects.filter(
        tenant=tenant,
        kind=Document.Kind.TASKS,
        slug="tasks",
    ).first()
    if not doc:
        return 0

    starter_lines = _starter_task_lines()
    return sum(
        1
        for line in (doc.markdown or "").splitlines()
        if line.lstrip().startswith("- [ ]") and line.strip() not in starter_lines
    )


def _count_active_goals(tenant: Tenant) -> int:
    """Active goals across the journal goals doc + FuelGoal records.

    A non-empty goals doc counts as one (it's a single curated document,
    so we treat the *thread* — engaging with goals — as 0 or 1). Each
    FuelGoal without ``achieved_at`` adds one.
    """
    count = 0

    goals_doc = Document.objects.filter(
        tenant=tenant,
        kind=Document.Kind.GOAL,
        slug="goals",
    ).first()
    if goals_doc and (goals_doc.markdown or "").strip():
        starter = _starter_goals_markdown().strip()
        body = (goals_doc.markdown or "").strip()
        if body and body != starter:
            count += 1

    count += FuelGoal.objects.filter(tenant=tenant, achieved_at__isnull=True).count()

    return count


def _count_planned_workouts(tenant: Tenant, *, days: int) -> int:
    """Planned workouts in the next ``days`` window."""
    today = _date.today()
    horizon = today + _timedelta(days=days)
    return Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.PLANNED,
        date__gte=today,
        date__lte=horizon,
    ).count()


def _count_active_payoff_plans(tenant: Tenant) -> int:
    return PayoffPlan.objects.filter(tenant=tenant, is_active=True).count()


# ---------------------------------------------------------------------------
# Starter-doc detection — copied from journal.envelope to avoid a circular
# import at module load. The journal app imports orchestrator.envelope_registry
# at boot; we'd loop if we imported back the other way during decoration.
# ---------------------------------------------------------------------------

_STARTER_CACHE: dict[str, str] = {}


def _starter_markdown(slug: str) -> str:
    if not _STARTER_CACHE:
        from apps.journal.services import STARTER_DOCUMENT_TEMPLATES

        _STARTER_CACHE.update({t["slug"]: t["markdown"] for t in STARTER_DOCUMENT_TEMPLATES})
    return _STARTER_CACHE.get(slug, "")


def _starter_task_lines() -> frozenset[str]:
    seed = _starter_markdown("tasks")
    return frozenset(line.strip() for line in seed.splitlines() if line.lstrip().startswith("- [ ]"))


def _starter_goals_markdown() -> str:
    return _starter_markdown("goals")
