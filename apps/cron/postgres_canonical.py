"""Postgres-canonical operations for the dashboard cron views.

When ``Tenant.postgres_cron_canonical=True``, the dashboard endpoints in
``apps/cron/tenant_views.py`` route through this module instead of calling
the OpenClaw gateway directly. Reads serve the ``CronJob`` table; writes
mutate it (signal-driven reconcile pushes to SQLite asynchronously).

Each helper returns a ``(payload, status_code)`` tuple so the view layer can
construct a ``Response`` without needing to know anything about the
Postgres-canonical internals.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.db import transaction
from rest_framework import status

from .models import CronJob, CronJobSource

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

_STRIP_FIELDS = {
    "id",
    "jobId",
    "createdAt",
    "state",
    "createdAtMs",
    "updatedAtMs",
    "nextRunAtMs",
    "runningAtMs",
}


def _row_to_job_dict(row: CronJob) -> dict:
    """Render a ``CronJob`` row in the gateway-shape dict the dashboard expects.

    Overlays the canonical ``name`` + ``enabled`` from the columns, strips
    gateway-internal fields, and tags ``foreground`` based on the Phase 2
    marker in the payload message (matches ``_filter_visible_jobs`` in
    ``tenant_views.py``).
    """
    from .tenant_views import _message_has_phase2_marker

    job = dict(row.data or {})
    for stripped in _STRIP_FIELDS:
        job.pop(stripped, None)
    job["name"] = row.name
    job["enabled"] = bool(row.enabled)

    payload = job.get("payload") or {}
    message = ""
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("text", "") or ""
    job["foreground"] = _message_has_phase2_marker(message)

    return job


def _classify_source(name: str) -> str:
    """Classify a cron job's source from its name."""
    from .tenant_views import HIDDEN_SYSTEM_CRONS

    if name in HIDDEN_SYSTEM_CRONS:
        return CronJobSource.SYSTEM
    if name.startswith("_fuel:"):
        # _fuel:{8-hex} == fuel session; _fuel:{plan_name} legacy → also system-ish
        suffix = name.removeprefix("_fuel:")
        if len(suffix) == 8 and all(c in "0123456789abcdef" for c in suffix.lower()):
            return CronJobSource.FUEL_SESSION
        return CronJobSource.SYSTEM
    if name.startswith("_sync:"):
        return CronJobSource.AGENT
    # Bare system cron names (Morning Briefing, Evening Check-in, etc.) —
    # assume system. Any user-created cron will be USER, not these.
    if name in {
        "Morning Briefing",
        "Evening Check-in",
        "Weekly Reflection",
        "Week Ahead Review",
        "Project Check-in",
        "Background Tasks",
        "Heartbeat Check-in",
    }:
        return CronJobSource.SYSTEM
    return CronJobSource.USER


# ─── Read ──────────────────────────────────────────────────────────────


def list_visible_jobs(tenant: Tenant) -> dict:
    """Return ``{"jobs": [...]}`` of visible (non-hidden) crons for the dashboard."""
    from .tenant_views import _is_hidden_cron

    rows = CronJob.objects.filter(tenant=tenant).order_by("name")
    jobs = [_row_to_job_dict(row) for row in rows if not _is_hidden_cron(row.name)]
    return {"jobs": jobs}


def get_job(tenant: Tenant, job_name: str) -> tuple[dict | None, int | None]:
    """Return the gateway-shape dict for a single job, or (None, 404)."""
    row = CronJob.objects.filter(tenant=tenant, name=job_name).first()
    if not row:
        return None, status.HTTP_404_NOT_FOUND
    return _row_to_job_dict(row), None


# ─── Writes ────────────────────────────────────────────────────────────


def create_job(tenant: Tenant, data: dict, *, max_visible: int = 10) -> tuple[dict, int]:
    """Create a CronJob row from a normalized job dict.

    Returns the rendered job dict + HTTP status. Enforces the visible-job
    cap and dup-name check from Postgres (no gateway call).
    """

    name = (data.get("name") or "").strip()
    if not name:
        return {"detail": "Job name is required."}, status.HTTP_400_BAD_REQUEST

    visible_count = (
        CronJob.objects.filter(tenant=tenant)
        .exclude(name__startswith="_sync:")
        .exclude(name__startswith="_fuel:")
        .exclude(name__in=["Background Tasks", "Heartbeat Check-in", "Project Check-in"])
        .count()
    )
    if visible_count >= max_visible:
        return (
            {
                "detail": (
                    f"Maximum of {max_visible} scheduled tasks reached. "
                    "Please delete an existing task before adding a new one."
                )
            },
            status.HTTP_409_CONFLICT,
        )

    if CronJob.objects.filter(tenant=tenant, name__iexact=name).exists():
        return (
            {
                "detail": (
                    f"A scheduled task named '{name}' already exists. "
                    "Please use a different name or edit the existing task."
                )
            },
            status.HTTP_409_CONFLICT,
        )

    enabled = bool(data.get("enabled", True))
    job_data = {k: v for k, v in data.items() if k not in _STRIP_FIELDS}

    row = CronJob.objects.create(
        tenant=tenant,
        name=name,
        data=job_data,
        source=_classify_source(name),
        managed=True,
        enabled=enabled,
    )
    # Signal fires post_save → debounced regen pushes to SQLite.
    return _row_to_job_dict(row), status.HTTP_201_CREATED


def update_job(tenant: Tenant, job_name: str, patch: dict) -> tuple[dict, int]:
    """Apply a patch to a CronJob row.

    ``patch`` is a partial dict in gateway shape (e.g. ``{"schedule": {...},
    "delivery": {...}, "enabled": True, "payload": {"message": "..."}}``).
    The patch is merged into ``row.data``; ``enabled`` is lifted to the
    column. Signal fires post_save.
    """
    row = CronJob.objects.filter(tenant=tenant, name=job_name).first()
    if not row:
        return {"detail": "Job not found."}, status.HTTP_404_NOT_FOUND

    data = dict(row.data or {})
    for key, value in patch.items():
        if key in _STRIP_FIELDS or key == "name":
            continue
        if key == "enabled":
            row.enabled = bool(value)
            continue
        data[key] = value

    row.data = data
    row.save()  # signal fires
    return _row_to_job_dict(row), status.HTTP_200_OK


def toggle_job(tenant: Tenant, job_name: str, enabled: bool) -> tuple[dict, int]:
    """Toggle a job's enabled flag."""
    row = CronJob.objects.filter(tenant=tenant, name=job_name).first()
    if not row:
        return {"detail": "Job not found."}, status.HTTP_404_NOT_FOUND
    row.enabled = bool(enabled)
    row.save(update_fields=["enabled", "updated_at"])  # signal fires
    return _row_to_job_dict(row), status.HTTP_200_OK


def delete_job(tenant: Tenant, job_name: str) -> tuple[dict | None, int]:
    """Delete a CronJob row. Signal fires post_delete."""
    row = CronJob.objects.filter(tenant=tenant, name=job_name).first()
    if not row:
        return {"detail": "Job not found."}, status.HTTP_404_NOT_FOUND
    row.delete()  # signal fires
    return None, status.HTTP_204_NO_CONTENT


def bulk_delete_jobs(tenant: Tenant, ids_or_names: list[str]) -> dict:
    """Bulk-delete by name (gateway IDs are also accepted but matched against name).

    Returns ``{"deleted": N, "errors": M, "results": [...]}`` matching the
    legacy bulk-delete view's response shape.
    """
    from .tenant_views import _is_hidden_cron

    seen: set[str] = set()
    unique: list[str] = []
    for raw in ids_or_names:
        if isinstance(raw, str) and raw not in seen:
            seen.add(raw)
            unique.append(raw)

    blocked = [n for n in unique if _is_hidden_cron(n)]
    if blocked:
        return {
            "_status": status.HTTP_403_FORBIDDEN,
            "detail": f"System tasks cannot be deleted: {', '.join(blocked)}",
        }

    if not unique:
        return {
            "_status": status.HTTP_400_BAD_REQUEST,
            "detail": "No valid job IDs provided.",
        }

    results: list[dict] = []
    errors: list[dict] = []

    with transaction.atomic():
        for name_or_id in unique:
            row = CronJob.objects.filter(tenant=tenant, name=name_or_id).first()
            if not row:
                row = CronJob.objects.filter(tenant=tenant, gateway_job_id=name_or_id).first()
            if not row:
                errors.append({"id": name_or_id, "deleted": False, "error": "not found"})
                continue
            row_name = row.name
            row.delete()  # signal fires per-row
            results.append({"id": row_name, "deleted": True})

    response_status = status.HTTP_200_OK
    if errors and not results:
        response_status = status.HTTP_404_NOT_FOUND
    elif errors:
        response_status = status.HTTP_207_MULTI_STATUS

    return {
        "_status": response_status,
        "deleted": len(results),
        "errors": len(errors),
        "results": results + errors,
    }


def bulk_update_foreground(tenant: Tenant, ids_or_names: list[str], foreground: bool) -> dict:
    """Bulk-toggle the Phase 2 wrap on a set of jobs.

    Re-wraps each job's payload.message via the existing helpers from
    ``tenant_views`` (idempotent strip+wrap).
    """
    from .tenant_views import (
        _is_hidden_cron,
        _strip_phase2_block,
        _wrap_message_with_phase2,
    )

    seen: set[str] = set()
    unique: list[str] = []
    for raw in ids_or_names:
        if isinstance(raw, str) and raw not in seen:
            seen.add(raw)
            unique.append(raw)

    blocked = [n for n in unique if _is_hidden_cron(n)]
    if blocked:
        return {
            "_status": status.HTTP_403_FORBIDDEN,
            "detail": f"System tasks cannot be modified: {', '.join(blocked)}",
        }

    if not unique:
        return {
            "_status": status.HTTP_400_BAD_REQUEST,
            "detail": "No valid job IDs provided.",
        }

    results: list[dict] = []
    errors: list[dict] = []

    with transaction.atomic():
        for name in unique:
            row = CronJob.objects.filter(tenant=tenant, name=name).first()
            if not row:
                errors.append({"id": name, "ok": False, "error": "not found"})
                continue
            data = dict(row.data or {})
            payload = data.get("payload") or {}
            if isinstance(payload, dict):
                base_message = payload.get("message", "") or payload.get("text", "")
                base_message = _strip_phase2_block(base_message)
                rewrapped = _wrap_message_with_phase2(base_message, row.name, foreground)
                data["payload"] = {**payload, "message": rewrapped}
                row.data = data
                row.save()  # signal fires
                results.append({"id": row.name, "ok": True, "foreground": foreground})
            else:
                errors.append({"id": row.name, "ok": False, "error": "no payload"})

    response_status = status.HTTP_200_OK
    if errors and not results:
        response_status = status.HTTP_400_BAD_REQUEST
    elif errors:
        response_status = status.HTTP_207_MULTI_STATUS

    return {
        "_status": response_status,
        "updated": len(results),
        "errors": len(errors),
        "results": results + errors,
    }


# Used by tests + service code that needs to seed Postgres directly.
def upsert_from_gateway_jobs(tenant: Tenant, raw_jobs: list[dict[str, Any]]) -> dict:
    """Replace a tenant's Postgres CronJob rows with the gateway's current set.

    Mirrors ``apps/cron/cache.py::upsert_jobs_to_cache`` but also writes the
    new ``source`` / ``managed`` / ``enabled`` columns. Used by the backfill
    command.

    Returns ``{"upserted": N, "removed": M}``.
    """
    by_name: dict[str, dict] = {}
    for job in raw_jobs:
        if not isinstance(job, dict):
            continue
        name = job.get("name") or ""
        if not name:
            continue
        prev = by_name.get(name)
        if prev is None or job.get("createdAt", "") > prev.get("createdAt", ""):
            by_name[name] = job

    desired_names = set(by_name)

    upserted = 0
    with transaction.atomic():
        existing = {cj.name: cj for cj in CronJob.objects.select_for_update().filter(tenant=tenant)}
        for name, job in by_name.items():
            gateway_job_id = str(job.get("id") or job.get("jobId") or "")[:64]
            enabled = bool(job.get("enabled", True))
            source = _classify_source(name)
            # _sync:* is unmanaged; reconciler ignores. Other agent-prefixes
            # could be added here as the surface evolves.
            managed = source != CronJobSource.AGENT

            row = existing.get(name)
            if row is None:
                CronJob.objects.create(
                    tenant=tenant,
                    name=name,
                    gateway_job_id=gateway_job_id,
                    data=job,
                    source=source,
                    managed=managed,
                    enabled=enabled,
                )
                upserted += 1
            else:
                row.gateway_job_id = gateway_job_id
                row.data = job
                row.source = source
                row.managed = managed
                row.enabled = enabled
                row.save()  # signal fires (idempotent — reconciler will see no diff)
                upserted += 1

        stale_names = set(existing) - desired_names
        removed = 0
        if stale_names:
            removed = CronJob.objects.filter(tenant=tenant, name__in=stale_names).count()
            CronJob.objects.filter(tenant=tenant, name__in=stale_names).delete()

    return {"upserted": upserted, "removed": removed}
