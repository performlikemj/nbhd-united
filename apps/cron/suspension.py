"""Cron job suspension and resumption for tenant lifecycle events.

When a tenant's trial expires or subscription lapses, their cron jobs
should be disabled (not deleted) so they can be re-enabled if the user
subscribes. This module provides the suspend/resume helpers.
"""

from __future__ import annotations

import logging
from typing import Any

from apps.tenants.models import Tenant

from .gateway_client import GatewayError, invoke_gateway_tool

logger = logging.getLogger(__name__)


def suspend_tenant_crons(tenant: Tenant) -> dict[str, Any]:
    """Disable all enabled cron jobs for a tenant.

    Returns a summary dict with counts of disabled/skipped/errors.
    Jobs that are already disabled are left untouched.
    """
    from .share_cron_sync import tenant_uses_file_cron_sync

    if tenant_uses_file_cron_sync(tenant):
        return _file_lifecycle(tenant, suspend=True)

    result = {"disabled": 0, "already_disabled": 0, "errors": 0, "job_names": []}

    if not tenant.container_fqdn:
        logger.warning("suspend_tenant_crons: tenant %s has no FQDN", tenant.id)
        return result

    try:
        list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        jobs = (
            list_result.get("jobs", [])
            if isinstance(list_result, dict)
            else list_result
            if isinstance(list_result, list)
            else []
        )
    except GatewayError as e:
        logger.error(
            "suspend_tenant_crons: failed to list crons for tenant %s: %s",
            tenant.id,
            e,
        )
        result["errors"] = 1
        return result

    for job in jobs:
        job_id = job.get("jobId") or job.get("id") or job.get("name")
        if not job_id:
            continue

        # Skip jobs that are already disabled
        if not job.get("enabled", True):
            result["already_disabled"] += 1
            continue

        try:
            invoke_gateway_tool(tenant, "cron.update", {"jobId": job_id, "patch": {"enabled": False}})
            result["disabled"] += 1
            result["job_names"].append(job.get("name", job_id))
        except GatewayError as e:
            logger.error(
                "suspend_tenant_crons: failed to disable job %s for tenant %s: %s",
                job_id,
                tenant.id,
                e,
            )
            result["errors"] += 1

    logger.info(
        "suspend_tenant_crons: tenant %s — disabled=%d already_disabled=%d errors=%d",
        str(tenant.id)[:8],
        result["disabled"],
        result["already_disabled"],
        result["errors"],
    )
    return result


def resume_tenant_crons(tenant: Tenant) -> dict[str, Any]:
    """Re-enable all disabled cron jobs for a tenant.

    Called when a suspended tenant reactivates (subscribes).
    Re-enables all disabled jobs — both system and user-created.
    """
    from .share_cron_sync import tenant_uses_file_cron_sync

    if tenant_uses_file_cron_sync(tenant):
        return _file_lifecycle(tenant, suspend=False)

    result = {"enabled": 0, "already_enabled": 0, "errors": 0, "job_names": []}

    if not tenant.container_fqdn:
        logger.warning("resume_tenant_crons: tenant %s has no FQDN", tenant.id)
        return result

    try:
        list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
        jobs = (
            list_result.get("jobs", [])
            if isinstance(list_result, dict)
            else list_result
            if isinstance(list_result, list)
            else []
        )
    except GatewayError as e:
        logger.error(
            "resume_tenant_crons: failed to list crons for tenant %s: %s",
            tenant.id,
            e,
        )
        result["errors"] = 1
        return result

    for job in jobs:
        job_id = job.get("jobId") or job.get("id") or job.get("name")
        if not job_id:
            continue

        # Skip jobs that are already enabled
        if job.get("enabled", True):
            result["already_enabled"] += 1
            continue

        try:
            invoke_gateway_tool(tenant, "cron.update", {"jobId": job_id, "patch": {"enabled": True}})
            result["enabled"] += 1
            result["job_names"].append(job.get("name", job_id))
        except GatewayError as e:
            logger.error(
                "resume_tenant_crons: failed to enable job %s for tenant %s: %s",
                job_id,
                tenant.id,
                e,
            )
            result["errors"] += 1

    logger.info(
        "resume_tenant_crons: tenant %s — enabled=%d already_enabled=%d errors=%d",
        str(tenant.id)[:8],
        result["enabled"],
        result["already_enabled"],
        result["errors"],
    )
    return result


def _file_lifecycle(tenant, *, suspend):
    """Pause the signed writer first, then disable operator-owned jobs.

    The saved enabled-ID set makes retries safe and leaves pre-existing disabled
    jobs disabled on resume. Managed declarations are rebuilt from Postgres.
    """
    import time

    from apps.orchestrator.runtime_operator import (
        capture_cron_declarations,
        list_crons,
        restore_crons,
        set_cron_enabled,
    )

    from .share_cron_sync import write_tenant_crons_file

    tenant.refresh_from_db(fields=["cron_suspend_state"])
    state = tenant.cron_suspend_state or {}
    action = "disabled" if suspend else "enabled"
    result = {action: 0, "errors": 0, "job_names": []}
    if suspend:
        if not state:
            jobs = capture_cron_declarations(tenant)
            state = {
                "active": True,
                "enabled_ids": [j["id"] for j in jobs if j.get("enabled", True)],
                "declarations": [j for j in jobs if not j.get("declarationKey", "").startswith("nbhd:")],
            }
            Tenant.objects.filter(pk=tenant.pk).update(cron_suspend_state=state)
            tenant.cron_suspend_state = state
        if not state.get("active"):
            state["active"] = True
            Tenant.objects.filter(pk=tenant.pk).update(cron_suspend_state=state)
            tenant.cron_suspend_state = state
        write_tenant_crons_file(tenant)
        # Let any prior file reconcile finish before checking for enabled jobs.
        # An empty signed file removes managed declarations; disabling the rest
        # prevents boot catch-up. Never deactivate while any enabled job remains.
        previous_quiet = False
        for attempt in range(8):
            jobs = list_crons(tenant)
            enabled = [j for j in jobs if j["enabled"]]
            for job in enabled:
                if job.get("declarationKey", "").startswith("nbhd:"):
                    # Let the empty signed declaration REMOVE managed jobs. The
                    # image helper's default list hides disabled jobs; disabling
                    # them here could strand them across wake/resume.
                    continue
                set_cron_enabled(tenant, job["id"], False)
                result[action] += 1
            if not enabled:
                # Require a second observation after a full sync interval so an
                # already-running reconcile cannot re-enable a stale declaration.
                if attempt and previous_quiet:
                    return result
                previous_quiet = True
                time.sleep(25)
                continue
            previous_quiet = False
            time.sleep(5)
        raise RuntimeError("Signed cron suspension did not converge")
    if not state:
        return result
    # Retain the resume IDs until both the file write and operator edits succeed.
    state["active"] = False
    Tenant.objects.filter(pk=tenant.pk).update(cron_suspend_state=state)
    tenant.cron_suspend_state = state
    write_tenant_crons_file(tenant)
    declarations = state.get("declarations")
    if declarations is None:
        # Old ID-only recovery records cannot establish what was lost.
        raise RuntimeError("recovery_declarations_missing")
    restored = restore_crons(tenant, declarations) if declarations else {"verified": True}
    if not restored.get("verified"):
        raise RuntimeError("recovery_not_verified")
    from apps.orchestrator.openclaw_migration import _signed_match
    from apps.orchestrator.runtime_operator import inspect_signed_crons

    from .share_cron_sync import _desired_jobs

    expected = {j["declarationKey"] for j in _desired_jobs(tenant)}
    for attempt in range(8):
        inspection = inspect_signed_crons(tenant)
        if _signed_match(inspection) and {m["key"] for m in inspection["matches"]} == expected:
            break
        time.sleep(5)
    else:
        raise RuntimeError("signed_resume_not_verified")
    Tenant.objects.filter(pk=tenant.pk).update(cron_suspend_state={})
    tenant.cron_suspend_state = {}
    return result
