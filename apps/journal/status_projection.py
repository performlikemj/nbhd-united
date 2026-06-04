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
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.finance.models import FinanceAccount
    from apps.tenants.models import Tenant

    from .models import Goal, Task


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


def build_journal_status(tenant: Tenant, today: date) -> dict:
    """Assemble the journal current-status projection for ``tenant``.

    - ``open_tasks`` / ``active_goals``: typed rows, only when the typed
      lifecycle flag is on (otherwise the typed store isn't canonical).
    - ``obligations``: recurring-payment status folded from the finance
      ledger, only when finance is enabled.
    - A task linked to a ``FinanceAccount`` is suppressed from
      ``open_tasks`` when finance is on, because the obligation is the
      single live signal for it — this is what prevents a done May task
      from reading as "loan handled" while June is outstanding.
    """
    from django.db.models import Sum

    from apps.finance.models import FinanceAccount, FinanceTransaction

    from .models import Goal, Task

    typed_on = bool(getattr(tenant, "experimental_typed_journal_lifecycle", False))
    finance_on = bool(getattr(tenant, "finance_enabled", False))

    open_tasks: list[dict] = []
    active_goals: list[dict] = []
    obligations: list[dict] = []

    if typed_on:
        task_qs = Task.objects.filter(
            tenant=tenant,
            status__in=[Task.Status.OPEN, Task.Status.IN_PROGRESS],
        ).order_by("due_date", "-updated_at")
        for task in task_qs:
            ref = task.related_ref if isinstance(task.related_ref, dict) else None
            if finance_on and ref and ref.get("object_type") == "FinanceAccount":
                continue  # represented by an obligation below
            open_tasks.append(_task_dict(task))

        goal_qs = Goal.objects.filter(tenant=tenant, status=Goal.Status.ACTIVE).order_by("target_date", "-updated_at")
        active_goals = [_goal_dict(goal) for goal in goal_qs]

    if finance_on:
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

        for account in FinanceAccount.objects.filter(tenant=tenant, is_active=True):
            ob = obligation_for_account(account, paid_by_account.get(account.id, Decimal("0")), today)
            if ob is not None:
                obligations.append(ob)

    return {
        "as_of": today.isoformat(),
        "typed_lifecycle": typed_on,
        "finance_enabled": finance_on,
        "open_tasks": open_tasks,
        "active_goals": active_goals,
        "obligations": obligations,
    }
