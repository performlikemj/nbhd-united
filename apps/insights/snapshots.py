"""Per-pillar snapshot computation for the assistant baseline.

Snapshot functions return a JSON-serializable dict that lands in
``PillarSnapshot.payload``. The shape mirrors what the corresponding pillar
tab renders today, so the assistant reasoning over snapshots is reasoning over
the same surface the user sees.

Phase 1 ships ``compute_gravity_snapshot`` only (account-level trajectory).
Category-level trajectory ("dining is 1.8x baseline") is Phase 1.5 — it
requires a ``category`` field on ``FinanceTransaction`` that does not exist
today.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apps.finance.models import FinanceAccount, FinanceTransaction, PayoffPlan
from apps.tenants.models import Tenant

SCHEMA_VERSION = 1


def _money(value: Decimal | None) -> str:
    """Serialize a Decimal to a string for JSON storage. ``None`` becomes ``"0"``."""
    return str(value if value is not None else Decimal("0"))


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
