"""Payoff calculation service — snowball, avalanche, and hybrid strategies."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from django.db import transaction as db_transaction

from .models import FinanceAccount, FinanceTransaction

logger = logging.getLogger(__name__)


@dataclass
class DebtInput:
    nickname: str
    balance: Decimal
    interest_rate: Decimal  # APR as percentage (e.g. 22.9)
    minimum_payment: Decimal


@dataclass
class MonthlyAccountState:
    nickname: str
    balance: Decimal
    payment: Decimal


@dataclass
class MonthScheduleEntry:
    month: int
    accounts: list[MonthlyAccountState]
    total_remaining: Decimal


@dataclass
class PayoffResult:
    strategy: str
    monthly_budget: Decimal
    total_debt: Decimal
    total_interest: Decimal
    payoff_months: int
    payoff_date: date
    schedule: list[MonthScheduleEntry]


def _monthly_rate(apr_percent: Decimal) -> Decimal:
    """Convert APR percentage to monthly decimal rate."""
    return apr_percent / Decimal("1200")


def _two_places(val: Decimal) -> Decimal:
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_payoff(
    debts: Sequence[DebtInput],
    monthly_budget: Decimal,
    strategy: str,
    start_date: date | None = None,
    max_months: int = 600,  # 50-year safety cap
) -> PayoffResult:
    """Calculate a debt payoff plan using the given strategy.

    Args:
        debts: List of debt accounts with balances, rates, and minimums.
        monthly_budget: Total monthly amount available for all debt payments.
        strategy: One of 'snowball', 'avalanche', or 'hybrid'.
        start_date: First payment date. Defaults to next month.
        max_months: Safety cap to prevent infinite loops.

    Returns:
        PayoffResult with timeline, total interest, and month-by-month schedule.
    """
    if not debts:
        today = start_date or date.today()
        return PayoffResult(
            strategy=strategy,
            monthly_budget=monthly_budget,
            total_debt=Decimal("0"),
            total_interest=Decimal("0"),
            payoff_months=0,
            payoff_date=today,
            schedule=[],
        )

    if start_date is None:
        today = date.today()
        start_date = (today + relativedelta(months=1)).replace(day=1)

    # Working copies
    balances = [Decimal(str(d.balance)) for d in debts]
    rates = [_monthly_rate(d.interest_rate) for d in debts]
    minimums = [Decimal(str(d.minimum_payment)) for d in debts]
    nicknames = [d.nickname for d in debts]

    total_debt = sum(balances)
    total_interest = Decimal("0")
    schedule: list[MonthScheduleEntry] = []

    for month_num in range(1, max_months + 1):
        # Check if all paid off
        if all(b <= 0 for b in balances):
            break

        # Step 1: accrue interest on each debt
        for i in range(len(debts)):
            if balances[i] > 0:
                interest = _two_places(balances[i] * rates[i])
                balances[i] += interest
                total_interest += interest

        # Step 2: pay minimums first
        payments = [Decimal("0")] * len(debts)
        remaining_budget = Decimal(str(monthly_budget))

        for i in range(len(debts)):
            if balances[i] <= 0:
                continue
            min_pay = min(minimums[i], balances[i])
            actual_pay = min(min_pay, remaining_budget)
            payments[i] = actual_pay
            remaining_budget -= actual_pay

        # Step 3: allocate extra payment based on strategy
        if remaining_budget > 0:
            target_order = _get_priority_order(strategy, balances, rates, nicknames)
            for idx in target_order:
                if balances[idx] <= 0 or remaining_budget <= 0:
                    continue
                extra = min(remaining_budget, balances[idx] - payments[idx])
                if extra > 0:
                    payments[idx] += extra
                    remaining_budget -= extra

        # Step 4: apply payments
        month_accounts = []
        for i in range(len(debts)):
            balances[i] = max(Decimal("0"), balances[i] - payments[i])
            month_accounts.append(
                MonthlyAccountState(
                    nickname=nicknames[i],
                    balance=_two_places(balances[i]),
                    payment=_two_places(payments[i]),
                )
            )

        schedule.append(
            MonthScheduleEntry(
                month=month_num,
                accounts=month_accounts,
                total_remaining=_two_places(sum(balances)),
            )
        )

        if all(b <= 0 for b in balances):
            break

    payoff_months = len(schedule)
    payoff_date = start_date + relativedelta(months=payoff_months)

    return PayoffResult(
        strategy=strategy,
        monthly_budget=monthly_budget,
        total_debt=_two_places(total_debt),
        total_interest=_two_places(total_interest),
        payoff_months=payoff_months,
        payoff_date=payoff_date,
        schedule=schedule,
    )


def _get_priority_order(
    strategy: str,
    balances: list[Decimal],
    rates: list[Decimal],
    nicknames: list[str],
) -> list[int]:
    """Return indices sorted by payment priority for the given strategy."""
    active = [i for i in range(len(balances)) if balances[i] > 0]

    if strategy == "snowball":
        # Smallest balance first
        return sorted(active, key=lambda i: balances[i])

    elif strategy == "avalanche":
        # Highest interest rate first
        return sorted(active, key=lambda i: rates[i], reverse=True)

    elif strategy == "hybrid":
        # Alternate: highest rate gets priority, but smallest balance
        # gets a share too. We split by sending 60% to highest rate
        # and 40% to smallest balance. For ordering, we use a
        # weighted score combining both factors.
        if not active:
            return []
        max_balance = max(balances[i] for i in active) or Decimal("1")
        max_rate = max(rates[i] for i in active) or Decimal("1")
        return sorted(
            active,
            key=lambda i: (
                # Higher score = higher priority
                float(rates[i] / max_rate) * 0.6 + float(1 - balances[i] / max_balance) * 0.4
            ),
            reverse=True,
        )

    return active  # fallback: original order


def compare_strategies(
    debts: Sequence[DebtInput],
    monthly_budget: Decimal,
    start_date: date | None = None,
) -> dict[str, PayoffResult]:
    """Run all three strategies and return results keyed by strategy name."""
    results = {}
    for strategy in ("snowball", "avalanche", "hybrid"):
        results[strategy] = calculate_payoff(debts, monthly_budget, strategy, start_date)
    return results


def payoff_result_to_dict(result: PayoffResult) -> dict:
    """Serialize a PayoffResult to a JSON-safe dict."""
    return {
        "strategy": result.strategy,
        "monthly_budget": str(result.monthly_budget),
        "total_debt": str(result.total_debt),
        "total_interest": str(result.total_interest),
        "payoff_months": result.payoff_months,
        "payoff_date": result.payoff_date.isoformat(),
        "schedule": [
            {
                "month": entry.month,
                "accounts": [
                    {
                        "nickname": a.nickname,
                        "balance": str(a.balance),
                        "payment": str(a.payment),
                    }
                    for a in entry.accounts
                ],
                "total_remaining": str(entry.total_remaining),
            }
            for entry in result.schedule
        ],
    }


# ═════════════════════════════════════════════════════════════════════
# Transaction recording (shared by the runtime + consumer write paths)
# ═════════════════════════════════════════════════════════════════════


class AccountNotFound(Exception):
    """No active account matched the supplied id or nickname."""


def resolve_account(tenant, *, account_id=None, account_nickname=None) -> FinanceAccount:
    """Resolve an active ``FinanceAccount`` for ``tenant``.

    Prefers an explicit ``account_id``; otherwise falls back to a fuzzy
    nickname match (case-insensitive exact, then ``icontains``). Raises
    ``AccountNotFound`` when nothing matches.
    """
    if account_id:
        try:
            uuid.UUID(str(account_id))
        except (ValueError, TypeError, AttributeError):
            raise AccountNotFound(f"No account found with id '{account_id}'") from None
        account = FinanceAccount.objects.filter(id=account_id, tenant=tenant, is_active=True).first()
        if account is None:
            raise AccountNotFound(f"No account found with id '{account_id}'")
        return account

    nickname = (account_nickname or "").strip()
    if nickname:
        account = FinanceAccount.objects.filter(tenant=tenant, is_active=True, nickname__iexact=nickname).first()
        if account is None:
            account = FinanceAccount.objects.filter(tenant=tenant, is_active=True, nickname__icontains=nickname).first()
        if account is not None:
            return account

    raise AccountNotFound(f"No account found matching '{account_nickname or account_id or ''}'")


def record_transaction(
    *,
    tenant,
    account: FinanceAccount,
    amount: Decimal,
    transaction_type: str = "payment",
    txn_date: date | None = None,
    description: str = "",
) -> tuple[dict, bool]:
    """Record a transaction against ``account`` and mutate its balance.

    Returns ``(payload, created)``; ``created`` is ``False`` on a dedup hit.

    Dedup guard: a pre-existing row with the same
    ``(tenant, account, transaction_type, amount, date)`` is treated as a
    re-record — an agent (or client) retry after a silent timeout — so the
    existing row is returned WITHOUT debiting the balance a second time. This
    is the forward fix for the 2026-05 incident where agent retries triple-
    recorded the same loan payment. ``select_for_update`` serialises concurrent
    writes per account, so the dedup check and balance mutation are atomic.
    """
    if transaction_type not in FinanceTransaction.TransactionType.values:
        transaction_type = "payment"
    txn_date = txn_date or date.today()
    description = (description or "")[:256]

    with db_transaction.atomic():
        locked = FinanceAccount.objects.select_for_update().get(pk=account.pk)
        existing = (
            FinanceTransaction.objects.filter(
                tenant=tenant,
                account=locked,
                transaction_type=transaction_type,
                amount=amount,
                date=txn_date,
            )
            .order_by("created_at")
            .first()
        )
        if existing is not None:
            logger.info(
                "finance.dedup_hit tenant=%s account=%s amount=%s date=%s type=%s existing_id=%s",
                str(tenant.id)[:8],
                locked.nickname,
                amount,
                txn_date,
                transaction_type,
                existing.id,
            )
            return (
                {
                    "transaction_id": str(existing.id),
                    "account_id": str(locked.id),
                    "account_nickname": locked.nickname,
                    "new_balance": str(locked.current_balance.quantize(Decimal("0.01"))),
                    "transaction_type": existing.transaction_type,
                    "amount": str(existing.amount),
                    "duplicate": True,
                    "existing_description": existing.description,
                    "existing_recorded_at": existing.created_at.isoformat(),
                },
                False,
            )

        txn_row = FinanceTransaction.objects.create(
            tenant=tenant,
            account=locked,
            transaction_type=transaction_type,
            amount=amount,
            description=description,
            date=txn_date,
        )
        if transaction_type in ("payment", "refund"):
            locked.current_balance = max(Decimal("0"), locked.current_balance - amount)
        elif transaction_type in ("charge", "interest"):
            locked.current_balance += amount
        locked.save(update_fields=["current_balance", "updated_at"])

    return (
        {
            "transaction_id": str(txn_row.id),
            "account_id": str(locked.id),
            "account_nickname": locked.nickname,
            "new_balance": str(locked.current_balance.quantize(Decimal("0.01"))),
            "transaction_type": transaction_type,
            "amount": str(amount),
            "duplicate": False,
        },
        True,
    )
