"""Postgres cache for OpenClaw cron jobs.

Helpers for the Phase 1 read-fallback path. The gateway is the source of
truth — these helpers populate the cache from successful ``cron.list``
responses and read from it when the container is unreachable.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from .gateway_client import GatewayError
from .models import CronJob

logger = logging.getLogger(__name__)


# HTTP status codes from the Azure ingress / gateway that indicate the
# container itself is unreachable (vs a real application error).
_UNAVAILABLE_STATUS_CODES = frozenset({404, 502, 503, 504})

# Substrings in the gateway error message that fingerprint Azure's
# "Container App - Unavailable" splash page.
_UNAVAILABLE_BODY_HINTS = (
    "Container App - Unavailable",
    "Container App is not available",
)


def is_container_unavailable_error(exc: GatewayError) -> bool:
    """Whether a GatewayError suggests the container is down vs a real app error.

    Used by the dashboard read path to decide between falling back to the
    Postgres cache and surfacing the error.
    """
    code = getattr(exc, "status_code", None)
    if code is not None and code in _UNAVAILABLE_STATUS_CODES:
        return True
    if code is None:
        # Connection errors / timeouts raise GatewayError without a status code.
        return True
    msg = str(exc)
    return any(hint in msg for hint in _UNAVAILABLE_BODY_HINTS)


def upsert_jobs_to_cache(tenant, jobs: list[dict[str, Any]]) -> None:
    """Replace the tenant's cached cron jobs with the gateway's current set.

    Deduplicates by ``name`` (newest ``createdAt`` wins) — matches the
    behaviour of the legacy ``cron_jobs_snapshot`` writer. The list is
    expected to be the raw gateway response, not the user-filtered view.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = job.get("name") or ""
        if not name:
            continue
        prev = by_name.get(name)
        if prev is None or job.get("createdAt", "") > prev.get("createdAt", ""):
            by_name[name] = job

    now = timezone.now()
    desired_names = set(by_name)

    with transaction.atomic():
        existing = {cj.name: cj for cj in CronJob.objects.select_for_update().filter(tenant=tenant)}
        for name, job in by_name.items():
            gateway_job_id = str(job.get("id") or job.get("jobId") or "")[:64]
            row = existing.get(name)
            if row is None:
                CronJob.objects.create(
                    tenant=tenant,
                    name=name,
                    gateway_job_id=gateway_job_id,
                    data=job,
                    last_synced_at=now,
                )
            else:
                row.gateway_job_id = gateway_job_id
                row.data = job
                row.last_synced_at = now
                row.save(update_fields=["gateway_job_id", "data", "last_synced_at", "updated_at"])

        stale_names = set(existing) - desired_names
        if stale_names:
            CronJob.objects.filter(tenant=tenant, name__in=stale_names).delete()


def read_jobs_from_cache(tenant) -> list[dict[str, Any]]:
    """Return the cached gateway-shape job list for a tenant.

    Falls back to the legacy ``Tenant.cron_jobs_snapshot`` JSONField if the
    table has no rows for this tenant — covers the period before the first
    successful ``cron.list`` populates the new cache.
    """
    rows = list(CronJob.objects.filter(tenant=tenant).order_by("name"))
    if rows:
        return [row.data for row in rows if isinstance(row.data, dict)]

    snapshot = getattr(tenant, "cron_jobs_snapshot", None)
    if isinstance(snapshot, dict):
        legacy_jobs = snapshot.get("jobs")
        if isinstance(legacy_jobs, list):
            return [j for j in legacy_jobs if isinstance(j, dict)]
    return []
