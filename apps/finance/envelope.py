"""USER.md ``Gravity — finance state`` section.

Active accounts + total debt, active payoff plan (snowball/avalanche-aware
top-priority debt), upcoming due dates within 7 days, recent transactions.
Gated on ``tenant.finance_enabled``.

Future-work TODO — community-finance schema (``RotatingFund``,
``RotatingFundMember``, ``RotatingFundRound``,
``ParticipantReliabilityScore``) will register additional sections here
once the community-groups infrastructure + reliability rating system are
designed. See ``docs/future/community-finance.md`` for the framing.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import timedelta as _timedelta
from decimal import Decimal

from apps.finance.models import FinanceAccount, FinanceTransaction, PayoffPlan
from apps.orchestrator.envelope_registry import register_section
from apps.tenants.models import Tenant


@register_section(
    key="finance",
    heading="## Gravity — finance state",
    enabled=lambda t: getattr(t, "finance_enabled", False),
    refresh_on=(FinanceAccount, FinanceTransaction, PayoffPlan),
    order=50,
)
def render_finance(tenant: Tenant, *, max_chars: int = 1000) -> str:
    sections: list[str] = []

    active_accounts = list(FinanceAccount.objects.filter(tenant=tenant, is_active=True))
    if not active_accounts:
        return ""

    debts = [a for a in active_accounts if a.is_debt]
    total_debt = sum((a.current_balance for a in debts), Decimal("0"))

    summary_line = f"- **Active accounts**: {len(active_accounts)} ({len(debts)} debts)"
    if debts:
        summary_line += f", total debt **${total_debt:,.2f}**"
    sections.append(summary_line)

    plan = PayoffPlan.objects.filter(tenant=tenant, is_active=True).order_by("-created_at").first()
    if plan:
        plan_line = (
            f"- **Active plan**: {plan.strategy} — {plan.payoff_months} months, "
            f"payoff by {plan.payoff_date.isoformat()}, "
            f"${plan.monthly_budget:,.2f}/mo"
        )
        sections.append(plan_line)

    if debts:
        if plan and plan.strategy == PayoffPlan.Strategy.AVALANCHE:
            priority = max(debts, key=lambda a: a.interest_rate or Decimal("0"))
            priority_line = (
                f"- **Top-priority debt** (avalanche): {priority.nickname} — ${priority.current_balance:,.2f}"
            )
            if priority.interest_rate:
                priority_line += f" @ {priority.interest_rate}% APR"
        else:
            priority = min(debts, key=lambda a: a.current_balance)
            priority_line = (
                f"- **Top-priority debt** (snowball): {priority.nickname} — ${priority.current_balance:,.2f}"
            )
            if priority.minimum_payment:
                priority_line += f", min ${priority.minimum_payment:,.2f}/mo"
        sections.append(priority_line)

    today = _date.today()
    upcoming = [a for a in debts if a.due_day and 0 <= ((a.due_day - today.day) % 31) <= 7 and a.minimum_payment]
    if upcoming:
        due_lines = ["**Upcoming due dates** (next 7 days):"]
        for a in upcoming[:5]:
            due_lines.append(f"- {a.nickname} — ${a.minimum_payment:,.2f} on day {a.due_day}")
        sections.append("\n".join(due_lines))

    recent_tx = (
        FinanceTransaction.objects.filter(tenant=tenant, date__gte=today - _timedelta(days=14))
        .select_related("account")
        .order_by("-date", "-created_at")[:3]
    )
    recent_list = list(recent_tx)
    if recent_list:
        tx_lines = ["**Recent transactions**:"]
        for t in recent_list:
            tx_lines.append(f"- {t.date.isoformat()} — {t.transaction_type} ${t.amount:,.2f} → {t.account.nickname}")
        sections.append("\n".join(tx_lines))

    body = "\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n_(truncated — call nbhd_finance_summary for full state)_"
    return body
