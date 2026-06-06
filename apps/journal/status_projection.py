"""Read-model projection for the Journal "current status" surface.

The journal page must show *current* state, not a baked copy. For typed
lifecycle objects (``Task``, ``Goal``) current state is the row's status.
For a *recurring* obligation (e.g. a monthly loan payment) there is no
single "done" flag that stays true — the truth of "is this period paid?"
is a projection over the dated ``FinanceTransaction`` event stream. This
module folds those streams into an as-of-now snapshot.

The pure helpers (``current_period_bounds``, ``effective_due_date``,
``obligation_for_account``) are date-injectable, so the recurrence logic
is unit-testable without depending on the wall clock.
``build_journal_status`` runs the queries and assembles the response dict.

Imports of finance + ORM symbols are LOCAL to the assembler to avoid any
app-load import cycle (journal ↔ finance); the pure helpers reference
``FinanceAccount`` only in string annotations (``from __future__``).
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from .status_registry import register_status_provider, status_providers

if TYPE_CHECKING:
    from apps.finance.models import FinanceAccount
    from apps.tenants.models import Tenant

    from .models import Goal, Task

logger = logging.getLogger(__name__)


def current_period_bounds(today: date) -> tuple[date, date]:
    """First and last day of ``today``'s calendar month.

    Payments are attributed to the calendar month they land in. This
    matches how payments are actually recorded ("May payment" booked a day
    or two *after* the due date) and avoids crediting a late prior-cycle
    payment to the next cycle — which a strict ``due_day``-boundary window
    would do (verified against canary data: a May-6 payment must count for
    May, not for the June-5 cycle).
    """
    first = today.replace(day=1)
    last_day = calendar.monthrange(today.year, today.month)[1]
    return first, today.replace(day=last_day)


def effective_due_date(year: int, month: int, due_day: int) -> date:
    """``due_day`` clamped to the month length (day 31 in Feb -> 28/29)."""
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(due_day, last_day))


def obligation_for_account(account: FinanceAccount, paid_amount: Decimal, today: date) -> dict | None:
    """Project one debt account's current-cycle payment status, or ``None``.

    Returns ``None`` for non-debt accounts and for accounts without a
    schedulable obligation (no ``due_day`` or no positive
    ``minimum_payment``). ``paid_amount`` is the signed sum of payment
    transactions in the current period — signed so a "+147 / revert -147"
    correction pair nets to zero, matching the ledger's own semantics.
    """
    if not account.is_debt:
        return None
    if account.due_day is None or not account.minimum_payment:
        return None

    minimum = account.minimum_payment
    due = effective_due_date(today.year, today.month, account.due_day)

    if paid_amount >= minimum:
        period_status = "paid"
    elif paid_amount > 0:
        period_status = "partial"
    else:
        period_status = "unpaid"

    return {
        "account_id": str(account.id),
        "nickname": account.nickname,
        "minimum_payment": str(minimum),
        "paid_amount": str(paid_amount),
        "due_date": due.isoformat(),
        "period": f"{today.year:04d}-{today.month:02d}",
        "period_status": period_status,
        "overdue": today > due and period_status != "paid",
    }


def _task_dict(task: Task) -> dict:
    return {
        "id": str(task.id),
        "title": task.title,
        "status": task.status,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "pillar": task.pillar,
    }


def _goal_dict(goal: Goal) -> dict:
    return {
        "id": str(goal.id),
        "title": goal.title,
        "status": goal.status,
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "pillar": goal.pillar,
    }


def _typed_lifecycle_enabled(tenant: Tenant) -> bool:
    return bool(getattr(tenant, "experimental_typed_journal_lifecycle", False))


def _finance_active(tenant: Tenant) -> bool:
    # ``finance_active`` (not ``finance_enabled``) folds in the GRAVITY_ENABLED
    # platform pause. A paused ledger can't be written, so its paid/unpaid
    # projection would be wrong (every cycle reads "unpaid" because no payment
    # can be recorded) — so finance contributes nothing while paused, and a
    # cron grounded on the snapshot stays silent on loans rather than nagging.
    # See docs/grounding/cron-stale-status-grounding.md.
    return bool(getattr(tenant, "finance_active", False))


def _provide_tasks(tenant: Tenant, today: date) -> dict:
    """Open typed tasks. A task linked to a ``FinanceAccount`` is suppressed
    while finance is active because the obligation is its single live signal —
    this stops a done task from reading as "loan handled" while a cycle is
    outstanding. When finance is paused there is no obligation, so the task
    stays visible."""
    from .models import Task

    finance_on = _finance_active(tenant)
    open_tasks: list[dict] = []
    task_qs = Task.objects.filter(
        tenant=tenant,
        status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS],
    ).order_by("due_date", "-updated_at")
    for task in task_qs:
        ref = task.related_ref if isinstance(task.related_ref, dict) else None
        if finance_on and ref and ref.get("object_type") == "FinanceAccount":
            continue
        open_tasks.append(_task_dict(task))
    return {"open_tasks": open_tasks}


def _provide_goals(tenant: Tenant, today: date) -> dict:
    from .models import Goal

    goal_qs = Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE).order_by("target_date", "-updated_at")
    return {"active_goals": [_goal_dict(goal) for goal in goal_qs]}


def _provide_finance(tenant: Tenant, today: date) -> dict:
    from django.db.models import Sum

    from apps.finance.models import FinanceAccount, FinanceTransaction

    first, last = current_period_bounds(today)
    paid_rows = (
        FinanceTransaction.objects.filter(
            tenant=tenant,
            transaction_type=FinanceTransaction.TransactionType.PAYMENT,
            date__gte=first,
            date__lte=last,
        )
        .values("account_id")
        .annotate(total=Sum("amount"))
    )
    paid_by_account = {row["account_id"]: (row["total"] or Decimal("0")) for row in paid_rows}

    obligations: list[dict] = []
    for account in FinanceAccount.objects.filter(tenant=tenant, is_active=True):
        ob = obligation_for_account(account, paid_by_account.get(account.id, Decimal("0")), today)
        if ob is not None:
            obligations.append(ob)
    return {"obligations": obligations}


# Built-in providers. Features in other apps should register from their
# ``AppConfig.ready()`` so they are present before any snapshot is built; the
# ``test_status_registry`` suite fails if a built-in domain loses its provider.
register_status_provider("tasks", enabled=_typed_lifecycle_enabled, provide=_provide_tasks)
register_status_provider("goals", enabled=_typed_lifecycle_enabled, provide=_provide_goals)
register_status_provider("finance", enabled=_finance_active, provide=_provide_finance)


def build_journal_status(tenant: Tenant, today: date) -> dict:
    """Assemble the current-status snapshot for ``tenant`` — the authoritative,
    live, tenant-scoped read the proactive/cron layer grounds on.

    The snapshot is the union of every *enabled* registered status provider's
    contribution (see ``status_registry``), so a new feature is included
    automatically once it registers — never hand-wired here. A disabled/paused
    domain contributes nothing, so the assistant stays silent about it rather
    than guessing. A provider that raises is isolated: its key is appended to
    ``unavailable`` and the rest of the snapshot still returns.

    ``open_tasks`` / ``active_goals`` / ``obligations`` are kept as stable
    top-level keys for the journal page; additional providers add their own.
    """
    result: dict = {
        "as_of": today.isoformat(),
        "typed_lifecycle": _typed_lifecycle_enabled(tenant),
        "finance_enabled": _finance_active(tenant),
        "open_tasks": [],
        "active_goals": [],
        "obligations": [],
    }
    for provider in status_providers():
        if not provider.enabled(tenant):
            continue
        try:
            result.update(provider.provide(tenant, today))
        except Exception:
            logger.exception("status provider %r failed for tenant %s", provider.key, getattr(tenant, "id", "?"))
            result.setdefault("unavailable", []).append(provider.key)
    return result
