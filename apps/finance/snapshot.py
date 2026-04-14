"""Monthly finance snapshot service — creates point-in-time balance records."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db.models import Sum

from apps.tenants.models import Tenant

from .models import FinanceAccount, FinanceSnapshot, FinanceTransaction

logger = logging.getLogger(__name__)


def create_monthly_snapshots(snapshot_date: date | None = None) -> int:
    """Create a FinanceSnapshot for each finance-enabled tenant.

    Intended to be called on the 1st of each month via QStash.
    Returns the number of snapshots created.
    """
    if snapshot_date is None:
        snapshot_date = date.today().replace(day=1)

    tenants = Tenant.objects.filter(
        finance_enabled=True,
        status=Tenant.Status.ACTIVE,
    )

    created_count = 0
    for tenant in tenants:
        try:
            _create_snapshot_for_tenant(tenant, snapshot_date)
            created_count += 1
        except Exception:
            logger.exception("Failed to create finance snapshot for tenant %s", tenant.id)

    logger.info("Created %d finance snapshots for %s", created_count, snapshot_date)
    return created_count


def _create_snapshot_for_tenant(tenant: Tenant, snapshot_date: date) -> FinanceSnapshot | None:
    """Create a snapshot for a single tenant, skipping if one already exists."""
    if FinanceSnapshot.objects.filter(tenant=tenant, date=snapshot_date).exists():
        return None

    accounts = list(FinanceAccount.objects.filter(tenant=tenant, is_active=True))
    if not accounts:
        return None

    debt_types = FinanceAccount.DEBT_TYPES
    total_debt = sum(a.current_balance for a in accounts if a.account_type in debt_types) or Decimal("0")
    total_savings = sum(a.current_balance for a in accounts if a.account_type not in debt_types) or Decimal("0")

    # Sum payments made in the previous month
    from dateutil.relativedelta import relativedelta

    prev_month_start = snapshot_date - relativedelta(months=1)
    total_payments = FinanceTransaction.objects.filter(
        tenant=tenant,
        transaction_type="payment",
        date__gte=prev_month_start,
        date__lt=snapshot_date,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    accounts_json = [
        {
            "nickname": a.nickname,
            "type": a.account_type,
            "balance": str(a.current_balance),
        }
        for a in accounts
    ]

    return FinanceSnapshot.objects.create(
        tenant=tenant,
        date=snapshot_date,
        total_debt=total_debt,
        total_savings=total_savings,
        total_payments_this_month=total_payments,
        accounts_json=accounts_json,
    )
