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


def _extract_list_page_metadata(
    list_result: object,
    *,
    list_size: int,
) -> tuple[int, bool]:
    inner = list_result.get("details", list_result) if isinstance(list_result, dict) else list_result
    if not isinstance(inner, dict):
        return list_size, False
    raw_total = inner.get("total")
    total = raw_total if type(raw_total) is int and raw_total >= list_size else list_size
    return total, inner.get("hasMore") is True


def _already_removed(exc: GatewayError) -> bool:
    if exc.status_code in {404, 409}:
        return True
    message = str(exc).lower()
    return "not found" in message or "no such" in message or "missing id" in message


def _sweep_ghost_jobs(
    tenant,
    jobs: list[dict],
    *,
    list_total: int | None = None,
    list_has_more: bool = False,
) -> dict[str, int]:
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
    failed = 0
    for job_id, job in candidates[:_GHOST_SWEEP_LIMIT]:
        for attempt in (1, 2):
            try:
                cron_remove(tenant, job_id=job_id)
            except GatewayError as exc:
                if attempt == 2 and _already_removed(exc):
                    swept += 1
                    break

                if attempt == 1:
                    try:
                        verify_result = invoke_gateway_tool(
                            tenant,
                            "cron.list",
                            {"includeDisabled": True},
                        )
                    except GatewayError as verify_exc:
                        logger.warning(
                            "cron_ghost_sweep_verify_failed tenant=%s job=%s name=%s "
                            "remove_status=%s list_status=%s attempt=1/2",
                            tenant.id,
                            job_id,
                            job.get("name") or "",
                            exc.status_code,
                            verify_exc.status_code,
                        )
                    else:
                        live_ids = {_live_job_id(live_job) for live_job in _extract_live_jobs(verify_result)}
                        if job_id not in live_ids:
                            swept += 1
                            logger.info(
                                "cron_ghost_sweep_remove_converged tenant=%s job=%s name=%s remove_status=%s",
                                tenant.id,
                                job_id,
                                job.get("name") or "",
                                exc.status_code,
                            )
                            break

                    logger.warning(
                        "cron_ghost_sweep_remove_retry tenant=%s job=%s name=%s status=%s attempt=1/2",
                        tenant.id,
                        job_id,
                        job.get("name") or "",
                        exc.status_code,
                    )
                    continue

                failed += 1
                logger.warning(
                    "cron_ghost_sweep_remove_failed tenant=%s job=%s name=%s status=%s attempt=%s/2",
                    tenant.id,
                    job_id,
                    job.get("name") or "",
                    exc.status_code,
                    attempt,
                    exc_info=True,
                )
                break
            else:
                swept += 1
                break

    summary = {
        "swept": swept,
        "failed": failed,
        "deferred": deferred,
        "skipped_future": skipped_future,
        "skipped_invalid": skipped_invalid,
        "list_size": len(jobs),
    }
    logger.info(
        "cron_ghost_sweep tenant=%s list_size=%s list_total=%s list_has_more=%s "
        "swept=%s failed=%s deferred=%s skipped_future=%s skipped_invalid=%s",
        tenant.id,
        len(jobs),
        list_total if list_total is not None else len(jobs),
        list_has_more,
        swept,
        failed,
        deferred,
        skipped_future,
        skipped_invalid,
    )
    return summary


def _live_job_id(job: dict) -> str:
    return str(job.get("id") or job.get("jobId") or "")


def _resync_gateway_job_ids(tenant, jobs: list[dict]) -> int:
    """Repair stale IDs for managed rows only when one live name match exists."""
    from apps.cron.models import CronJob

    live_ids = {_live_job_id(job) for job in jobs}
    live_ids.discard("")

    live_by_name: dict[str, list[dict]] = {}
    for job in jobs:
        name = job.get("name")
        if not isinstance(name, str) or not name or name.startswith("_sync:"):
            continue
        live_by_name.setdefault(name, []).append(job)

    resynced = 0
    for row in CronJob.objects.filter(tenant=tenant, managed=True):
        old_id = str(row.gateway_job_id or "")
        if old_id in live_ids:
            continue

        matches = live_by_name.get(row.name, [])
        if not matches:
            logger.info(
                "cron_gateway_id_resync tenant=%s row=%s old=%s matches=0",
                tenant.id,
                row.pk,
                old_id,
            )
            continue
        if len(matches) > 1:
            logger.warning(
                "cron_gateway_id_resync tenant=%s row=%s old=%s matches=%s",
                tenant.id,
                row.pk,
                old_id,
                len(matches),
            )
            continue

        new_id = _live_job_id(matches[0])
        if not new_id:
            logger.info(
                "cron_gateway_id_resync tenant=%s row=%s old=%s matches=1 missing_live_id=1",
                tenant.id,
                row.pk,
                old_id,
            )
            continue

        CronJob.objects.filter(pk=row.pk).update(gateway_job_id=new_id)
        resynced += 1
        logger.info(
            "cron_gateway_id_resync tenant=%s row=%s old=%s new=%s",
            tenant.id,
            row.pk,
            old_id,
            new_id,
        )

    return resynced


def run_post_reconcile_maintenance(tenant) -> dict[str, int]:
    """Fetch the live cron list and run gated container-start maintenance."""
    if not _ghost_sweep_enabled(tenant):
        return {
            "swept": 0,
            "failed": 0,
            "deferred": 0,
            "skipped_future": 0,
            "skipped_invalid": 0,
            "list_size": 0,
            "resynced": 0,
        }

    try:
        # OpenClaw 2026.5.28's public ``cron`` tool forwards only
        # includeDisabled/agentId to RPC ``cron.list``. Its daemon listPage
        # clamps the page to 200, while limit/offset supplied to this HTTP
        # tool are ignored. This is therefore the first <=200 jobs after the
        # tool's agent/enabled filters and nextRunAtMs sort, from the daemon's
        # loaded valid-job snapshot — not necessarily every row in jobs.json.
        list_result = invoke_gateway_tool(tenant, "cron.list", {"includeDisabled": True})
    except GatewayError:
        logger.warning(
            "cron_ghost_sweep_list_failed tenant=%s",
            tenant.id,
            exc_info=True,
        )
        return {
            "swept": 0,
            "failed": 0,
            "deferred": 0,
            "skipped_future": 0,
            "skipped_invalid": 0,
            "list_size": 0,
            "resynced": 0,
        }

    jobs = _extract_live_jobs(list_result)
    list_total, list_has_more = _extract_list_page_metadata(
        list_result,
        list_size=len(jobs),
    )
    summary = _sweep_ghost_jobs(
        tenant,
        jobs,
        list_total=list_total,
        list_has_more=list_has_more,
    )
    summary["resynced"] = _resync_gateway_job_ids(tenant, jobs)
    return summary
