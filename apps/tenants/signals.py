"""Tenant lifecycle signal handlers."""

from __future__ import annotations

import logging

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


@receiver(pre_delete, sender=Tenant)
def hibernate_container_on_tenant_delete(sender, instance: Tenant, **kwargs) -> None:
    """Hibernate a tenant's container the instant its row is deleted.

    A Tenant row can be hard-deleted without deprovisioning the Azure side —
    most commonly a User account deletion (``Tenant.user`` is
    ``on_delete=CASCADE``) whose container teardown was blocked by the prod
    resource-group ``CanNotDelete`` lock. That strands a running container
    which keeps billing and POSTs internal requests that fail auth (it no
    longer has a Tenant row to validate against → log noise).

    Deactivating revisions is NOT a delete, so it succeeds under the prod
    locks. This guarantees a deleted tenant's container goes dormant
    immediately even when full teardown is blocked. Best-effort — never raises,
    so it cannot block the delete (including a User cascade).

    Full resource teardown (delete, not just hibernate) is handled separately
    by ``orphan_reaper`` / ``deprovision_tenant`` once the locks permit.
    """
    container_id = (getattr(instance, "container_id", "") or "").strip()
    if not container_id:
        return

    try:
        from apps.orchestrator.azure_client import hibernate_container_app

        hibernate_container_app(container_id)
        logger.info(
            "tenant_delete: hibernated container %s for deleted tenant %s",
            container_id,
            str(instance.id)[:8],
        )
    except Exception:
        logger.exception(
            "tenant_delete: failed to hibernate container %s for tenant %s",
            container_id,
            str(instance.id)[:8],
        )
