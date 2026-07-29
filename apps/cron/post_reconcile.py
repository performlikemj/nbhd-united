"""Container-start cron maintenance that runs after the normal reconciler."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Literal

from django.conf import settings

from apps.common.tenant_tz import tenant_today
from apps.cron.gateway_client import GatewayError, cron_remove, invoke_gateway_tool

logger = logging.getLogger(__name__)

_DATED_SYNC_EXPR = re.compile(r"^\d+ \d+ \d+ \d+ \*$")
_GHOST_REAP_GRACE = timedelta(days=2)
_YEAR_WRAP_FUTURE_DAYS = 180
_GHOST_SWEEP_LIMIT = 100

GhostDisposition = Literal["remove", "future", "invalid", "keep"]


def _ghost_sweep_enabled(tenant) -> bool:
    configured = str(getattr(settings, "CRON_GHOST_SWEEP_TENANTS", "") or "").strip()
    if not configured:
        return False
    if configured == "*":
        return True
    return str(tenant.id) in {value.strip() for value in configured.split(",") if value.strip()}


def _dated_sync_disposition(job: object, *, today: date) -> GhostDisposition:
    """Classify one live job under the ratified dated ``_sync:`` sweep rule."""
    if not isinstance(job, dict):
        return "keep"
    name = job.get("name")
    schedule = job.get("schedule")
    if not isinstance(name, str) or not name.startswith("_sync:"):
        return "keep"
    if not isinstance(schedule, dict) or schedule.get("kind") != "cron":
        return "keep"
    expr = schedule.get("expr")
    if not isinstance(expr, str) or _DATED_SYNC_EXPR.fullmatch(expr) is None:
        return "keep"

    _, _, day_text, month_text, _ = expr.split()
    day = int(day_text)
    month = int(month_text)
    try:
        fire_date = date(today.year, month, day)
    except ValueError:
        return "invalid"

    if fire_date > today:
        if (fire_date - today).days <= _YEAR_WRAP_FUTURE_DAYS:
            return "future"
        try:
            fire_date = date(today.year - 1, month, day)
        except ValueError:
            return "invalid"

    if today - fire_date > _GHOST_REAP_GRACE:
        return "remove"
    return "keep"


def _extract_live_jobs(list_result: object) -> list[dict]:
    inner = list_result.get("details", list_result) if isinstance(list_result, dict) else list_result
    if isinstance(inner, dict):
        jobs = inner.get("jobs", [])
    elif isinstance(inner, list):
        jobs = inner
    else:
        jobs = []
    if not isinstance(jobs, list):
        return []
    return [job for job in jobs if isinstance(job, dict)]


def _already_removed(exc: GatewayError) -> bool:
    if exc.status_code in {404, 409}:
        return True
    message = str(exc).lower()
    return "not found" in message or "no such" in message or "missing id" in message


def _sweep_ghost_jobs(tenant, jobs: list[dict]) -> dict[str, int]:
    today = tenant_today(tenant)
    candidates: list[tuple[str, dict]] = []
    skipped_future = 0
    skipped_invalid = 0

    for job in jobs:
        disposition = _dated_sync_disposition(job, today=today)
        if disposition == "future":
            skipped_future += 1
            continue
        if disposition == "invalid":
            skipped_invalid += 1
            logger.warning(
                "cron_ghost_sweep_invalid tenant=%s job=%s expr=%s",
                tenant.id,
                job.get("id") or job.get("jobId") or "",
                (job.get("schedule") or {}).get("expr", ""),
            )
            continue
        if disposition != "remove":
            continue
        job_id = str(job.get("id") or job.get("jobId") or "")
        if not job_id:
            logger.warning(
                "cron_ghost_sweep_missing_id tenant=%s name=%s",
                tenant.id,
                job.get("name") or "",
            )
            continue
        candidates.append((job_id, job))

    deferred = max(0, len(candidates) - _GHOST_SWEEP_LIMIT)
    swept = 0
    for job_id, job in candidates[:_GHOST_SWEEP_LIMIT]:
        try:
            cron_remove(tenant, job_id)
        except GatewayError as exc:
            if _already_removed(exc):
                swept += 1
                continue
            logger.warning(
                "cron_ghost_sweep_remove_failed tenant=%s job=%s name=%s",
                tenant.id,
                job_id,
                job.get("name") or "",
                exc_info=True,
            )
        else:
            swept += 1

    summary = {
        "swept": swept,
        "deferred": deferred,
        "skipped_future": skipped_future,
        "skipped_invalid": skipped_invalid,
    }
    logger.info(
        "cron_ghost_sweep tenant=%s swept=%s deferred=%s skipped_future=%s skipped_invalid=%s",
        tenant.id,
        swept,
        deferred,
        skipped_future,
        skipped_invalid,
    )
    return summary


def run_post_reconcile_maintenance(tenant) -> dict[str, int]:
    """Fetch the live cron list and run gated container-start maintenance."""
    if not _ghost_sweep_enabled(tenant):
        return {
            "swept": 0,
            "deferred": 0,
            "skipped_future": 0,
            "skipped_invalid": 0,
        }

    try:
        list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
    except GatewayError:
        logger.warning(
            "cron_ghost_sweep_list_failed tenant=%s",
            tenant.id,
            exc_info=True,
        )
        return {
            "swept": 0,
            "deferred": 0,
            "skipped_future": 0,
            "skipped_invalid": 0,
        }

    return _sweep_ghost_jobs(tenant, _extract_live_jobs(list_result))
