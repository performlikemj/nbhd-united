"""Structured enumeration of open agenda threads (Phase C).

Phase A's renderer aggregates pillar state into a human-readable
markdown block. Phase C needs the *same* data in a structured form
so the cross-domain inference classifier can ask "did the user's
journal mention any of these threads, and how?"

This module is the single source of "what threads are eligible right
now" — used by:

- The agenda renderer (will adopt this in a future cleanup) for the
  authoritative open-threads list
- The Phase C agenda-hint extractor in ``apps.journal.agenda_hints``
- The Phase D commitment surfacing logic
- The audit pass that will fold the welcome cron into commitments

Returns ``ThreadSpec`` records with stable identifiers — the same
``(kind, item_id)`` shape that ``AgendaEngagement`` keys on, so any
classifier output maps cleanly back to engagement rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta as _timedelta
from typing import TYPE_CHECKING

from apps.tenants.agenda_models import AgendaEngagement
from apps.tenants.agenda_service import is_eligible_now

if TYPE_CHECKING:
    from apps.tenants.models import Tenant


@dataclass(frozen=True)
class ThreadSpec:
    """A structured representation of one open agenda thread.

    ``kind`` matches ``AgendaEngagement.Kind`` values. ``item_id`` is
    the thread's stable identifier — feature key, UUID, content hash.
    ``label`` is the human-readable handle the LLM classifier compares
    against journal text. ``context`` is optional supporting detail
    (next workout date, payoff strategy, etc.) that helps the LLM
    distinguish between similar threads.
    """

    kind: str
    item_id: str
    label: str
    context: str = ""


# Mirror of ``agenda_envelope._FEATURE_INTROS``. Kept in sync manually
# (small list, low churn) to avoid coupling the renderer's private
# constant to this public-facing helper.
_FEATURE_INTROS: tuple[tuple[str, str, str], ...] = (
    ("fuel", "fuel_enabled", "Fuel — fitness assistant"),
    ("finance", "finance_enabled", "Gravity — finance assistant"),
)


def open_threads(tenant: Tenant) -> list[ThreadSpec]:
    """Enumerate eligible open threads for a tenant.

    Honors the same filters as the renderer: untouched feature intros
    only when ``welcomes_sent[key]`` is null AND ``is_eligible_now``
    passes; planned workouts within the next 7 days; active fuel goals
    and payoff plans. Classifier-facing — never raises; on a per-thread
    error returns the partial list (degrade gracefully so a bad row
    doesn't kill the whole hint pass).
    """
    threads: list[ThreadSpec] = []
    threads.extend(_feature_intros(tenant))
    threads.extend(_planned_workouts(tenant))
    threads.extend(_fuel_goals(tenant))
    threads.extend(_payoff_plans(tenant))
    return threads


def _feature_intros(tenant: Tenant) -> list[ThreadSpec]:
    welcomes_sent = tenant.welcomes_sent or {}
    engagements = {
        e.item_id: e
        for e in AgendaEngagement.objects.filter(
            tenant=tenant,
            kind=AgendaEngagement.Kind.FEATURE_INTRO,
        )
    }

    out: list[ThreadSpec] = []
    for key, enabled_attr, label in _FEATURE_INTROS:
        if not getattr(tenant, enabled_attr, False):
            continue
        if welcomes_sent.get(key):
            continue
        if not is_eligible_now(engagements.get(key)):
            continue
        out.append(
            ThreadSpec(
                kind=AgendaEngagement.Kind.FEATURE_INTRO,
                item_id=key,
                label=label,
                context="enabled but no engagement yet",
            )
        )
    return out


def _planned_workouts(tenant: Tenant) -> list[ThreadSpec]:
    from apps.fuel.models import Workout, WorkoutStatus

    today = _date.today()
    horizon = today + _timedelta(days=7)
    qs = Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.PLANNED,
        date__gte=today,
        date__lte=horizon,
    ).order_by("date", "scheduled_at")[:10]

    engagements = {
        e.item_id: e
        for e in AgendaEngagement.objects.filter(
            tenant=tenant,
            kind=AgendaEngagement.Kind.PLANNED_WORKOUT,
        )
    }

    out: list[ThreadSpec] = []
    for w in qs:
        item_id = str(w.id)
        if not is_eligible_now(engagements.get(item_id)):
            continue
        when = w.scheduled_at.isoformat() if w.scheduled_at else w.date.isoformat()
        label = f"{w.activity or w.category} on {w.date.isoformat()}"
        out.append(
            ThreadSpec(
                kind=AgendaEngagement.Kind.PLANNED_WORKOUT,
                item_id=item_id,
                label=label,
                context=f"category={w.category}, scheduled={when}",
            )
        )
    return out


def _fuel_goals(tenant: Tenant) -> list[ThreadSpec]:
    from apps.fuel.models import FuelGoal

    qs = FuelGoal.objects.filter(tenant=tenant, achieved_at__isnull=True).order_by("-created_at")[:10]
    engagements = {
        e.item_id: e
        for e in AgendaEngagement.objects.filter(
            tenant=tenant,
            kind=AgendaEngagement.Kind.FUEL_GOAL,
        )
    }

    out: list[ThreadSpec] = []
    for g in qs:
        item_id = str(g.id)
        if not is_eligible_now(engagements.get(item_id)):
            continue
        target = ""
        if g.target_value:
            target = f" target {g.target_value} {g.metric}"
        if g.target_date:
            target += f" by {g.target_date.isoformat()}"
        out.append(
            ThreadSpec(
                kind=AgendaEngagement.Kind.FUEL_GOAL,
                item_id=item_id,
                label=f"{g.exercise_name}{target}".strip(),
                context="fuel goal in progress",
            )
        )
    return out


def _payoff_plans(tenant: Tenant) -> list[ThreadSpec]:
    from apps.finance.models import PayoffPlan

    qs = PayoffPlan.objects.filter(tenant=tenant, is_active=True).order_by("-created_at")[:5]
    engagements = {
        e.item_id: e
        for e in AgendaEngagement.objects.filter(
            tenant=tenant,
            kind=AgendaEngagement.Kind.PAYOFF_PLAN,
        )
    }

    out: list[ThreadSpec] = []
    for p in qs:
        item_id = str(p.id)
        if not is_eligible_now(engagements.get(item_id)):
            continue
        label = f"{p.strategy} payoff plan"
        ctx_bits = []
        if p.total_debt:
            ctx_bits.append(f"${p.total_debt} debt")
        if p.payoff_date:
            ctx_bits.append(f"target {p.payoff_date.isoformat()}")
        out.append(
            ThreadSpec(
                kind=AgendaEngagement.Kind.PAYOFF_PLAN,
                item_id=item_id,
                label=label,
                context=", ".join(ctx_bits),
            )
        )
    return out
