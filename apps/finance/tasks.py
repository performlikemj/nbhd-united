"""Finance async tasks — executed via QStash."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


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

    if not tenant.finance_enabled:
        return

    try:
        _schedule_finance_welcome(tenant)
    except Exception:
        # Fire-and-forget for the live toggle path. The daily
        # reconcile_welcomes watchdog will retry on failure.
        logger.exception("schedule_finance_welcome_task failed for tenant %s", tenant_id)
