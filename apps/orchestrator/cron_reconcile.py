"""Generalized Postgres-canonical cron reconciler.

This is the broad-surface generalization of ``apps/orchestrator/fuel_cron.py``.
While that module reconciles only ``_fuel:{8-hex}`` per-session crons derived
from ``Workout.scheduled_at``, this one reconciles the full set of managed
crons stored as ``apps/cron/models.CronJob`` rows: system schedules
(Morning Briefing, Heartbeat, etc.), user-created crons, and Fuel sessions
(once they're materialized into CronJob rows by the broader cutover).

Postgres ``CronJob`` rows where ``managed=True`` are the desired set.
``cron.list`` against the gateway gives current SQLite state. Diff by name;
``cron.add`` for missing, ``cron.remove`` for stale.

Unmanaged jobs — agent-created and self-cleaning — are explicitly skipped:
  * Name-prefix matches (``_sync:*`` — Phase-2 sync crons)
  * Schedule-kind matches (``kind:"at"`` — one-shot reminders that the
    gateway auto-deletes via ``deleteAfterRun=true`` after firing)

The reconciler also runs a janitor pass that reaps ``kind:"at"`` jobs
whose fire time is more than one hour in the past — covers the cases
where an agent crashed mid-fire or the container was hibernated through
the scheduled time.

The reconciler is gated by ``Tenant.postgres_cron_canonical``. While that
flag is False (cutover-day default), the legacy gateway-canonical paths
remain authoritative and this function is a no-op.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# Cron-name prefixes that the reconciler MUST NOT touch. These crons live
# only in OpenClaw's SQLite (the agent writes them via the cron tool) and
# self-clean. If we managed them, the reconciler would race the agent and
# remove them mid-flight.
#
#   * ``_sync:``  — Phase-2 sync crons, agent-written and self-cleaning.
#   * ``_fuel:``  — per-session Fuel crons. These are owned solely by
#     ``apps/orchestrator/fuel_cron.py:regenerate_fuel_crons`` (written via
#     ``cron.add`` with NO ``managed=True`` CronJob row backing them). If
#     this reconciler owned them, they'd be absent from ``desired_by_name``
#     and get removed on every pass — a destructive flapping race against
#     the fuel reconciler that just added them.
_UNMANAGED_PREFIXES: tuple[str, ...] = ("_sync:", "_fuel:")

# Schedule kinds that the reconciler treats as unmanaged. ``kind:"at"`` is
# a one-shot whose gateway-side default is ``deleteAfterRun=true`` — the
# job vanishes after firing. Reconciling against Postgres truth would race
# that auto-delete and remove the job before it fires.
_UNMANAGED_SCHEDULE_KINDS: frozenset[str] = frozenset({"at"})

# Grace period after an ``at`` job's fire time before the janitor reaps
# it. If the job is still in the gateway this long after its scheduled
# fire, either it fired and didn't auto-delete (gateway bug) or the
# container was down through the fire time. Either way it's stale.
_AT_CRON_REAP_GRACE_MS = 60 * 60 * 1000  # 1 hour

# Concurrent-``at``-cron caps. Three tiers:
#  * Soft (20) — enforced by the agent itself via its docs (see
#    ``templates/openclaw/docs/cron-management.md``). No backend action.
#  * Hard (50) — reconciler logs a ``PlatformIssueLog`` of severity
#    MEDIUM. No reaping; the user's queued intent is respected.
#  * Catastrophic (200) — reconciler reaps newest-first back to 200
#    and logs severity HIGH. Real users do not accumulate 200 pending
#    one-offs; this only fires on abuse.
# Real creation-time enforcement requires routing ``cron.add`` through
# Django (Phase 3 — wrapping plugin). Until then, this is the backstop.
_AT_CRON_HARD_CAP = 50
_AT_CRON_CATASTROPHIC_CAP = 200


def _is_unmanaged_cron(job: dict | str) -> bool:
    """Return True if the reconciler must not own this gateway-side cron.

    Accepts either a gateway job dict (preferred — lets us inspect
    ``schedule.kind``) or a bare name string (back-compat for callers
    that only have a name).
    """
    if isinstance(job, str):
        return not job or job.startswith(_UNMANAGED_PREFIXES)
    if not isinstance(job, dict):
        return True
    name = job.get("name") or ""
    if not name:
        return True
    if name.startswith(_UNMANAGED_PREFIXES):
        return True
    schedule = job.get("schedule")
    if isinstance(schedule, dict) and schedule.get("kind") in _UNMANAGED_SCHEDULE_KINDS:
        return True
    return False


def _at_cron_fire_ms(job: dict) -> int | None:
    """Return the fire time (epoch ms) for an ``at`` cron, or None.

    Prefers ``state.nextRunAtMs`` (the gateway's resolved value) and
    falls back to parsing ``schedule.at`` as ISO 8601.
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


def _is_past_due_at_cron(job: dict, now_ms: int, grace_ms: int = _AT_CRON_REAP_GRACE_MS) -> bool:
    """Whether an ``at`` cron is stale enough that the janitor should reap it."""
    if not isinstance(job, dict):
        return False
    schedule = job.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("kind") != "at":
        return False
    fire_ms = _at_cron_fire_ms(job)
    if fire_ms is None:
        return False
    return fire_ms + grace_ms < now_ms


def _pending_at_crons(jobs: list[dict], reaped_ids: set[str]) -> list[dict]:
    """Return ``at`` crons that are still pending (excludes those just reaped)."""
    return [
        j
        for j in jobs
        if isinstance(j, dict)
        and isinstance(j.get("schedule"), dict)
        and j["schedule"].get("kind") == "at"
        and str(j.get("id") or j.get("jobId") or "") not in reaped_ids
    ]


def _newest_first(jobs: list[dict]) -> list[dict]:
    """Sort gateway jobs newest-first by ``createdAtMs`` (missing → very old).

    Used for reap-newest selection when a tenant blows past the
    catastrophic cap: the burst of abuse is most likely the recently-added
    set, and reaping newest preserves the user's older queue.
    """
    return sorted(jobs, key=lambda j: j.get("createdAtMs") or 0, reverse=True)


def _log_at_cron_cap_breach(tenant: Tenant, *, severity: str, summary: str, detail: str) -> None:
    """Record a one-off cap breach, deduped to one unresolved log per tenant.

    Skips re-logging if an unresolved ``RATE_LIMIT`` entry already exists
    for this tenant — that record IS the "still ongoing" marker. After an
    operator resolves the entry, the next breach will create a fresh one.
    """
    try:
        from apps.platform_logs.models import PlatformIssueLog

        if PlatformIssueLog.objects.filter(
            tenant=tenant,
            category=PlatformIssueLog.Category.RATE_LIMIT,
            tool_name="cron.add",
            resolved=False,
        ).exists():
            return
        PlatformIssueLog.objects.create(
            tenant=tenant,
            category=PlatformIssueLog.Category.RATE_LIMIT,
            severity=severity,
            tool_name="cron.add",
            summary=summary,
            detail=detail,
        )
    except Exception:
        # Telemetry must never break the reconciler. Best-effort only.
        logger.exception(
            "regenerate_tenant_crons: failed to record at-cron cap breach for tenant %s",
            tenant.id,
        )


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

    from .cron_drift import job_drift
    from .services import _extract_cron_jobs

    summary = {
        "added": 0,
        "removed": 0,
        "recreated": 0,
        "unchanged": 0,
        "errors": 0,
        "stuck_reaped": 0,
        "cap_reaped": 0,
        "at_pending": 0,
        "duplicates_reaped": 0,
    }

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

    # Pre-pass: reap same-name duplicates from the gateway. The historical
    # reconciler diffed by name only — ``current_managed = {name: job}``
    # silently collapses two jobs with the same name into one dict entry,
    # so duplicates are invisible to the add/remove diff and accumulate
    # indefinitely. Sources of dups include pre-Postgres-canonical SQLite
    # state (the cutover backfilled Postgres but didn't clean OC's runtime),
    # agent hallucinations during Phase 2 sync, or race conditions between
    # parallel ``cron.add`` paths during boot. Keep the newest by
    # ``createdAtMs`` (an agent-created refresh is more likely current than
    # a stale 2026-04 entry) and remove the rest before the main diff so
    # ``current_managed`` is clean for the to_add/to_remove logic below.
    by_name: dict[str, list[dict]] = {}
    for j in current_jobs:
        if not isinstance(j, dict) or _is_unmanaged_cron(j):
            continue
        name = j.get("name") or ""
        if name:
            by_name.setdefault(name, []).append(j)

    dup_ids_to_remove: list[tuple[str, str]] = []  # (name, gateway_id) pairs for logging
    for name, jobs in by_name.items():
        if len(jobs) <= 1:
            continue
        # Sort newest-first by createdAtMs (0 for missing → treated as oldest)
        jobs.sort(key=lambda j: j.get("createdAtMs") or 0, reverse=True)
        for dupe in jobs[1:]:
            dup_id = dupe.get("id") or dupe.get("jobId") or ""
            if dup_id:
                dup_ids_to_remove.append((name, str(dup_id)))

    if dup_ids_to_remove:
        reaped_dup_ids: set[str] = set()
        for name, dup_id in dup_ids_to_remove:
            try:
                invoke_gateway_tool(tenant, "cron.remove", {"jobId": dup_id})
                summary["duplicates_reaped"] += 1
                reaped_dup_ids.add(dup_id)
                logger.info(
                    "regenerate_tenant_crons: reaped duplicate '%s' (id %s, older copy) for tenant %s",
                    name,
                    dup_id[:12],
                    tenant.id,
                )
            except GatewayError:
                logger.warning(
                    "regenerate_tenant_crons: cron.remove failed for duplicate %s on tenant %s",
                    dup_id[:12],
                    tenant.id,
                    exc_info=True,
                )
                summary["errors"] += 1
        # Rebuild current_jobs minus the just-removed dupes so the rest of
        # the function sees a clean view (and the at-cron janitor below
        # doesn't double-reap something we already removed).
        current_jobs = [
            j
            for j in current_jobs
            if isinstance(j, dict) and (j.get("id") or j.get("jobId") or "") not in reaped_dup_ids
        ]

    current_managed = {j.get("name", ""): j for j in current_jobs if isinstance(j, dict) and not _is_unmanaged_cron(j)}

    to_add = [desired_by_name[n] for n in desired_by_name if n not in current_managed]
    to_remove = [current_managed[n] for n in current_managed if n not in desired_by_name]

    # Payload-aware drift detection. Names that exist in both sides but with
    # drifted payload/schedule must be recreated — OpenClaw rejects payload
    # patches via ``cron.update`` for the legacy systemEvent→agentTurn shape
    # and we'd rather always converge than have a partial update path. The
    # ``cron.remove``+``cron.add`` pattern is what ``update_system_cron_prompts``
    # used pre-refactor; we move it here so the postgres-canonical reconciler
    # is the single writer for system cron payload state.
    #
    # The dimensions checked (model, kind, message body after date strip,
    # schedule, enabled) all originate from the seed or user-customized
    # postgres state; any difference in OC is by definition stale. See
    # ``apps/orchestrator/cron_drift.py`` for the per-field rules and the
    # ``project_cron_payload_drift_extended.md`` memory for the canary
    # 2026-05-12 incident this generalizes.
    to_recreate: list[tuple[str, dict, dict]] = []  # (name, existing, desired)
    for name in desired_by_name.keys() & current_managed.keys():
        drift = job_drift(current_managed[name], desired_by_name[name])
        if drift:
            to_recreate.append((name, current_managed[name], desired_by_name[name]))
            logger.info(
                "regenerate_tenant_crons: drift on '%s' for tenant %s — fields=%s",
                name,
                tenant.id,
                ",".join(drift),
            )

    summary["unchanged"] = len(desired_by_name) - len(to_add) - len(to_recreate)

    # Janitor: reap ``kind:"at"`` jobs whose fire time is more than the grace
    # window in the past. Covers agent-crashed-mid-fire and
    # container-down-through-fire-time cases that auto-delete misses.
    now_ms = int(django_tz.now().timestamp() * 1000)
    stuck_at_jobs = [j for j in current_jobs if isinstance(j, dict) and _is_past_due_at_cron(j, now_ms)]

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

    for name, existing, desired in to_recreate:
        gateway_job_id = existing.get("id") or existing.get("jobId") or ""
        try:
            if gateway_job_id:
                invoke_gateway_tool(tenant, "cron.remove", {"jobId": gateway_job_id})
            invoke_gateway_tool(tenant, "cron.add", {"job": desired})
            summary["recreated"] += 1
            row = desired_rows_by_name.get(name)
            if row is not None:
                CronJob.objects.filter(pk=row.pk).update(last_pushed_to_container_at=pushed_at)
        except GatewayError:
            logger.warning(
                "regenerate_tenant_crons: recreate failed for '%s' on tenant %s",
                name,
                tenant.id,
                exc_info=True,
            )
            summary["errors"] += 1

    reaped_ids: set[str] = set()
    for job in stuck_at_jobs:
        job_id = job.get("id") or job.get("jobId", "")
        if not job_id:
            continue
        try:
            invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
            summary["stuck_reaped"] += 1
            reaped_ids.add(str(job_id))
        except GatewayError:
            logger.warning(
                "regenerate_tenant_crons: stuck-at janitor cron.remove failed for %s on tenant %s",
                str(job_id)[:12],
                tenant.id,
                exc_info=True,
            )
            summary["errors"] += 1

    # Cap enforcement on concurrent ``at`` jobs.
    pending_at = _pending_at_crons(current_jobs, reaped_ids)
    summary["at_pending"] = len(pending_at)

    if len(pending_at) >= _AT_CRON_CATASTROPHIC_CAP:
        # Reap newest-first back to the cap. Real users do not stack 200+
        # pending one-offs — at this point we're handling abuse, not intent.
        excess = _newest_first(pending_at)[: len(pending_at) - _AT_CRON_CATASTROPHIC_CAP]
        for job in excess:
            job_id = job.get("id") or job.get("jobId", "")
            if not job_id:
                continue
            try:
                invoke_gateway_tool(tenant, "cron.remove", {"jobId": job_id})
                summary["cap_reaped"] += 1
                reaped_ids.add(str(job_id))
            except GatewayError:
                logger.warning(
                    "regenerate_tenant_crons: cap-reap cron.remove failed for %s on tenant %s",
                    str(job_id)[:12],
                    tenant.id,
                    exc_info=True,
                )
                summary["errors"] += 1
        _log_at_cron_cap_breach(
            tenant,
            severity="high",
            summary=f"Catastrophic cap: {len(pending_at)} pending at-crons (cap {_AT_CRON_CATASTROPHIC_CAP})",
            detail=(
                f"Reaped {summary['cap_reaped']} newest at-crons to enforce the "
                f"catastrophic cap. Investigate whether the agent has been prompted to "
                f"create unbounded one-offs."
            ),
        )
    elif len(pending_at) > _AT_CRON_HARD_CAP:
        _log_at_cron_cap_breach(
            tenant,
            severity="medium",
            summary=f"Hard cap exceeded: {len(pending_at)} pending at-crons (cap {_AT_CRON_HARD_CAP})",
            detail=(
                f"Tenant has {len(pending_at)} concurrent kind:'at' crons, above the "
                f"documented soft cap of 20 and the hard cap of {_AT_CRON_HARD_CAP}. "
                f"No reaping — user queue preserved. Resolve this log once cleaned up."
            ),
        )

    logger.info(
        "regenerate_tenant_crons: tenant %s — added=%d removed=%d recreated=%d unchanged=%d "
        "stuck_reaped=%d cap_reaped=%d at_pending=%d errors=%d",
        str(tenant.id)[:8],
        summary["added"],
        summary["removed"],
        summary["recreated"],
        summary["unchanged"],
        summary["stuck_reaped"],
        summary.get("cap_reaped", 0),
        summary["at_pending"],
        summary["errors"],
    )
    return summary


__all__ = [
    "regenerate_tenant_crons",
    "_is_unmanaged_cron",
    "_is_past_due_at_cron",
    "_pending_at_crons",
    "_row_to_cron_dict",
]
