"""Review-always approval gate and post-commit dispatch for typed cron creation."""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.actions.models import (
    ActionAuditLog,
    ActionAuditOutcome,
    ActionStatus,
    ActionType,
    CronDispatch,
    CronDispatchState,
    PendingAction,
)
from apps.actions.origin import OriginStamp
from apps.actions.services import record_action_audit
from apps.cron.models import CronJob, CronJobSource
from apps.cron.services import (
    CronNameConflictError,
    TypedCronError,
    create_validated_typed_cron,
    validate_typed_cron_request,
)
from apps.pii.store_authoring import author_store_fields, owner_store_representation
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

CRON_GATE_REVIEW_WINDOW = timedelta(hours=72)
CRON_DUPLICATE_WINDOW = timedelta(minutes=2)
CRON_DISPATCH_LEASE = timedelta(minutes=10)
CRON_DISPATCH_MAX_ATTEMPTS = 5
CRON_ACTION_TYPES = frozenset({ActionType.CRON_CREATE})


class CronGateConflict(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CronDispatchVerificationError(Exception):
    """A stale at-dispatch could not prove whether cron.add already landed."""


def cron_gate_enabled(tenant: Tenant) -> bool:
    """Single fail-closed allowlist gate for cron-create review."""

    from django.conf import settings

    raw = str(getattr(settings, "CRON_GATE_TENANT_IDS", "") or "")
    allowed = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return bool(allowed) and str(tenant.id).lower() in allowed


def is_cron_action_type(action_type: str) -> bool:
    return action_type in CRON_ACTION_TYPES


def _schedule_gate_changed(action: PendingAction) -> None:
    from apps.datebook.notify import dispatch_datebook_gate_changed

    tenant_id = action.tenant_id
    transaction.on_commit(lambda: dispatch_datebook_gate_changed(tenant_id))


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_request(validated, reason: str) -> dict:
    return {
        "pattern": validated.pattern,
        "name": validated.name,
        "schedule": validated.schedule,
        "typed_payload": validated.typed_payload,
        "reason": reason,
    }


def _request_hash(canonical: dict) -> str:
    return hashlib.sha256(_canonical_bytes(canonical)).hexdigest()


def _cron_request_id(value, canonical: dict) -> str:
    if value in (None, ""):
        return f"auto:{_request_hash(canonical)[:32]}"
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 128:
        raise TypedCronError("cron_request_id must be a string of at most 128 characters", code="invalid_request_id")
    return value.strip()


def _humanize_schedule(schedule: dict) -> str:
    kind = schedule.get("kind")
    if kind == "every":
        milliseconds = schedule.get("everyMs", 0)
        units = ((86_400_000, "day"), (3_600_000, "hour"), (60_000, "minute"))
        for divisor, label in units:
            if milliseconds and milliseconds % divisor == 0:
                count = milliseconds // divisor
                return f"Every {count} {label}{'' if count == 1 else 's'}"
        return f"Every {milliseconds / 1000:g} seconds"
    if kind == "at":
        parsed = datetime.fromisoformat(str(schedule.get("at", "")).replace("Z", "+00:00"))
        return f"Once at {parsed.strftime('%Y-%m-%d %H:%M %Z').strip()}"
    if kind == "cron":
        expression = str(schedule.get("expr", ""))
        timezone_name = str(schedule.get("tz", "UTC"))
        fields = expression.split()
        if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
            minute, hour, day_of_month, month, day_of_week = fields
            clock = f"{int(hour):02d}:{int(minute):02d}"
            if day_of_month == month == day_of_week == "*":
                return f"Every day at {clock} ({timezone_name})"
            day_names = {
                "0": "Sunday",
                "1": "Monday",
                "2": "Tuesday",
                "3": "Wednesday",
                "4": "Thursday",
                "5": "Friday",
                "6": "Saturday",
                "7": "Sunday",
                "SUN": "Sunday",
                "MON": "Monday",
                "TUE": "Tuesday",
                "WED": "Wednesday",
                "THU": "Thursday",
                "FRI": "Friday",
                "SAT": "Saturday",
            }
            if day_of_month == month == "*" and day_of_week.upper() in day_names:
                return f"Every {day_names[day_of_week.upper()]} at {clock} ({timezone_name})"
        return f"Cron {expression} ({timezone_name})"
    return "Scheduled time"


def _display_summary(name: str, schedule: dict) -> str:
    return f"Create scheduled task “{name}” — {_humanize_schedule(schedule)}"


def _author_cron_fields(tenant: Tenant, canonical: dict, canonical_hash: str) -> tuple[dict, dict]:
    payload = deepcopy(canonical)
    protocol = {
        "pattern": payload.pop("pattern"),
        "schedule": payload.pop("schedule"),
    }
    typed_payload = payload.get("typed_payload", {})
    typed_protocol = {}
    if isinstance(typed_payload, dict):
        typed_payload = dict(typed_payload)
        for key in ("refresh_facts_via", "query_tool", "render_block"):
            if key in typed_payload:
                typed_protocol[key] = typed_payload.pop(key)
        payload["typed_payload"] = typed_payload

    authored, receipts = author_store_fields(
        tenant,
        {
            "action_payload": payload,
            "display_summary": _display_summary(canonical["name"], canonical["schedule"]),
        },
        model_label="actions.PendingAction",
        seam="cron.runtime.review.request",
        writer="runtime",
        defer_detection=True,
    )
    authored_payload = authored["action_payload"]
    authored_payload.update(protocol)
    authored_payload["canonical_hash"] = canonical_hash
    authored_payload["typed_payload"].update(typed_protocol)
    return authored, receipts


def _rehydrated_fields(action: PendingAction) -> dict:
    represented = owner_store_representation(
        action,
        action.tenant,
        {"action_payload": action.action_payload},
        model_label="actions.PendingAction",
    )["action_payload"]
    if not isinstance(represented, dict):
        raise TypedCronError("stored cron request is invalid", code="invalid_payload")
    return represented


def _rehydrated_summary(action: PendingAction) -> str:
    return owner_store_representation(
        action,
        action.tenant,
        {"display_summary": action.display_summary},
        model_label="actions.PendingAction",
    )["display_summary"]


def cron_action_state(action: PendingAction) -> dict:
    if action.status == ActionStatus.PENDING:
        return {
            "state": "pending_approval",
            "action_id": action.id,
            "expires_at": action.expires_at.isoformat(),
            "summary": _rehydrated_summary(action),
        }
    if action.status == ActionStatus.APPROVED:
        failed = action.resolution_code.startswith(("create_failed", "dispatch_failed"))
        execution = "failed" if failed else "executed" if action.resolution_code == "executed" else "queued"
        return {"status": action.status, "execution": execution, "code": action.resolution_code if failed else ""}
    return {
        "status": action.status,
        "execution": "failed",
        "code": action.resolution_code or action.status,
    }


def _existing_request(tenant: Tenant, request_id: str, canonical_hash: str) -> PendingAction | None:
    action = PendingAction.objects.filter(tenant=tenant, cron_request_id=request_id).first()
    if action is None:
        return None
    payload = action.action_payload if isinstance(action.action_payload, dict) else {}
    if payload.get("canonical_hash") != canonical_hash:
        raise CronGateConflict("request_id_conflict")
    return action


def _recent_duplicate(tenant: Tenant, canonical_hash: str) -> PendingAction | None:
    recent = PendingAction.objects.filter(
        tenant=tenant,
        action_type=ActionType.CRON_CREATE,
        status=ActionStatus.PENDING,
        created_at__gte=timezone.now() - CRON_DUPLICATE_WINDOW,
    ).order_by("-created_at")[:20]
    for candidate in recent:
        payload = candidate.action_payload if isinstance(candidate.action_payload, dict) else {}
        if payload.get("canonical_hash") == canonical_hash:
            return candidate
    return None


def request_cron_action(
    tenant: Tenant,
    *,
    cron_request_id,
    pattern: str,
    name: str,
    schedule: dict,
    typed_payload: dict,
    reason: str,
    origin_stamp: OriginStamp,
) -> dict:
    """Validate and create an idempotent, review-always cron proposal."""

    if not isinstance(reason, str) or len(reason) > 200:
        raise TypedCronError("reason must be a string of at most 200 characters", code="invalid_reason")
    validated = validate_typed_cron_request(
        tenant=tenant,
        pattern=pattern,
        typed_payload=typed_payload,
        name=name,
        schedule=schedule,
        require_future_at=True,
    )
    canonical = _canonical_request(validated, reason)
    canonical_hash = _request_hash(canonical)
    request_id = _cron_request_id(cron_request_id, canonical)

    existing = _existing_request(tenant, request_id, canonical_hash)
    if existing is not None:
        return {**cron_action_state(existing), "_created": False}

    try:
        with transaction.atomic():
            locked_tenant = Tenant.objects.select_for_update().get(pk=tenant.pk)
            existing = _existing_request(locked_tenant, request_id, canonical_hash)
            if existing is not None:
                return {**cron_action_state(existing), "_created": False}
            duplicate = _recent_duplicate(locked_tenant, canonical_hash)
            if duplicate is not None:
                return {**cron_action_state(duplicate), "_created": False}
            if CronJob.objects.filter(tenant=locked_tenant, name=validated.name, enabled=True).exists():
                raise CronGateConflict("name_conflict")

            authored, receipts = _author_cron_fields(locked_tenant, canonical, canonical_hash)
            action = PendingAction.objects.create(
                tenant=locked_tenant,
                action_type=ActionType.CRON_CREATE,
                action_payload=authored["action_payload"],
                display_summary=authored["display_summary"],
                pii_receipts=receipts,
                cron_request_id=request_id,
                origin_kind=origin_stamp.kind,
                origin_cron_name=origin_stamp.cron_name,
                origin_run_id=origin_stamp.run_id,
                expires_at=timezone.now() + CRON_GATE_REVIEW_WINDOW,
            )
    except IntegrityError:
        existing = _existing_request(tenant, request_id, canonical_hash)
        if existing is None:
            raise
        return {**cron_action_state(existing), "_created": False}

    from apps.actions.messaging import send_gate_confirmation

    delivered = send_gate_confirmation(tenant, action)
    if not delivered:
        action.delivery_state = "no_channel"
        action.save(update_fields=["delivery_state"])

    _schedule_gate_changed(action)
    return {**cron_action_state(action), "_created": True}


def _approval_failure(action: PendingAction, responded_at, code: str) -> dict:
    action.status = ActionStatus.APPROVED
    action.responded_at = responded_at
    action.resolution_code = code
    action.save(update_fields=["status", "responded_at", "resolution_code"])
    record_action_audit(action, ActionAuditOutcome.APPROVED, responded_at=responded_at)
    record_action_audit(action, ActionAuditOutcome.FAILED, responded_at=responded_at, detail_code=code)
    _schedule_gate_changed(action)
    return cron_action_state(action)


def approve_cron_action(action: PendingAction, *, responded_at=None) -> dict:
    """Txn-A portion: revalidate, insert locally, approve, and create outbox."""

    responded_at = responded_at or timezone.now()
    fields = _rehydrated_fields(action)
    try:
        validated = validate_typed_cron_request(
            tenant=action.tenant,
            pattern=fields.get("pattern"),
            typed_payload=fields.get("typed_payload"),
            name=fields.get("name"),
            schedule=fields.get("schedule"),
            now=responded_at,
            require_future_at=True,
        )
    except TypedCronError as exc:
        code = "create_failed:past" if exc.code == "at_in_past" else "create_failed:invalid"
        return _approval_failure(action, responded_at, code)

    if CronJob.objects.filter(tenant=action.tenant, name=validated.name, enabled=True).exists():
        return _approval_failure(action, responded_at, "create_failed:name")

    from apps.cron.signals import suppress_cronjob_reconcile

    try:
        with suppress_cronjob_reconcile():
            cron = create_validated_typed_cron(
                tenant=action.tenant,
                request=validated,
                source=CronJobSource.USER,
            )
    except CronNameConflictError:
        return _approval_failure(action, responded_at, "create_failed:name")

    action.status = ActionStatus.APPROVED
    action.responded_at = responded_at
    action.resolution_code = ActionAuditOutcome.QUEUED
    action.save(update_fields=["status", "responded_at", "resolution_code"])
    record_action_audit(action, ActionAuditOutcome.APPROVED, responded_at=responded_at)
    dispatch = CronDispatch.objects.create(
        action=action,
        cron=cron,
        kind=validated.schedule["kind"],
    )
    response = {"status": ActionStatus.APPROVED, "execution": "queued", "code": ""}
    transaction.on_commit(lambda: dispatch_cron_action(dispatch.id, response=response), robust=True)
    _schedule_gate_changed(action)
    return response


def _terminal_dispatch_audit(action: PendingAction):
    if action.responded_at is None:
        return None
    return (
        ActionAuditLog.objects.filter(
            tenant_id=action.tenant_id,
            action_type=ActionType.CRON_CREATE,
            responded_at=action.responded_at,
            result__in=(ActionAuditOutcome.EXECUTED, ActionAuditOutcome.FAILED),
        )
        .order_by("-id")
        .first()
    )


def _set_dispatch_response(response: dict | None, state: str, code: str = "") -> None:
    if response is None:
        return
    response.update(
        execution=(
            "executed"
            if state == CronDispatchState.EXECUTED
            else "failed"
            if state == CronDispatchState.FAILED
            else "queued"
        ),
        code=code,
    )


def _finish_dispatch_claim(dispatch_id: int, attempt: int, *, failure_code: str = "") -> str:
    """Txn B: CAS the claimed attempt into one immutable terminal outcome."""

    with transaction.atomic():
        dispatch = CronDispatch.objects.select_related("action", "cron").select_for_update().get(pk=dispatch_id)
        if dispatch.state != CronDispatchState.DISPATCHING or dispatch.attempts != attempt:
            return dispatch.state

        action = dispatch.action
        if failure_code:
            CronJob.objects.filter(pk=dispatch.cron_id).update(enabled=False)
            dispatch.state = CronDispatchState.FAILED
            action.resolution_code = failure_code
            audit_outcome = ActionAuditOutcome.FAILED
        else:
            dispatch.state = CronDispatchState.EXECUTED
            action.resolution_code = ActionAuditOutcome.EXECUTED
            audit_outcome = ActionAuditOutcome.EXECUTED

        dispatch.save(update_fields=["state"])
        action.save(update_fields=["resolution_code"])
        if _terminal_dispatch_audit(action) is None:
            record_action_audit(
                action,
                audit_outcome,
                responded_at=action.responded_at,
                detail_code=failure_code,
            )
        return dispatch.state


def _claim_dispatch(dispatch_id: int) -> tuple[int, bool] | tuple[None, str]:
    """Claim one queued/stale row without holding a lock across I/O."""

    now = timezone.now()
    with transaction.atomic():
        dispatch = CronDispatch.objects.select_related("action", "cron").select_for_update().get(pk=dispatch_id)
        if dispatch.state in (CronDispatchState.EXECUTED, CronDispatchState.FAILED):
            return None, dispatch.state

        terminal = _terminal_dispatch_audit(dispatch.action)
        if terminal is not None:
            dispatch.state = (
                CronDispatchState.EXECUTED
                if terminal.result == ActionAuditOutcome.EXECUTED
                else CronDispatchState.FAILED
            )
            dispatch.save(update_fields=["state"])
            return None, dispatch.state

        stale_claim = dispatch.state == CronDispatchState.DISPATCHING
        if (
            stale_claim
            and dispatch.last_attempt_at is not None
            and dispatch.last_attempt_at > now - CRON_DISPATCH_LEASE
        ):
            return None, "busy"
        if dispatch.attempts >= CRON_DISPATCH_MAX_ATTEMPTS:
            state = _finish_dispatch_claim_locked(dispatch, "dispatch_failed:exhausted")
            return None, state

        dispatch.state = CronDispatchState.DISPATCHING
        dispatch.attempts += 1
        dispatch.last_attempt_at = now
        dispatch.save(update_fields=["state", "attempts", "last_attempt_at"])
        return dispatch.attempts, stale_claim


def _finish_dispatch_claim_locked(dispatch: CronDispatch, failure_code: str) -> str:
    """Terminalize a row already locked by the caller."""

    CronJob.objects.filter(pk=dispatch.cron_id).update(enabled=False)
    dispatch.state = CronDispatchState.FAILED
    dispatch.save(update_fields=["state"])
    action = dispatch.action
    action.resolution_code = failure_code
    action.save(update_fields=["resolution_code"])
    if _terminal_dispatch_audit(action) is None:
        record_action_audit(
            action,
            ActionAuditOutcome.FAILED,
            responded_at=action.responded_at,
            detail_code=failure_code,
        )
    return dispatch.state


def _release_at_verification_claim(dispatch_id: int, attempt: int) -> tuple[str, str]:
    """Keep an unverifiable at-dispatch leased for another verified retry."""

    with transaction.atomic():
        dispatch = CronDispatch.objects.select_related("action", "cron").select_for_update().get(pk=dispatch_id)
        if dispatch.state != CronDispatchState.DISPATCHING or dispatch.attempts != attempt:
            return dispatch.state, dispatch.action.resolution_code
        if dispatch.attempts >= CRON_DISPATCH_MAX_ATTEMPTS:
            code = "dispatch_failed:exhausted"
            return _finish_dispatch_claim_locked(dispatch, code), code
        # Do not put an attempted at-job back into QUEUED: a later claim would
        # treat it as a first attempt and could cron.add without cron.list.
        # Retaining DISPATCHING makes the lease the double-push guard and every
        # later attempt re-verifies the gateway before considering cron.add.
        return CronDispatchState.QUEUED, ""


def _listed_at_gateway_job(result, cron: CronJob) -> tuple[bool, str]:
    details = result.get("details", result) if isinstance(result, dict) else None
    jobs = details.get("jobs") if isinstance(details, dict) else None
    if not isinstance(jobs, list):
        raise RuntimeError("cron.list returned an invalid jobs payload")
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or job.get("jobId") or "")[:64]
        if (cron.gateway_job_id and job_id == cron.gateway_job_id) or job.get("name") == cron.name:
            return True, job_id
    return False, ""


def _dispatch_claimed_cron(dispatch: CronDispatch, *, stale_claim: bool) -> str:
    action = dispatch.action
    cron = dispatch.cron
    if dispatch.kind == "at":
        from apps.cron.gateway_client import invoke_gateway_tool

        if stale_claim:
            try:
                listed, gateway_job_id = _listed_at_gateway_job(
                    invoke_gateway_tool(action.tenant, "cron.list", {"includeDisabled": True}),
                    cron,
                )
            except Exception as exc:
                raise CronDispatchVerificationError from exc
            if listed:
                if gateway_job_id:
                    CronJob.objects.filter(pk=cron.pk).update(gateway_job_id=gateway_job_id)
                return ""
        result = invoke_gateway_tool(action.tenant, "cron.add", {"job": cron.data})
        details = result.get("details", result) if isinstance(result, dict) else {}
        gateway_job_id = str(details.get("id") or details.get("jobId") or "") if isinstance(details, dict) else ""
        if not gateway_job_id:
            raise RuntimeError("cron.add returned no job id")
        CronJob.objects.filter(pk=cron.pk).update(gateway_job_id=gateway_job_id[:64])
    elif getattr(action.tenant, "postgres_cron_canonical", False):
        from apps.cron.signals import _enqueue_regen

        if not _enqueue_regen(str(action.tenant_id)) and not _enqueue_regen(str(action.tenant_id)):
            raise RuntimeError("reconcile enqueue failed twice")
    return ""


def dispatch_cron_action(dispatch_id: int, *, response: dict | None = None) -> str:
    """Claim, dispatch outside a transaction, then persist Txn B outcome."""

    try:
        attempt, claim_state = _claim_dispatch(dispatch_id)
    except Exception:
        logger.warning("cron approval dispatch claim failed dispatch=%s", dispatch_id, exc_info=True)
        _set_dispatch_response(response, CronDispatchState.QUEUED)
        return CronDispatchState.QUEUED
    if attempt is None:
        state = claim_state
        code = ""
        if state == CronDispatchState.FAILED:
            code = (
                CronDispatch.objects.filter(pk=dispatch_id).values_list("action__resolution_code", flat=True).first()
                or "dispatch_failed"
            )
        _set_dispatch_response(response, state, code)
        return state

    dispatch = CronDispatch.objects.select_related("action__tenant", "cron").get(pk=dispatch_id)
    try:
        _dispatch_claimed_cron(dispatch, stale_claim=claim_state)
    except CronDispatchVerificationError:
        if dispatch.kind == "at" and claim_state:
            logger.warning("stale at-cron dispatch could not be verified dispatch=%s", dispatch_id, exc_info=True)
            state, code = _release_at_verification_claim(dispatch_id, attempt)
            _set_dispatch_response(response, state, code)
            return state
        raise
    except Exception:
        logger.warning("cron approval dispatch failed dispatch=%s", dispatch_id, exc_info=True)
        failure_code = "dispatch_failed"
    else:
        failure_code = ""

    try:
        state = _finish_dispatch_claim(dispatch_id, attempt, failure_code=failure_code)
    except Exception:
        logger.warning("cron approval dispatch Txn B failed dispatch=%s", dispatch_id, exc_info=True)
        _set_dispatch_response(response, CronDispatchState.QUEUED)
        return CronDispatchState.QUEUED

    _set_dispatch_response(response, state, failure_code)
    return state


def deny_cron_action(action: PendingAction, *, responded_at=None) -> dict:
    responded_at = responded_at or timezone.now()
    action.status = ActionStatus.DENIED
    action.responded_at = responded_at
    action.resolution_code = ActionStatus.DENIED
    action.save(update_fields=["status", "responded_at", "resolution_code"])
    record_action_audit(action, ActionAuditOutcome.DENIED, responded_at=responded_at)
    _schedule_gate_changed(action)
    return cron_action_state(action)


def expire_cron_action(action: PendingAction) -> dict:
    action.status = ActionStatus.EXPIRED
    action.responded_at = timezone.now()
    action.resolution_code = ActionStatus.EXPIRED
    action.save(update_fields=["status", "responded_at", "resolution_code"])
    record_action_audit(
        action,
        ActionAuditOutcome.EXPIRED,
        responded_at=action.responded_at,
        detail_code=ActionStatus.EXPIRED,
    )
    _schedule_gate_changed(action)
    return cron_action_state(action)
