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
        except Exception:
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

    # Trigger hot-reload — Azure Files (SMB) doesn't fire inotify events,
    # so the container's file watcher won't detect the config change.
    # gateway.reload reads the updated file and applies it without restart.
    try:
        from apps.cron.gateway_client import invoke_gateway_tool

        tenant.refresh_from_db()
        invoke_gateway_tool(tenant, "gateway.reload", {})
        logger.info("Config hot-reloaded for tenant %s", str(tenant_id)[:8])
    except Exception:
        logger.warning("Config written but hot-reload failed for %s — will apply on next restart", str(tenant_id)[:8])


def apply_single_tenant_image_task(tenant_id: str, desired_tag: str) -> None:
    """Update a single tenant's container image (enqueued by apply-pending-configs)."""
    import logging

    from django.conf import settings as django_settings

    from apps.orchestrator.azure_client import update_container_image
    from apps.tenants.models import Tenant

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

    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
    from apps.orchestrator.config_generator import build_cron_seed_jobs
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    # Only touch system-managed cron jobs (by name).
    # User-created crons (reminders, custom schedules) are left untouched.
    SYSTEM_JOB_NAMES = {
        "Morning Briefing",
        "Evening Check-in",
        "Weekly Reflection",
        "Week Ahead Review",
        "Background Tasks",
    }

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
            existing = (
                result.get("jobs", []) if isinstance(result, dict) else result if isinstance(result, list) else []
            )

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
            logger.info(
                "force_reseed: tenant %s — %d system jobs deleted, %d created (user jobs preserved)",
                tid,
                deleted,
                created,
            )

        except GatewayError as e:
            logger.error("force_reseed: tenant %s failed: %s", tid, e)
            total_errors += 1

    return {"tenants": tenants.count(), "updated": total_updated, "errors": total_errors}


def broadcast_single_tenant_task(tenant_id: str, message: str) -> None:
    """Send a one-off agent-driven message to a single tenant's user.

    Posts directly to the container's /v1/chat/completions endpoint —
    the same path used by the Telegram poller and LINE webhook.
    The agent processes the prompt and messages the user via
    nbhd_send_to_user.
    """
    import logging

    import httpx
    from django.conf import settings

    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).select_related("user").first()
    if not tenant or not tenant.container_fqdn:
        return

    url = f"https://{tenant.container_fqdn}/v1/chat/completions"
    gateway_token = getattr(settings, "NBHD_INTERNAL_API_KEY", "").strip()
    if not gateway_token:
        from apps.orchestrator.azure_client import read_key_vault_secret

        gateway_token = read_key_vault_secret("nbhd-internal-api-key") or ""

    user_tz = getattr(tenant.user, "timezone", None) or "UTC"

    try:
        resp = httpx.post(
            url,
            json={
                "model": "openclaw",
                "messages": [{"role": "user", "content": message}],
            },
            headers={
                "Authorization": f"Bearer {gateway_token}",
                "X-User-Timezone": user_tz,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        logger.info("Broadcast sent to tenant %s", tenant_id[:8])
    except httpx.TimeoutException:
        logger.error("Broadcast timeout for tenant %s", tenant_id[:8])
    except httpx.HTTPStatusError as e:
        logger.error(
            "Broadcast failed for tenant %s: %s %s", tenant_id[:8], e.response.status_code, e.response.text[:200]
        )
    except Exception as e:
        logger.error("Broadcast failed for tenant %s: %s", tenant_id[:8], e)


def hibernate_idle_tenants_task() -> dict:
    """Find active tenants idle >24h and hibernate their containers."""
    import logging
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from apps.orchestrator.hibernation import hibernate_idle_tenant
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    from django.db import transaction

    cutoff = timezone.now() - timedelta(hours=24)

    hibernated = 0
    failed = 0
    with transaction.atomic():
        idle_tenants = (
            Tenant.objects.filter(
                status=Tenant.Status.ACTIVE,
                container_id__gt="",
                hibernated_at__isnull=True,
            )
            .filter(Q(last_message_at__lt=cutoff) | Q(last_message_at__isnull=True, provisioned_at__lt=cutoff))
            .select_for_update(skip_locked=True)
        )

        for tenant in idle_tenants:
            # Re-check last_message_at to avoid TOCTOU race
            tenant.refresh_from_db(fields=["last_message_at", "hibernated_at"])
            if tenant.hibernated_at:
                continue
            if tenant.last_message_at and tenant.last_message_at >= cutoff:
                continue

            if hibernate_idle_tenant(tenant):
                hibernated += 1
            else:
                failed += 1

    logger.info(
        "hibernate_idle_tenants: hibernated=%d failed=%d",
        hibernated,
        failed,
    )
    return {"hibernated": hibernated, "failed": failed}


def nightly_extraction_task() -> dict:
    """Run nightly extraction for all active tenants.

    Iterates every active tenant and calls run_extraction_for_tenant().
    Each tenant is handled independently — one failure doesn't block the rest.
    """
    import logging

    from apps.journal.extraction import run_extraction_for_tenant
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
    ).select_related("user")

    results = []
    for tenant in tenants:
        try:
            result = run_extraction_for_tenant(tenant)
        except Exception:
            logger.exception("nightly_extraction: failed for tenant %s", str(tenant.id)[:8])
            result = {"skipped": "error"}
        results.append({"tenant": str(tenant.id)[:8], **result})

    extracted = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    logger.info("nightly_extraction: total=%d extracted=%d skipped=%d", len(results), extracted, skipped)
    return {"total": len(results), "extracted": extracted, "skipped": skipped}


def nightly_task_propagation_task() -> dict:
    """Propagate checked-off tasks from documents to the tasks document.

    Iterates every active tenant and calls propagate_completions_for_tenant().
    Each tenant is handled independently — one failure doesn't block the rest.
    """
    import logging

    from apps.journal.propagation import propagate_completions_for_tenant
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
    ).select_related("user")

    results = []
    for tenant in tenants:
        try:
            result = propagate_completions_for_tenant(tenant)
        except Exception:
            logger.exception("nightly_task_propagation: failed for tenant %s", str(tenant.id)[:8])
            result = {"skipped": "error"}
        results.append({"tenant": str(tenant.id)[:8], **result})

    propagated = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    logger.info("nightly_task_propagation: total=%d propagated=%d skipped=%d", len(results), propagated, skipped)
    return {"total": len(results), "propagated": propagated, "skipped": skipped}


def dedup_cron_jobs_task(tenant_id: str) -> None:
    """Remove duplicate cron jobs from a tenant's container.

    Groups by name, keeps newest (by createdAt), deletes rest.
    """
    import logging

    from apps.orchestrator.services import dedup_tenant_cron_jobs
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenant = Tenant.objects.filter(id=tenant_id).first()
    if not tenant or not tenant.container_fqdn:
        return

    result = dedup_tenant_cron_jobs(tenant)
    logger.info(
        "dedup_task: tenant %s — kept %d, deleted %d, errors %d",
        tenant_id[:8],
        result["kept"],
        result["deleted"],
        result["errors"],
    )


def remove_zombie_heartbeats_task() -> dict:
    """Remove Heartbeat Check-in cron jobs from tenants with heartbeat disabled."""
    import logging
    import time

    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
    from apps.tenants.models import Tenant

    logger = logging.getLogger(__name__)

    tenants = Tenant.objects.filter(
        status=Tenant.Status.ACTIVE,
        container_id__gt="",
        heartbeat_enabled=False,
    ).select_related("user")

    removed = 0
    skipped = 0
    errors = 0

    for tenant in tenants:
        tid = str(tenant.id)[:8]
        if not tenant.container_fqdn:
            skipped += 1
            continue

        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        except GatewayError:
            logger.warning("zombie_heartbeats: tenant %s — cannot list jobs", tid)
            errors += 1
            time.sleep(1)
            continue

        jobs = []
        if isinstance(list_result, dict):
            inner = list_result.get("details", list_result)
            if isinstance(inner, dict):
                jobs = inner.get("jobs", [])
            else:
                jobs = list_result.get("jobs", [])
        elif isinstance(list_result, list):
            jobs = list_result

        heartbeat = next(
            (j for j in jobs if isinstance(j, dict) and j.get("name") == "Heartbeat Check-in"),
            None,
        )
        if not heartbeat:
            skipped += 1
            time.sleep(0.5)
            continue

        job_id = heartbeat.get("id") or heartbeat.get("jobId", "Heartbeat Check-in")
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            removed += 1
            logger.info("zombie_heartbeats: tenant %s — removed heartbeat (id=%s)", tid, job_id)
        except GatewayError:
            logger.exception("zombie_heartbeats: tenant %s — failed to remove", tid)
            errors += 1

        time.sleep(1)

    return {"removed": removed, "skipped": skipped, "errors": errors}
