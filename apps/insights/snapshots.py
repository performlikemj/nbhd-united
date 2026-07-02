"""Per-pillar snapshot computation for the assistant baseline.

Snapshot functions return a JSON-serializable dict that lands in
``PillarSnapshot.payload``. The shape mirrors what the corresponding pillar
tab renders today, so the assistant reasoning over snapshots is reasoning over
the same surface the user sees.

``compute_gravity_snapshot`` (account-level finance trajectory) shipped first;
``compute_fuel_snapshot`` / ``compute_core_snapshot`` / ``compute_journal_snapshot``
extend the same append-only time series to the other pillars so the assistant's
weekly-history / compare / synthesis machinery is no longer finance-only.

Every payload carries a ``totals`` block of flat numeric values (parallel to
Gravity's) so a future ``TOPIC_EXTRACTORS`` entry can read a baseline straight
out of the snapshot without bespoke parsing.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Sum

from apps.common.tenant_tz import tenant_today
from apps.core.models import MeditationSession, MeditationStatus
from apps.finance.models import FinanceAccount, FinanceTransaction, PayoffPlan
from apps.fuel.models import BodyWeightLog, PlanStatus, Workout, WorkoutPlan, WorkoutStatus
from apps.journal.models import Goal, JournalEntry, Task
from apps.tenants.models import Tenant

SCHEMA_VERSION = 1

# Core sessions in these states count as a completed sit (the audio was made
# and either delivered or is ready to play). Pending / rendering / failed are
# not practice.
_CORE_DONE_STATES = (MeditationStatus.READY, MeditationStatus.DELIVERED)


def _money(value: Decimal | None) -> str:
    """Serialize a Decimal to a string for JSON storage. ``None`` becomes ``"0"``."""
    return str(value if value is not None else Decimal("0"))


def _window_starts(today: date) -> tuple[date, date]:
    """Return (start_7d, start_28d) inclusive lower bounds ending on ``today``."""
    return today - timedelta(days=6), today - timedelta(days=27)


def _practice_streak(session_dates: set[date], *, today: date) -> int:
    """Consecutive-day practice streak ending today (or yesterday — today's day
    isn't over yet, so a sit yesterday still counts as a live streak).

    Returns 0 when neither today nor yesterday has a completed session.
    """
    if today in session_dates:
        cursor = today
    elif (today - timedelta(days=1)) in session_dates:
        cursor = today - timedelta(days=1)
    else:
        return 0
    streak = 0
    while cursor in session_dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def compute_fuel_snapshot(tenant: Tenant, *, today: date | None = None) -> dict[str, Any]:
    """Compute the Fuel (workout) snapshot payload for a tenant.

    Volume (sessions + minutes over 7d / 28d), current active-plan adherence,
    and body-weight trajectory. Windows use the tenant's LOCAL day so a late
    JST workout lands in the right week. Pass ``today`` in tests for
    determinism; production omits it.
    """
    today = today or tenant_today(tenant)
    start_7, start_28 = _window_starts(today)

    done = Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.DONE,
        date__gte=start_28,
        date__lte=today,
    )

    def _vol(qs) -> tuple[int, int]:
        agg = qs.aggregate(n=Count("id"), mins=Sum("duration_minutes"))
        return int(agg["n"] or 0), int(agg["mins"] or 0)

    workouts_28d, minutes_28d = _vol(done)
    workouts_7d, minutes_7d = _vol(done.filter(date__gte=start_7))

    # Active-plan adherence over the current 7-day window (cheap: one count of
    # non-rest scheduled slots, one of completed). None when no active plan or
    # nothing scheduled this week.
    active_plan = WorkoutPlan.objects.filter(tenant=tenant, status=PlanStatus.ACTIVE).order_by("-start_date").first()
    plan_block: dict[str, Any] | None = None
    if active_plan is not None:
        scheduled_qs = Workout.objects.filter(
            tenant=tenant,
            plan=active_plan,
            date__gte=start_7,
            date__lte=today,
        ).exclude(status=WorkoutStatus.REST)
        scheduled = scheduled_qs.count()
        completed = scheduled_qs.filter(status=WorkoutStatus.DONE).count()
        plan_block = {
            "name": active_plan.name,
            "scheduled_this_week": scheduled,
            "completed_this_week": completed,
            "adherence": round(completed / scheduled, 4) if scheduled else None,
        }

    # Body-weight latest + 28-day delta (latest minus oldest in window).
    weights = list(BodyWeightLog.objects.filter(tenant=tenant, date__gte=start_28, date__lte=today).order_by("date"))
    latest_weight = weights[-1].weight_kg if weights else None
    weight_delta = (weights[-1].weight_kg - weights[0].weight_kg) if len(weights) >= 2 else None

    return {
        "schema_version": SCHEMA_VERSION,
        "totals": {
            "workouts_7d": workouts_7d,
            "workouts_28d": workouts_28d,
            "minutes_7d": minutes_7d,
            "minutes_28d": minutes_28d,
            "body_weight_kg": _money(latest_weight) if latest_weight is not None else None,
            "body_weight_delta_28d": _money(weight_delta) if weight_delta is not None else None,
        },
        "active_plan": plan_block,
        "window": {"today": today.isoformat(), "start_7d": start_7.isoformat(), "start_28d": start_28.isoformat()},
    }


def compute_core_snapshot(tenant: Tenant, *, today: date | None = None) -> dict[str, Any]:
    """Compute the Core (mindfulness) snapshot payload for a tenant.

    Completed sessions over 7d / 28d and the current consecutive-day practice
    streak. Tenant-local day boundaries.
    """
    today = today or tenant_today(tenant)
    start_7, start_28 = _window_starts(today)

    done = MeditationSession.objects.filter(
        tenant=tenant,
        status__in=_CORE_DONE_STATES,
        date__lte=today,
    )
    sessions_28d = done.filter(date__gte=start_28).count()
    sessions_7d = done.filter(date__gte=start_7).count()

    # Streak: look back far enough to catch a long run, but bound the scan.
    streak_dates = set(done.filter(date__gte=today - timedelta(days=365)).values_list("date", flat=True))
    streak = _practice_streak(streak_dates, today=today)
    last_session_date = done.order_by("-date").values_list("date", flat=True).first()

    return {
        "schema_version": SCHEMA_VERSION,
        "totals": {
            "sessions_7d": sessions_7d,
            "sessions_28d": sessions_28d,
            "practice_streak_days": streak,
        },
        "last_session_date": last_session_date.isoformat() if last_session_date else None,
        "window": {"today": today.isoformat(), "start_7d": start_7.isoformat(), "start_28d": start_28.isoformat()},
    }


def compute_journal_snapshot(tenant: Tenant, *, today: date | None = None) -> dict[str, Any]:
    """Compute the Journal snapshot payload for a tenant.

    Entries over 7d / 28d, count of currently-active goals, and tasks completed
    in the last 7 days. Journal is always-on (no per-tenant enable flag), so
    this snapshot writes for every active tenant.
    """
    today = today or tenant_today(tenant)
    start_7, start_28 = _window_starts(today)

    entries = JournalEntry.objects.filter(tenant=tenant, date__lte=today)
    entries_28d = entries.filter(date__gte=start_28).count()
    entries_7d = entries.filter(date__gte=start_7).count()

    active_goals = Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE).count()
    tasks_open = Task.objects.filter(tenant=tenant, status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS]).count()
    tasks_completed_7d = Task.objects.filter(
        tenant=tenant,
        status=Task.Status.DONE,
        completed_at__date__gte=start_7,
        completed_at__date__lte=today,
    ).count()

    last_entry_date = entries.order_by("-date").values_list("date", flat=True).first()

    return {
        "schema_version": SCHEMA_VERSION,
        "totals": {
            "entries_7d": entries_7d,
            "entries_28d": entries_28d,
            "active_goals": active_goals,
            "tasks_open": tasks_open,
            "tasks_completed_7d": tasks_completed_7d,
        },
        "last_entry_date": last_entry_date.isoformat() if last_entry_date else None,
        "window": {"today": today.isoformat(), "start_7d": start_7.isoformat(), "start_28d": start_28.isoformat()},
    }


def compute_gravity_snapshot(tenant: Tenant) -> dict[str, Any]:
    """Compute the Gravity (finance) snapshot payload for a tenant.

    Mirrors the shape ``FinanceDashboardView`` returns; keep the two in sync.
    Returns a serializable dict suitable for ``PillarSnapshot.payload``.
    """
    accounts = list(FinanceAccount.objects.filter(tenant=tenant, is_active=True))
    debt_types = FinanceAccount.DEBT_TYPES
    debt_accounts = [a for a in accounts if a.account_type in debt_types]
    savings_accounts = [a for a in accounts if a.account_type not in debt_types]

    total_debt = sum((a.current_balance for a in debt_accounts), Decimal("0"))
    total_savings = sum((a.current_balance for a in savings_accounts), Decimal("0"))
    total_minimums = sum(
        (a.minimum_payment for a in debt_accounts if a.minimum_payment),
        Decimal("0"),
    )

    active_plan = PayoffPlan.objects.filter(tenant=tenant, is_active=True).first()

    recent_transactions = list(
        FinanceTransaction.objects.filter(tenant=tenant).select_related("account").order_by("-date", "-created_at")[:10]
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "totals": {
            "debt": _money(total_debt),
            "savings": _money(total_savings),
            "minimum_payments": _money(total_minimums),
        },
        "account_counts": {
            "debt": len(debt_accounts),
            "savings": len(savings_accounts),
        },
        "accounts": [
            {
                "id": str(a.id),
                "type": a.account_type,
                "nickname": a.nickname,
                "current_balance": _money(a.current_balance),
                "original_balance": _money(a.original_balance),
                "is_debt": a.account_type in debt_types,
                "payoff_progress": float(a.payoff_progress) if a.payoff_progress is not None else None,
            }
            for a in accounts
        ],
        "active_plan": (
            {
                "strategy": active_plan.strategy,
                "monthly_budget": _money(active_plan.monthly_budget),
                "total_debt": _money(active_plan.total_debt),
                "total_interest": _money(active_plan.total_interest),
                "payoff_months": active_plan.payoff_months,
                "payoff_date": active_plan.payoff_date.isoformat() if active_plan.payoff_date else None,
            }
            if active_plan
            else None
        ),
        "recent_transactions": [
            {
                "type": t.transaction_type,
                "amount": _money(t.amount),
                "date": t.date.isoformat(),
                "account_nickname": t.account.nickname if t.account_id else None,
                "description": t.description or "",
            }
            for t in recent_transactions
        ],
    }
