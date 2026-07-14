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

from apps.cron.models import CronCreationPath, CronJob, CronJobSource, CronPattern
from apps.cron.patterns import get_handler
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


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


def _validate_schedule_shape(schedule: dict[str, Any]) -> None:
    """Surface-level schedule validation. OC does the full normalization."""
    if not isinstance(schedule, dict):
        raise TypedCronError("schedule must be an object", code="invalid_schedule")
    kind = schedule.get("kind")
    if kind not in ("cron", "every", "at"):
        raise TypedCronError(
            f"schedule.kind must be one of cron/every/at; got {kind!r}",
            code="invalid_schedule",
        )
    if kind == "cron" and not schedule.get("expr"):
        raise TypedCronError(
            "schedule.kind='cron' requires schedule.expr",
            code="invalid_schedule",
        )
    if kind == "at" and not schedule.get("at"):
        raise TypedCronError(
            "schedule.kind='at' requires schedule.at (ISO-8601)",
            code="invalid_schedule",
        )
    if kind == "every" and not schedule.get("everyMs"):
        raise TypedCronError(
            "schedule.kind='every' requires schedule.everyMs",
            code="invalid_schedule",
        )


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
    _validate_schedule_shape(schedule)

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
