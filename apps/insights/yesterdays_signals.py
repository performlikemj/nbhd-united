"""Cross-pillar yesterday's-signals roll-up.

A day-scoped snapshot across Fuel / Journal / Lessons used by the
``nbhd_yesterdays_signals`` tool. The Personal Question and Heartbeat
cron prompts call the tool to ground their asking / nudge decisions in
recent activity rather than only memory + recent notes.

This is intentionally distinct from ``apps.insights.signals`` (Phase 3
register signals: per-pillar, per-topic, window-scoped). This module is
cross-pillar, day-scoped, no topic.

Design notes:
- Backend returns raw evidence + cheap threshold flags (``notable_gaps``).
  The LLM weighs them. See ``feedback_llm_not_formula_for_judgment``.
- Core pillar included (MeditationSession) — its data model landed with the
  Core mindfulness pillar.
- Gravity (finance) is included only when ``tenant.finance_active`` and only as
  counts (no amounts) — the authoritative kill switch stays honored and no
  finance detail leaks into a cross-pillar prompt.
- Tenant-tz-aware "yesterday" so a workout logged at 11pm local doesn't
  fall on the wrong day after the UTC roll-over.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, time, timedelta
from typing import Any

from django.utils import timezone

from apps.fuel.models import Workout, WorkoutStatus
from apps.journal.models import JournalEntry
from apps.lessons.models import Lesson
from apps.tenants.models import Tenant

NOTABLE_JOURNAL_DARK_DAYS = 3
NOTABLE_FUEL_QUIET_DAYS = 5
NOTABLE_ENERGY_STALE_DAYS = 7
NOTABLE_CORE_QUIET_DAYS = 5


def _tenant_tz(tenant: Tenant) -> zoneinfo.ZoneInfo:
    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
    try:
        return zoneinfo.ZoneInfo(user_tz)
    except Exception:
        return zoneinfo.ZoneInfo("UTC")


def _local_day_bounds(day: date, tz: zoneinfo.ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def compute(tenant: Tenant, *, now: datetime | None = None) -> dict[str, Any]:
    """Cross-pillar snapshot of yesterday's activity in tenant-local time.

    Returns a JSON-serialisable dict. Pass ``now`` in tests to make the
    "yesterday" anchor deterministic; production callers should omit it.
    """
    tz = _tenant_tz(tenant)
    now_local = (now or timezone.now()).astimezone(tz)
    today = now_local.date()
    yesterday = today - timedelta(days=1)

    fuel = _fuel_signals(tenant, today=today, yesterday=yesterday, tz=tz)
    journal = _journal_signals(tenant, today=today, yesterday=yesterday)
    lessons = _lessons_signals(tenant, yesterday=yesterday, tz=tz)
    core = _core_signals(tenant, today=today, yesterday=yesterday)

    notable_gaps: list[str] = []
    if journal["days_since_last_entry"] is not None and journal["days_since_last_entry"] >= NOTABLE_JOURNAL_DARK_DAYS:
        notable_gaps.append(f"journal_dark_{journal['days_since_last_entry']}_days")
    if fuel["days_since_last_workout"] is not None and fuel["days_since_last_workout"] >= NOTABLE_FUEL_QUIET_DAYS:
        notable_gaps.append(f"fuel_quiet_{fuel['days_since_last_workout']}_days")
    last_energy = journal.get("last_energy_reading")
    if last_energy and last_energy["days_ago"] >= NOTABLE_ENERGY_STALE_DAYS:
        notable_gaps.append(f"energy_stale_{last_energy['days_ago']}_days")
    if core["days_since_last_session"] is not None and core["days_since_last_session"] >= NOTABLE_CORE_QUIET_DAYS:
        notable_gaps.append(f"core_quiet_{core['days_since_last_session']}_days")

    result = {
        "as_of": now_local.isoformat(),
        "today_date": today.isoformat(),
        "yesterday_date": yesterday.isoformat(),
        "fuel": fuel,
        "journal": journal,
        "lessons": lessons,
        "core": core,
        "notable_gaps": notable_gaps,
    }

    # Gravity is opt-in and privacy-gated: only surface it when the kill switch
    # is on, and only as counts (never amounts) so no finance detail leaks.
    if getattr(tenant, "finance_active", False):
        result["gravity"] = _gravity_signals(tenant, today=today, yesterday=yesterday)

    return result


def _fuel_signals(tenant: Tenant, *, today: date, yesterday: date, tz: zoneinfo.ZoneInfo) -> dict[str, Any]:
    workouts = Workout.objects.filter(tenant=tenant)

    workouts_yesterday_done = workouts.filter(date=yesterday, status=WorkoutStatus.DONE).count()
    workouts_today_done = workouts.filter(date=today, status=WorkoutStatus.DONE).count()

    last_done_date = (
        workouts.filter(status=WorkoutStatus.DONE, date__lt=today)
        .order_by("-date")
        .values_list("date", flat=True)
        .first()
    )
    days_since_last = (today - last_done_date).days if last_done_date else None

    return {
        "yesterday": {"workouts_done": workouts_yesterday_done},
        "today_so_far": {"workouts_done": workouts_today_done},
        "days_since_last_workout": days_since_last,
    }


def _journal_signals(tenant: Tenant, *, today: date, yesterday: date) -> dict[str, Any]:
    entries = JournalEntry.objects.filter(tenant=tenant)

    yesterday_entries = entries.filter(date=yesterday)
    yesterday_count = yesterday_entries.count()
    yesterday_energy = yesterday_entries.order_by("-created_at").values_list("energy", flat=True).first()

    last_entry_date = entries.filter(date__lt=today).order_by("-date").values_list("date", flat=True).first()
    days_since_last = (today - last_entry_date).days if last_entry_date else None

    last_energy_entry = entries.exclude(energy="").order_by("-date", "-created_at").first()
    last_energy_reading: dict[str, Any] | None = None
    if last_energy_entry:
        last_energy_reading = {
            "value": last_energy_entry.energy,
            "days_ago": (today - last_energy_entry.date).days,
        }

    # Typed Goal / Task lifecycle — active goals the user is steering toward and
    # what they closed out yesterday. Local import per feedback_local_reimport_pattern.
    from apps.journal.models import Goal, Task

    active_goals = Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE).count()
    tasks_completed_yesterday = Task.objects.filter(
        tenant=tenant,
        status=Task.Status.DONE,
        completed_at__date=yesterday,
    ).count()

    return {
        "yesterday": {
            "entries": yesterday_count,
            "energy": yesterday_energy,
            "tasks_completed": tasks_completed_yesterday,
        },
        "days_since_last_entry": days_since_last,
        "last_energy_reading": last_energy_reading,
        "active_goals": active_goals,
    }


def _core_signals(tenant: Tenant, *, today: date, yesterday: date) -> dict[str, Any]:
    """Core (mindfulness) day-scoped signals — completed sits by ``date``.

    Local import per feedback_local_reimport_pattern.
    """
    from apps.core.models import MeditationSession, MeditationStatus

    done_states = [MeditationStatus.READY, MeditationStatus.DELIVERED]
    sessions = MeditationSession.objects.filter(tenant=tenant, status__in=done_states)

    sessions_yesterday = sessions.filter(date=yesterday).count()
    sessions_today = sessions.filter(date=today).count()

    last_date = sessions.filter(date__lt=today).order_by("-date").values_list("date", flat=True).first()
    days_since_last = (today - last_date).days if last_date else None

    return {
        "yesterday": {"sessions": sessions_yesterday},
        "today_so_far": {"sessions": sessions_today},
        "days_since_last_session": days_since_last,
    }


def _gravity_signals(tenant: Tenant, *, today: date, yesterday: date) -> dict[str, Any]:
    """Gravity (finance) day-scoped signals — COUNTS ONLY, never amounts.

    Only ever called when ``tenant.finance_active`` is True (see ``compute``).
    Local import per feedback_local_reimport_pattern.
    """
    from apps.finance.models import FinanceTransaction

    txns = FinanceTransaction.objects.filter(tenant=tenant)
    txns_yesterday = txns.filter(date=yesterday).count()

    last_date = txns.filter(date__lt=today).order_by("-date").values_list("date", flat=True).first()
    days_since_last = (today - last_date).days if last_date else None

    return {
        "yesterday": {"transactions": txns_yesterday},
        "days_since_last_transaction": days_since_last,
    }


def _lessons_signals(tenant: Tenant, *, yesterday: date, tz: zoneinfo.ZoneInfo) -> dict[str, Any]:
    yesterday_start, yesterday_end = _local_day_bounds(yesterday, tz)
    lessons = Lesson.objects.filter(tenant=tenant)

    yesterday_approved = lessons.filter(
        status="approved",
        approved_at__gte=yesterday_start,
        approved_at__lt=yesterday_end,
    ).count()
    pending = lessons.filter(status="pending").count()

    return {
        "yesterday": {"approved": yesterday_approved},
        "pending": pending,
    }
