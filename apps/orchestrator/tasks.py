"""Tasks for async provisioning/deprovisioning (executed via QStash)."""
from .services import (
    deprovision_tenant,
    provision_tenant,
    repair_stale_tenant_provisioning,
    seed_cron_jobs,
    update_tenant_config,
)


def provision_tenant_task(tenant_id: str) -> None:
    """Provision an OpenClaw instance for a tenant."""
    provision_tenant(tenant_id)


def deprovision_tenant_task(tenant_id: str) -> None:
    """Deprovision a tenant's OpenClaw instance."""
    deprovision_tenant(tenant_id)


def update_tenant_config_task(tenant_id: str) -> None:
    """Update an active tenant's OpenClaw container config."""
    update_tenant_config(tenant_id)


def seed_cron_jobs_task(tenant_id: str) -> None:
    """Seed cron job definitions into a tenant's running OpenClaw container."""
    seed_cron_jobs(tenant_id)


def repair_stale_tenant_provisioning_task(limit: int = 50) -> dict:
    """Repair stale tenant provisioning states in a safe, idempotent sweep."""
    return repair_stale_tenant_provisioning(limit=limit, dry_run=False)


def hibernate_suspended_task() -> dict:
    """Hibernate all suspended tenant containers (deactivate revisions)."""
    from apps.orchestrator.azure_client import hibernate_container_app
    from apps.tenants.models import Tenant

    tenants = Tenant.objects.filter(
        status=Tenant.Status.SUSPENDED,
    ).exclude(container_id="")

    hibernated = 0
    failed = 0
    for tenant in tenants:
        try:
            hibernate_container_app(tenant.container_id)
            hibernated += 1
        except Exception as e:
            failed += 1

    return {"hibernated": hibernated, "failed": failed, "total": tenants.count()}


def apply_single_tenant_config_task(tenant_id: str) -> None:
    """Apply pending config for a single tenant (enqueued by apply-pending-configs).

    Updates the tenant's OpenClaw config and bumps config_version.
    """
    import logging
    from django.db import models as db_models
    from django.utils import timezone as tz
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant:
        return

    # Skip if no longer pending
    if tenant.config_version >= tenant.pending_config_version:
        return

    try:
        update_tenant_config(tenant_id)
    except Exception:
        logger.exception("apply_single_tenant_config failed for %s", tenant_id)
        return

    Tenant.objects.filter(id=tenant_id).update(
        config_version=db_models.F("pending_config_version"),
        config_refreshed_at=tz.now(),
    )


def apply_single_tenant_image_task(tenant_id: str, desired_tag: str) -> None:
    """Update a single tenant's container image (enqueued by apply-pending-configs)."""
    import logging
    from django.conf import settings as django_settings
    from apps.tenants.models import Tenant
    from apps.orchestrator.azure_client import update_container_image

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant or not tenant.container_id:
        return

    desired_image = f"{django_settings.AZURE_ACR_SERVER}/nbhd-openclaw:{desired_tag}"
    try:
        update_container_image(tenant.container_id, desired_image)
        Tenant.objects.filter(id=tenant_id).update(
            container_image_tag=desired_tag,
        )
    except Exception:
        logger.exception("apply_single_tenant_image failed for %s", tenant_id)


def force_reseed_crons_task() -> dict:
    """Delete and recreate cron jobs for all active tenants.

    Use when cron job definitions have changed and need to be pushed everywhere.
    """
    import logging
    from apps.tenants.models import Tenant
    from apps.orchestrator.config_generator import build_cron_seed_jobs
    from apps.cron.gateway_client import invoke_gateway_tool, GatewayError

    logger = logging.getLogger(__name__)

    # Only touch system-managed cron jobs (by name).
    # User-created crons (reminders, custom schedules) are left untouched.
    SYSTEM_JOB_NAMES = {"Morning Briefing", "Evening Check-in", "Week Ahead Review", "Background Tasks"}

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
    ).select_related("user")

    total_updated = 0
    total_errors = 0

    for tenant in tenants:
        tid = str(tenant.id)[:8]
        if not tenant.container_fqdn:
            logger.warning("force_reseed: tenant %s has no FQDN, skipping", tid)
            total_errors += 1
            continue

        try:
            # List existing
            result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
            existing = result.get("jobs", []) if isinstance(result, dict) else result if isinstance(result, list) else []

            # Delete only system-managed jobs
            deleted = 0
            for job in existing:
                if job.get("name") not in SYSTEM_JOB_NAMES:
                    continue  # User-created job — don't touch
                job_id = job.get("id") or job.get("jobId")
                if job_id:
                    try:
                        invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
                        deleted += 1
                    except GatewayError:
                        pass

            # Re-create system jobs with updated config
            created = 0
            for job in build_cron_seed_jobs(tenant):
                try:
                    invoke_gateway_tool(tenant, "cron.add", {"job": job})
                    created += 1
                except GatewayError as e:
                    logger.error("force_reseed: add %s for %s failed: %s", job.get("name"), tid, e)
                    total_errors += 1

            total_updated += created
            logger.info("force_reseed: tenant %s — %d system jobs deleted, %d created (user jobs preserved)", tid, deleted, created)

        except GatewayError as e:
            logger.error("force_reseed: tenant %s failed: %s", tid, e)
            total_errors += 1

    return {"tenants": tenants.count(), "updated": total_updated, "errors": total_errors}


def broadcast_single_tenant_task(tenant_id: str, message: str) -> None:
    """Send a one-off agent-driven message to a single tenant's user.

    Creates a temporary cron job that fires once in 30 seconds, then
    self-deletes. The agent receives the prompt and messages the user
    via nbhd_send_to_user.
    """
    import logging
    from apps.tenants.models import Tenant
    from apps.cron.gateway_client import invoke_gateway_tool, GatewayError

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant or not tenant.container_fqdn:
        return

    # Use a one-shot cron: fires in 30s, maxRuns=1
    job = {
        "name": "One-off broadcast",
        "schedule": "in 30 seconds",
        "maxRuns": 1,
        "prompt": message,
    }
    try:
        invoke_gateway_tool(tenant, "cron.add", {"job": job})
        logger.info("Broadcast cron added for tenant %s", tenant_id[:8])
    except GatewayError as e:
        logger.error("Broadcast failed for tenant %s: %s", tenant_id[:8], e)


def dedup_cron_jobs_task(tenant_id: str) -> None:
    """Remove duplicate cron jobs from a tenant's container.

    Keeps the first job for each unique name and deletes the rest.
    """
    import logging
    from apps.tenants.models import Tenant
    from apps.cron.gateway_client import invoke_gateway_tool, GatewayError

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant or not tenant.container_fqdn:
        return

    try:
        list_result = invoke_gateway_tool(
            tenant, "cron.list", {"includeDisabled": True}
        )
    except GatewayError as e:
        logger.error("dedup: failed to list crons for tenant %s: %s", tenant_id[:8], e)
        return

    # Extract jobs
    jobs = []
    if isinstance(list_result, list):
        jobs = list_result
    elif isinstance(list_result, dict):
        inner = list_result.get("details", list_result)
        if isinstance(inner, dict):
            jobs = inner.get("jobs", [])
        if not jobs:
            jobs = list_result.get("jobs", [])

    if not jobs:
        logger.info("dedup: tenant %s has no jobs", tenant_id[:8])
        return

    # Group by name, keep first, delete rest
    seen = {}
    to_delete = []
    for job in jobs:
        name = job.get("name", "")
        job_id = job.get("id", job.get("jobId", ""))
        if not name or not job_id:
            continue
        if name in seen:
            to_delete.append((job_id, name))
        else:
            seen[name] = job_id

    deleted = 0
    errors = 0
    for job_id, name in to_delete:
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"id": job_id})
            deleted += 1
        except GatewayError as e:
            logger.error("dedup: failed to delete job %s (%s) for tenant %s: %s",
                         job_id, name, tenant_id[:8], e)
            errors += 1

    logger.info(
        "dedup: tenant %s — kept %d unique jobs, deleted %d duplicates, %d errors",
        tenant_id[:8], len(seen), deleted, errors,
    )
