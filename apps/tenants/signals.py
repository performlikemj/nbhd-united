"""Tenant lifecycle signal handlers."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)
_defer_tenant_hibernate: ContextVar[bool] = ContextVar(
    "defer_tenant_hibernate",
    default=False,
)


@contextmanager
def defer_tenant_delete_hibernation():
    """Defer this task's cascade hibernation until its transaction commits."""

    token = _defer_tenant_hibernate.set(True)
    try:
        yield
    finally:
        _defer_tenant_hibernate.reset(token)


@receiver(pre_delete, sender=User)
def preserve_apple_grant_on_user_delete(sender, instance: User, **kwargs) -> None:
    """ORM-only fallback for user deletions that bypass ``_do_hard_delete``."""

    try:
        from apps.tenants.apple_services import write_apple_revocation_outbox_fallback

        write_apple_revocation_outbox_fallback(instance)
    except Exception:
        # Deletion intent wins even if the fallback copy fails. No QStash
        # publish, decrypt, or Apple HTTP is permitted from this signal.
        logger.warning(
            "user_delete: failed to preserve Apple revocation grant",
            exc_info=True,
        )


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

    tenant_id = str(instance.id)

    def hibernate_container() -> None:
        try:
            from apps.orchestrator.azure_client import hibernate_container_app

            hibernate_container_app(container_id)
            logger.info(
                "tenant_delete: hibernated container %s for deleted tenant %s",
                container_id,
                tenant_id[:8],
            )
        except Exception:
            logger.exception(
                "tenant_delete: failed to hibernate container %s for tenant %s",
                container_id,
                tenant_id[:8],
            )

    if _defer_tenant_hibernate.get():
        transaction.on_commit(hibernate_container)
    else:
        # Preserve the established behavior for deletion paths outside the
        # centralized user hard-delete service.
        hibernate_container()
