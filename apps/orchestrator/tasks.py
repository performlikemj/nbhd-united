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
    """Scale all suspended tenant containers to zero replicas."""
    from apps.orchestrator.azure_client import scale_container_app
    from apps.tenants.models import Tenant

    tenants = Tenant.objects.filter(
        status=Tenant.Status.SUSPENDED,
    ).exclude(container_id="")

    hibernated = 0
    failed = 0
    for tenant in tenants:
        try:
            scale_container_app(tenant.container_id, min_replicas=0, max_replicas=0)
            hibernated += 1
        except Exception as e:
            failed += 1

    return {"hibernated": hibernated, "failed": failed, "total": tenants.count()}


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
