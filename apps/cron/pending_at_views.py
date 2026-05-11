"""Tenant-facing REST API for pending one-off (``kind:"at"``) crons.

These crons are *transients* — agent-created in chat, auto-deleted after
firing. They live only in the gateway's SQLite, never in Postgres, so this
view path is gateway-only regardless of ``Tenant.postgres_cron_canonical``.

Kept separate from ``tenant_views.CronJobListCreateView`` so the
canonical-tenant recurring-task read stays fast and Postgres-only — a
gateway hiccup degrades pending reminders without blanking the user's
Scheduled Tasks card.
"""

from __future__ import annotations

import logging
from datetime import datetime

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .cache import is_container_unavailable_error, read_jobs_from_cache
from .gateway_client import GatewayError, invoke_gateway_tool
from .tenant_views import _get_tenant_for_user, _require_active_tenant

logger = logging.getLogger(__name__)

# Mirrors the documented soft cap in templates/openclaw/docs/cron-management.md.
# The frontend uses this to render the "N of 20" badge — server-published so
# the cap moves in one place if we ever change it.
AT_CRON_SOFT_CAP = 20


def _at_fires_at_ms(job: dict) -> int | None:
    """Return the epoch-ms fire time of an ``at`` job, or None.

    Prefers ``state.nextRunAtMs`` (the gateway's resolved value), falls
    back to parsing ``schedule.at`` as ISO 8601.
    """
    state = job.get("state")
    if isinstance(state, dict):
        next_run = state.get("nextRunAtMs")
        if isinstance(next_run, int):
            return next_run
    schedule = job.get("schedule")
    if isinstance(schedule, dict):
        at_value = schedule.get("at")
        if isinstance(at_value, str):
            try:
                parsed = datetime.fromisoformat(at_value.replace("Z", "+00:00"))
                return int(parsed.timestamp() * 1000)
            except ValueError:
                return None
    return None


def _extract_at_jobs(raw_jobs: list[dict]) -> list[dict]:
    """Filter a raw ``cron.list`` response to pending ``kind:"at"`` jobs.

    Decorates each job with a computed ``firesAtMs`` and trims the noisy
    gateway-internal fields the frontend doesn't need. Sorted by fire time
    ascending (next-to-fire first).
    """
    out: list[dict] = []
    for job in raw_jobs:
        if not isinstance(job, dict):
            continue
        schedule = job.get("schedule")
        if not isinstance(schedule, dict) or schedule.get("kind") != "at":
            continue
        if job.get("enabled") is False:
            # The gateway's auto-delete-on-success only fires for enabled jobs.
            # A disabled at-job is functionally invisible — hide from the UI.
            continue
        fires_at_ms = _at_fires_at_ms(job)
        out.append(
            {
                "jobId": job.get("id") or job.get("jobId"),
                "name": job.get("name") or "",
                "firesAtMs": fires_at_ms,
                "schedule": schedule,
                "payload": job.get("payload") or {},
                "delivery": job.get("delivery") or {},
            }
        )
    out.sort(key=lambda j: j.get("firesAtMs") or 0)
    return out


class PendingAtCronView(APIView):
    """GET ``/api/v1/cron-jobs/pending-at/`` — list pending one-off reminders.

    Always reads from the gateway. Falls back to the Postgres cache when
    the container is hibernated or temporarily unreachable. Returns:

        {"jobs": [{jobId, name, firesAtMs, schedule, payload, delivery}, ...],
         "soft_cap": 20,
         "stale": false}

    ``stale=true`` indicates the response was served from the cache rather
    than a live ``cron.list`` — the frontend renders a banner.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        tenant = _get_tenant_for_user(request.user)

        # Hibernated tenants: gateway is down, serve from snapshot.
        if tenant.hibernated_at:
            jobs = _extract_at_jobs(read_jobs_from_cache(tenant))
            return Response({"jobs": jobs, "soft_cap": AT_CRON_SOFT_CAP, "stale": True})

        try:
            _require_active_tenant(tenant)
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        try:
            result = invoke_gateway_tool(tenant, "cron.list", {})
        except GatewayError as exc:
            if is_container_unavailable_error(exc):
                logger.info(
                    "pending-at cron.list gateway unavailable for tenant %s — serving cached",
                    tenant.id,
                )
                jobs = _extract_at_jobs(read_jobs_from_cache(tenant))
                return Response({"jobs": jobs, "soft_cap": AT_CRON_SOFT_CAP, "stale": True})
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        from apps.orchestrator.services import _extract_cron_jobs

        raw_jobs = _extract_cron_jobs(result) or []
        jobs = _extract_at_jobs(raw_jobs)
        return Response({"jobs": jobs, "soft_cap": AT_CRON_SOFT_CAP, "stale": False})


class PendingAtCronCancelView(APIView):
    """DELETE ``/api/v1/cron-jobs/pending-at/<name>/`` — cancel a pending reminder.

    Always routes through the gateway (no Postgres path). Idempotent: a
    missing job returns 404 but does not raise.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, name: str):
        tenant = _get_tenant_for_user(request.user)
        try:
            _require_active_tenant(tenant)
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        # Look up the job to verify it's an at-kind cron before we delete.
        # Stops a stray DELETE from yanking a recurring task.
        try:
            list_result = invoke_gateway_tool(tenant, "cron.list", {})
        except GatewayError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        from apps.orchestrator.services import _extract_cron_jobs

        target = None
        for job in _extract_cron_jobs(list_result) or []:
            if (
                isinstance(job, dict)
                and job.get("name") == name
                and isinstance(job.get("schedule"), dict)
                and job["schedule"].get("kind") == "at"
            ):
                target = job
                break
        if target is None:
            return Response({"detail": "Pending reminder not found."}, status=status.HTTP_404_NOT_FOUND)

        job_id = target.get("id") or target.get("jobId") or name
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
        except GatewayError as exc:
            msg = str(exc).lower()
            if "not found" in msg or "no such" in msg:
                return Response(status=status.HTTP_204_NO_CONTENT)
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(status=status.HTTP_204_NO_CONTENT)
