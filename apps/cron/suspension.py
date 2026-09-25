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
