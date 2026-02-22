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
