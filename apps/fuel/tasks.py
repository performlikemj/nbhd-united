"""Fuel async tasks — executed via QStash."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def schedule_fuel_welcome_task(tenant_id: str) -> None:
    """Create a one-shot welcome cron for a newly Fuel-enabled tenant.

    Called via QStash with a ~90s delay after container restart, giving
    the container time to boot before we hit the Gateway API.
    """
    from apps.tenants.models import Tenant

    from .models import FuelProfile
    from .views import _schedule_fuel_welcome

    try:
        tenant = Tenant.objects.select_related("user").get(id=tenant_id)
    except Tenant.DoesNotExist:
        logger.warning("schedule_fuel_welcome: tenant %s not found", tenant_id)
        return

    if not tenant.fuel_enabled:
        return

    try:
        profile = FuelProfile.objects.get(tenant=tenant)
        if profile.onboarding_status != "pending":
            return
    except FuelProfile.DoesNotExist:
        return

    try:
        _schedule_fuel_welcome(tenant)
    except Exception:
        # Fire-and-forget for the live toggle path: a failure here means
        # the user gets organic onboarding on their next message instead
        # of a proactive welcome. The daily reconcile_welcomes watchdog
        # will retry. Logged so we have visibility.
        logger.exception("schedule_fuel_welcome_task failed for tenant %s", tenant_id)
