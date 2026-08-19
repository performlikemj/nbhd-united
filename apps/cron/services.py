"""Service layer for typed cron creation + freeform escape-hatch.

Called by runtime endpoints (agent-facing) and tenant views (console-facing).
All business logic lives here; views are thin adapters that parse a request
and call into this module.

Two distinct creation paths:

  create_typed_cron(...)    — for typed-pattern crons. The agent's
                              ``nbhd_cron_create_*`` tools and the future
                              console "Create" form land here. Payload is
                              validated against the pattern's Pydantic schema;
                              the pre_save signal derives ``data`` from
                              pattern + typed_payload.

  create_freeform_cron(...) — explicit user opt-in to an unvalidated cron via
                              the console UI's "Create freeform (advanced)"
                              flow. Caller must pass ``user_confirmed_at``
                              (the DB CHECK constraint rejects the row
                              otherwise). NEVER called from agent paths.

One-off (``kind:"at"``) typed crons are pushed to OpenClaw immediately and
marked ``managed=False`` so the reconciler leaves them alone (OC auto-deletes
them after fire). Recurring crons (``kind:"cron"`` / ``kind:"every"``) land
in Postgres and the existing signal-triggered reconciler debounces a push to
OC.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import IntegrityError, transaction

from apps.common.tenant_tz import tenant_tz_name
from apps.cron.models import CronCreationPath, CronJob, CronJobSource, CronPattern
from apps.cron.patterns import get_handler
from apps.cron.schedule_validation import (
    ScheduleValidationError,
    normalize_schedule,
)
from apps.platform_logs.telemetry import emit_tool_event
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

# ── task_hygiene seeding ────────────────────────────────────────────────────
# The weekly proactive cleanup turn. Its name is load-bearing in three places:
# the (tenant, name) uniqueness constraint, the idempotency check below, and
# the ``refresh_system_cron_rows_from_seed`` reaper (which skips typed rows —
# this row is NOT in ``build_cron_seed_jobs``, so without that skip the reaper
# would delete it on the next config apply).
TASK_HYGIENE_CRON_NAME = "Task Hygiene"

# Sunday 18:30 in the tenant's own timezone, clear of every other Sunday-evening
# sender: Gravity Weekly Check-in at 19:00, Weekly Reflection at 20:00, and — the
# reason for the :30 rather than a round 18:00 — the heartbeat, whose expression
# is ``0 {heartbeat_start_hour} * * *``. A tenant with heartbeat_start_hour == 18
# would have had both land on the same minute every Sunday.
TASK_HYGIENE_CRON_EXPR = "30 18 * * 0"


class TypedCronError(Exception):
    """Validation or creation failure for a typed cron."""

    def __init__(self, message: str, *, code: str = "invalid"):
        super().__init__(message)
        self.code = code


class CronNameConflictError(TypedCronError):
    """An ENABLED cron with the same (tenant, name) already exists.

    The uniqueness constraint is scoped to ``enabled=True`` (apps/cron/models.py),
    so a spent one-shot that has been retired by ``expire_finished_at_crons_task``
    no longer conflicts. The message is relayed verbatim to the agent in the 409
    body (apps/integrations/runtime_views.py), so it must be actionable: it is the
    only guidance the model gets at the moment it needs to recover.
    """

    def __init__(self, name: str):
        super().__init__(
            f"An active cron named {name!r} already exists for this tenant — "
            f"choose a different name, or update or disable the existing one first.",
            code="name_conflict",
        )
        self.name = name


def _is_at_schedule(schedule: dict[str, Any]) -> bool:
    return isinstance(schedule, dict) and schedule.get("kind") == "at"


def create_typed_cron(
    *,
    tenant: Tenant,
    pattern: str,
    typed_payload: dict[str, Any],
    name: str,
    schedule: dict[str, Any],
    source: str = CronJobSource.USER,
) -> CronJob:
    """Create a typed cron and (for at-kind) push to OC immediately.

    Raises:
        TypedCronError: payload validation, unknown pattern, invalid schedule.
        CronNameConflictError: (tenant, name) already exists.
    """
    name = (name or "").strip()
    if not name:
        raise TypedCronError("name is required", code="invalid_name")
    if pattern not in CronPattern.values:
        raise TypedCronError(
            f"pattern must be one of {list(CronPattern.values)}; got {pattern!r}",
            code="invalid_pattern",
        )
    tool_name = f"cron-create-{pattern}"
    submitted_kind = schedule.get("kind") if isinstance(schedule, dict) else None
    try:
        schedule, normalizations = normalize_schedule(schedule, tz_name=tenant_tz_name(tenant))
    except ScheduleValidationError as exc:
        # The reason code is the whole point of this event: "cron creation is
        # rejected" is not actionable, "the model keeps sending everyMs in
        # seconds" is.
        emit_tool_event(
            tool_name=tool_name,
            outcome="rejected",
            namespace="cron",
            tenant_id=tenant.id,
            reason_code=exc.code,
            detail={"schedule_kind": submitted_kind, "pattern": pattern},
        )
        raise TypedCronError(str(exc), code=exc.code) from exc

    for reason in normalizations:
        emit_tool_event(
            tool_name=tool_name,
            outcome="normalized",
            namespace="cron",
            tenant_id=tenant.id,
            reason_code=reason,
            detail={"schedule_kind": submitted_kind, "pattern": pattern},
        )

    handler = get_handler(pattern)
    # Construct + validate the typed payload up front so we surface a clean
    # error to the caller before any DB writes. The pre_save signal will
    # re-validate via the same handler.
    handler.validate_payload(typed_payload)

    managed = not _is_at_schedule(schedule)

    try:
        with transaction.atomic():
            cron = CronJob(
                tenant=tenant,
                name=name,
                source=source,
                managed=managed,
                enabled=True,
                pattern=pattern,
                typed_payload=typed_payload,
                creation_path=CronCreationPath.TYPED,
                # Seed data with the schedule so the pre_save signal can build
                # the full OC dict around it.
                data={"schedule": schedule},
            )
            cron.save()
    except IntegrityError as exc:
        if "cron_unique_tenant_name" in str(exc):
            raise CronNameConflictError(name) from exc
        raise

    if not managed:
        _push_at_cron_immediately(tenant, cron)

    return cron


def create_freeform_cron(
    *,
    tenant: Tenant,
    name: str,
    data: dict[str, Any],
    user_confirmed_at,
    source: str = CronJobSource.USER,
) -> CronJob:
    """Create a freeform (unvalidated) cron via the console escape hatch.

    The caller MUST pass ``user_confirmed_at`` — a non-null timestamp
    indicating the user explicitly accepted the lack of validation. The DB
    CHECK constraint enforces this independently.

    NEVER call this from agent paths — the agent's surface has no path
    to a freeform cron by design.
    """
    name = (name or "").strip()
    if not name:
        raise TypedCronError("name is required", code="invalid_name")
    if user_confirmed_at is None:
        raise TypedCronError(
            "Freeform crons require user_confirmed_at — explicit user opt-in.",
            code="missing_confirmation",
        )
    schedule = (data or {}).get("schedule")
    if not isinstance(schedule, dict):
        raise TypedCronError(
            "data.schedule is required for freeform crons",
            code="invalid_data",
        )

    managed = not _is_at_schedule(schedule)

    try:
        with transaction.atomic():
            cron = CronJob(
                tenant=tenant,
                name=name,
                source=source,
                managed=managed,
                enabled=True,
                pattern=None,
                typed_payload={},
                creation_path=CronCreationPath.FREEFORM,
                user_confirmed_at=user_confirmed_at,
                data=data,
            )
            cron.save()
    except IntegrityError as exc:
        if "cron_unique_tenant_name" in str(exc):
            raise CronNameConflictError(name) from exc
        raise

    if not managed:
        _push_at_cron_immediately(tenant, cron)

    return cron


def _push_at_cron_immediately(tenant: Tenant, cron: CronJob) -> None:
    """For one-shot (at-kind) crons: push to OC right now, skipping the reconciler.

    Why: ``apps/orchestrator/cron_reconcile.py`` explicitly skips
    ``kind:'at'`` jobs because OC auto-sets ``deleteAfterRun=true`` for
    them and reconciling would race the auto-delete. So one-offs need
    an immediate push at create time; after that, OC owns the lifecycle
    and our ``cron_changed`` hook learns of the fire/delete.
    """
    from apps.cron.gateway_client import GatewayError, invoke_gateway_tool

    try:
        result = invoke_gateway_tool(tenant, "cron.add", {"job": cron.data})
    except GatewayError:
        # Surface to caller so the agent / UI can decide what to do.
        logger.exception(
            "Immediate at-cron push failed (tenant=%s cron=%s)",
            str(tenant.id)[:8],
            cron.name,
        )
        raise

    # Stamp the gateway's job id so subsequent updates target the right row.
    #
    # Bookkeeping ONLY — the gateway already accepted the job above, so the at-cron
    # WILL fire. A DB blip stamping the id here must NOT propagate as a push failure:
    # callers roll back their own state (e.g. the workout-congrats stamp) when this
    # function raises, and rolling back a LIVE cron would drop a delivered message and
    # let it re-fire on a later retry. So the boundary is explicit — this function
    # raises ONLY for a failure BEFORE the gateway accepted the job. Losing the id just
    # means a later ``cron.update`` can't target this row by id; one-shots auto-delete
    # after firing anyway. (This repo has known idle-connection wedges, so the blip is
    # real, not theoretical.)
    details = result.get("details", result) if isinstance(result, dict) else {}
    job_id = ""
    if isinstance(details, dict):
        job_id = str(details.get("id") or details.get("jobId") or "")
    if job_id:
        cron.gateway_job_id = job_id
        try:
            cron.save(update_fields=["gateway_job_id"])
        except Exception:
            logger.warning(
                "At-cron push SUCCEEDED but gateway_job_id bookkeeping failed (tenant=%s cron=%s) — "
                "cron is live and will fire; leaving gateway_job_id unstamped",
                str(tenant.id)[:8],
                cron.name,
                exc_info=True,
            )


def task_hygiene_enabled(tenant: Tenant) -> bool:
    """Is the weekly task-hygiene cron open for this tenant?

    THE single gate. Every call site reads through here (invariant 13's
    one-shared-helper rule) so the seed path and any future console surface
    cannot drift into disagreeing about who gets a proactive sender.

    Backed by ``settings.TASK_HYGIENE_TENANT_IDS`` — a comma-separated
    allowlist of tenant UUIDs. Unset/empty means NOBODY: this cron messages
    the user unprompted, so it fails closed and is opened one tenant at a time
    (canary first, per the standing rollout ladder). Fleet-go is a deliberate
    later change, never a side effect of this code deploying.
    """
    from django.conf import settings

    raw = str(getattr(settings, "TASK_HYGIENE_TENANT_IDS", "") or "")
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not allowed:
        return False
    if str(tenant.id).lower() in allowed:
        return True
    # The allowlist is populated but this tenant is not on it. Almost always
    # intentional (canary rollout), but it is also exactly what a typo'd or
    # truncated TASK_HYGIENE_TENANT_IDS looks like — and the symptom of that
    # mistake is silence, which is indistinguishable from working correctly.
    # One line at INFO turns a confused "why didn't the canary get it" into a
    # log grep.
    logger.info(
        "task_hygiene: tenant %s is not in TASK_HYGIENE_TENANT_IDS (%d id(s) configured) — no hygiene cron",
        str(tenant.id)[:8],
        len(allowed),
    )
    return False


def seed_task_hygiene_cron(tenant: Tenant) -> dict[str, Any]:
    """Converge the weekly task-hygiene cron to match the gate, both directions.

    Called from the provisioning seed path and from the config-apply refresh
    path (``apps/orchestrator/services.py``) so a tenant added to — or removed
    from — the gate converges without a manual DB write.

    GATE OPEN, no row      → create it.
    GATE OPEN, row exists  → leave it EXACTLY as it is, enabled or not. This is
                             what makes "user turns the weekly cleanup off"
                             stick: a check that only looked for enabled rows
                             would helpfully recreate it on the next config
                             apply and the user could never be rid of it.
    GATE CLOSED, row exists → DISABLE it. Removing a tenant from the allowlist
                             is the rollback lever for a proactive sender, so it
                             has to actually stop the sending — a gate that only
                             guarded creation would leave every already-seeded
                             tenant messaging forever, which makes the canary
                             ladder one-way. Disabled, never deleted: the row is
                             the audit trail, and re-gating re-enables nothing
                             by surprise (it stays off until someone turns it
                             back on deliberately).

    Returns a small status dict; never raises. A hygiene cron is a nicety and
    must not be able to fail provisioning.
    """
    result: dict[str, Any] = {"tenant_id": str(tenant.id), "created": False}
    existing = CronJob.objects.filter(tenant=tenant, name=TASK_HYGIENE_CRON_NAME).first()

    if not task_hygiene_enabled(tenant):
        if existing is not None and existing.enabled:
            existing.enabled = False
            existing.save(update_fields=["enabled", "updated_at"])
            logger.info(
                "task_hygiene: disabled %r for ungated tenant %s",
                TASK_HYGIENE_CRON_NAME,
                str(tenant.id)[:8],
            )
            result["reason"] = "disabled_ungated"
            return result
        result["reason"] = "not_gated"
        return result

    if existing is not None:
        result["reason"] = "already_exists"
        return result

    try:
        create_typed_cron(
            tenant=tenant,
            pattern=CronPattern.TASK_HYGIENE,
            typed_payload={},
            name=TASK_HYGIENE_CRON_NAME,
            schedule={
                "kind": "cron",
                "expr": TASK_HYGIENE_CRON_EXPR,
                # Tenant-local Sunday evening, through the canonical tz front
                # door (invariant 7) — never a private tz helper.
                "tz": tenant_tz_name(tenant),
            },
            source=CronJobSource.SYSTEM,
        )
    except CronNameConflictError:
        # Raced another seed call between the exists() check and the insert.
        result["reason"] = "already_exists"
        return result
    except Exception:
        logger.warning(
            "seed_task_hygiene_cron failed for tenant %s (non-fatal)",
            str(tenant.id)[:8],
            exc_info=True,
        )
        result["reason"] = "error"
        return result

    result["created"] = True
    logger.info("seed_task_hygiene_cron: created %r for tenant %s", TASK_HYGIENE_CRON_NAME, str(tenant.id)[:8])
    return result
