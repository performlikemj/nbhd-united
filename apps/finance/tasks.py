"""Finance async tasks — executed via QStash."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def create_monthly_snapshots_task() -> int:
    """Write FinanceSnapshot rows for every finance-enabled active tenant.

    Called on the 1st of each month via QStash
    (``snapshot-finance-monthly`` in register_system_crons.py).

    Thin wrapper around ``apps.finance.snapshot.create_monthly_snapshots``
    so the function can be referenced from TASK_MAP in apps/cron/views.py.
    Returns the number of snapshots created (0 when all tenants are
    already snapshotted or none are finance-enabled).
    """
    from .snapshot import create_monthly_snapshots

    return create_monthly_snapshots()


def schedule_finance_welcome_task(tenant_id: str) -> None:
    """Create a one-shot welcome cron for a newly Gravity-enabled tenant.

    Called via QStash with a ~90s delay after the FinanceSettingsView.patch
    flip, giving the container time to pick up the new config (the agent
    would otherwise see the welcome cron's payload before the finance
    plugin fully reloads).

    Mirrors ``apps/fuel/tasks.py:schedule_fuel_welcome_task``. No-op if
    finance has been re-disabled in the interval.
    """
    from apps.tenants.models import Tenant

    from .views import _schedule_finance_welcome

    try:
        tenant = Tenant.objects.select_related("user").get(id=tenant_id)
    except Tenant.DoesNotExist:
        logger.warning("schedule_finance_welcome: tenant %s not found", tenant_id)
        return

    if not tenant.finance_active:
        return

    try:
        _schedule_finance_welcome(tenant)
    except Exception:
        # Fire-and-forget for the live toggle path. The daily
        # reconcile_welcomes watchdog will retry on failure.
        logger.exception("schedule_finance_welcome_task failed for tenant %s", tenant_id)
