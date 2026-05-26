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
- Core pillar omitted: no data model yet.
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

    notable_gaps: list[str] = []
    if journal["days_since_last_entry"] is not None and journal["days_since_last_entry"] >= NOTABLE_JOURNAL_DARK_DAYS:
        notable_gaps.append(f"journal_dark_{journal['days_since_last_entry']}_days")
    if fuel["days_since_last_workout"] is not None and fuel["days_since_last_workout"] >= NOTABLE_FUEL_QUIET_DAYS:
        notable_gaps.append(f"fuel_quiet_{fuel['days_since_last_workout']}_days")
    last_energy = journal.get("last_energy_reading")
    if last_energy and last_energy["days_ago"] >= NOTABLE_ENERGY_STALE_DAYS:
        notable_gaps.append(f"energy_stale_{last_energy['days_ago']}_days")

    return {
        "as_of": now_local.isoformat(),
        "today_date": today.isoformat(),
        "yesterday_date": yesterday.isoformat(),
        "fuel": fuel,
        "journal": journal,
        "lessons": lessons,
        "notable_gaps": notable_gaps,
    }


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

    return {
        "yesterday": {
            "entries": yesterday_count,
            "energy": yesterday_energy,
        },
        "days_since_last_entry": days_since_last,
        "last_energy_reading": last_energy_reading,
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
