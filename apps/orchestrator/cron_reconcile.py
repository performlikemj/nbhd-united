"""Generalized Postgres-canonical cron reconciler.

This is the broad-surface generalization of ``apps/orchestrator/fuel_cron.py``.
While that module reconciles only ``_fuel:{8-hex}`` per-session crons derived
from ``Workout.scheduled_at``, this one reconciles the full set of managed
crons stored as ``apps/cron/models.CronJob`` rows: system schedules
(Morning Briefing, Heartbeat, etc.), user-created crons, and Fuel sessions
(once they're materialized into CronJob rows by the broader cutover).

Postgres ``CronJob`` rows where ``managed=True`` are the desired set.
``cron.list`` against the gateway gives current SQLite state. Diff by name;
``cron.add`` for missing, ``cron.remove`` for stale. Unmanaged prefixes
(``_sync:*`` — agent-created Phase 2 sync crons) are explicitly skipped so
agent-side writes survive reconciliation.

The reconciler is gated by ``Tenant.postgres_cron_canonical``. While that
flag is False (cutover-day default), the legacy gateway-canonical paths
remain authoritative and this function is a no-op.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Cron-name prefixes that the reconciler MUST NOT touch. These crons live
# only in OpenClaw's SQLite (the agent writes them via the cron tool) and
# self-clean. If we managed them, the reconciler would race the agent and
# remove them mid-flight.
_UNMANAGED_PREFIXES: tuple[str, ...] = ("_sync:",)


def _is_unmanaged_cron(name: str) -> bool:
    if not name:
        return True
    return name.startswith(_UNMANAGED_PREFIXES)


def _row_to_cron_dict(row) -> dict:
    """Render a ``CronJob`` row into the gateway-shape dict for ``cron.add``.

    The full schedule/payload/delivery/sessionTarget shape lives in
    ``row.data``; we overlay the canonical ``name`` and ``enabled`` so the
    columns can't drift from the JSON. Strips gateway-internal fields that
    ``cron.add`` rejects (``id``, ``createdAt``, ...).
    """
    job = dict(row.data or {})
    job["name"] = row.name
    job["enabled"] = bool(row.enabled)
    for stripped in ("id", "jobId", "createdAt", "state", "createdAtMs", "updatedAtMs", "nextRunAtMs", "runningAtMs"):
        job.pop(stripped, None)
    return job


def regenerate_tenant_crons(tenant: Tenant) -> dict:
    """Reconcile container managed crons against the Postgres CronJob table.

    Postgres rows where ``managed=True`` are the desired set. The container's
    ``cron.list`` (filtered to managed-prefix crons) is the current set. Diff
    by name and apply the minimal change via the gateway.

    Called from:
      - ``apps/cron/signals.py`` post_save / post_delete on CronJob (via
        debounced QStash task ``regenerate_tenant_crons_task``)
      - Hourly fleet reconcile (``reconcile_tenant_crons_task``)
      - Container-start hook (``RuntimeContainerStartedView``) — fires
        immediately after a container reports readiness

    Returns a dict with ``{added, removed, unchanged, errors}`` for telemetry.
    No-op if the tenant is not on the Postgres-canonical flow (returns
    zeros without touching the gateway).
    """
    from django.utils import timezone as django_tz

    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
    from apps.cron.models import CronJob

    from .services import _extract_cron_jobs

    summary = {"added": 0, "removed": 0, "unchanged": 0, "errors": 0}

    if not getattr(tenant, "postgres_cron_canonical", False):
        logger.debug(
            "regenerate_tenant_crons: tenant %s not on new flow — skipping",
            tenant.id,
        )
        return summary

    desired_rows = list(CronJob.objects.filter(tenant=tenant, managed=True))
    desired_by_name: dict[str, dict] = {row.name: _row_to_cron_dict(row) for row in desired_rows}
    desired_rows_by_name: dict[str, CronJob] = {row.name: row for row in desired_rows}

    try:
        list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
    except GatewayError:
        logger.exception("regenerate_tenant_crons: cron.list failed for tenant %s", tenant.id)
        summary["errors"] += 1
        return summary

    current_jobs = _extract_cron_jobs(list_result) or []
    current_managed = {
        j.get("name", ""): j for j in current_jobs if isinstance(j, dict) and not _is_unmanaged_cron(j.get("name", ""))
    }

    to_add = [desired_by_name[n] for n in desired_by_name if n not in current_managed]
    to_remove = [current_managed[n] for n in current_managed if n not in desired_by_name]
    summary["unchanged"] = len(desired_by_name) - len(to_add)

    pushed_at = django_tz.now()

    for job in to_add:
        try:
            invoke_gateway_tool(tenant, "cron.add", {"job": job})
            summary["added"] += 1
            row = desired_rows_by_name.get(job["name"])
            if row is not None:
                CronJob.objects.filter(pk=row.pk).update(last_pushed_to_container_at=pushed_at)
        except GatewayError:
            logger.warning(
                "regenerate_tenant_crons: cron.add failed for %s on tenant %s",
                job["name"],
                tenant.id,
                exc_info=True,
            )
            summary["errors"] += 1

    for job in to_remove:
        job_id = job.get("id") or job.get("jobId", "")
        if not job_id:
            continue
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            summary["removed"] += 1
        except GatewayError:
            logger.warning(
                "regenerate_tenant_crons: cron.remove failed for %s on tenant %s",
                str(job_id)[:12],
                tenant.id,
                exc_info=True,
            )
            summary["errors"] += 1

    logger.info(
        "regenerate_tenant_crons: tenant %s — added=%d removed=%d unchanged=%d errors=%d",
        str(tenant.id)[:8],
        summary["added"],
        summary["removed"],
        summary["unchanged"],
        summary["errors"],
    )
    return summary


# Standalone helper for tests + ad-hoc calls. The signal-driven debounced
# wrapper lives in apps/orchestrator/tasks.py.
__all__ = ["regenerate_tenant_crons", "_is_unmanaged_cron", "_row_to_cron_dict"]
