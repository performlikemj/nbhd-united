"""Payoff calculation service — snowball, avalanche, and hybrid strategies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Sequence

from dateutil.relativedelta import relativedelta


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
            target_order = _get_priority_order(
                strategy, balances, rates, nicknames
            )
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
                float(rates[i] / max_rate) * 0.6
                + float(1 - balances[i] / max_balance) * 0.4
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
        results[strategy] = calculate_payoff(
            debts, monthly_budget, strategy, start_date
        )
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
