"""Automation domain services."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone as dt_timezone
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.utils import timezone

from apps.router.services import forward_to_openclaw
from apps.tenants.models import Tenant

from .models import Automation, AutomationRun

MAX_ACTIVE_AUTOMATIONS = 5
MIN_INTERVAL_MINUTES = 120
MAX_RUNS_PER_DAY = 12

MIN_RUN_INTERVAL = timedelta(minutes=MIN_INTERVAL_MINUTES)


class AutomationError(Exception):
    """Base automation exception."""


class AutomationValidationError(AutomationError):
    """Raised when automation inputs are invalid."""


class AutomationLimitError(AutomationError):
    """Raised when automation caps are exceeded."""


class AutomationExecutionError(AutomationError):
    """Raised when automation dispatch fails."""


def validate_timezone_name(timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise AutomationValidationError(f"Unknown timezone: {timezone_name}") from exc


def normalize_schedule_days(schedule_days: list[int] | tuple[int, ...] | None) -> list[int]:
    if schedule_days is None:
        return []
    if not isinstance(schedule_days, (list, tuple)):
        raise AutomationValidationError("schedule_days must be a list of weekday integers")

    normalized: list[int] = []
    for day in schedule_days:
        try:
            day_int = int(day)
        except (TypeError, ValueError) as exc:
            raise AutomationValidationError("schedule_days must contain integers between 0 and 6") from exc
        if day_int < 0 or day_int > 6:
            raise AutomationValidationError("schedule_days must contain integers between 0 and 6")
        normalized.append(day_int)

    return sorted(set(normalized))


def validate_schedule(
    schedule_type: str,
    schedule_days: list[int] | tuple[int, ...] | None,
) -> list[int]:
    normalized_days = normalize_schedule_days(schedule_days)

    if schedule_type == Automation.ScheduleType.DAILY:
        return []
    if schedule_type == Automation.ScheduleType.WEEKLY:
        if not normalized_days:
            raise AutomationValidationError("schedule_days is required for weekly schedules")
        return normalized_days

    raise AutomationValidationError(f"Unsupported schedule_type: {schedule_type}")


def compute_next_run_at(
    *,
    timezone_name: str,
    schedule_type: str,
    schedule_time: dt_time,
    schedule_days: list[int] | tuple[int, ...] | None,
    reference_utc: datetime | None = None,
) -> datetime:
    """Compute the next scheduled UTC time from timezone-local schedule settings."""
    validate_timezone_name(timezone_name)
    normalized_days = validate_schedule(schedule_type, schedule_days)
    tz = ZoneInfo(timezone_name)

    reference = reference_utc or timezone.now()
    if timezone.is_naive(reference):
        reference = timezone.make_aware(reference, dt_timezone.utc)
    else:
        reference = reference.astimezone(dt_timezone.utc)

    local_reference = reference.astimezone(tz)

    if schedule_type == Automation.ScheduleType.DAILY:
        candidate = local_reference.replace(
            hour=schedule_time.hour,
            minute=schedule_time.minute,
            second=0,
            microsecond=0,
        )
        if candidate <= local_reference:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(dt_timezone.utc)

    for offset in range(0, 14):
        candidate_date = local_reference.date() + timedelta(days=offset)
        if candidate_date.weekday() not in normalized_days:
            continue

        candidate = datetime.combine(candidate_date, schedule_time, tzinfo=tz)
        if candidate <= local_reference:
            continue
        return candidate.astimezone(dt_timezone.utc)

    raise AutomationValidationError("Unable to compute next run time from schedule")


def _day_window_for_timezone(reference_utc: datetime, timezone_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(timezone_name)
    local_now = reference_utc.astimezone(tz)
    local_day_start = datetime.combine(local_now.date(), dt_time.min, tzinfo=tz)
    local_day_end = local_day_start + timedelta(days=1)
    return local_day_start.astimezone(dt_timezone.utc), local_day_end.astimezone(dt_timezone.utc)


def _runs_today_count(tenant: Tenant, timezone_name: str, reference_utc: datetime) -> int:
    day_start_utc, day_end_utc = _day_window_for_timezone(reference_utc, timezone_name)
    return AutomationRun.objects.filter(
        tenant=tenant,
        scheduled_for__gte=day_start_utc,
        scheduled_for__lt=day_end_utc,
    ).count()


def _active_automations_count(tenant: Tenant, *, exclude_id: uuid.UUID | None = None) -> int:
    queryset = Automation.objects.filter(tenant=tenant, status=Automation.Status.ACTIVE)
    if exclude_id is not None:
        queryset = queryset.exclude(id=exclude_id)
    return queryset.count()


def _build_automation_prompt(automation: Automation, run_id: uuid.UUID) -> str:
    if automation.kind == Automation.Kind.DAILY_BRIEF:
        return (
            f"[AUTOMATION:daily_brief run_id={run_id}] "
            "Prepare today's brief using available tools and memory. "
            "Respond with sections: Top Priorities, Calendar Constraints, Inbox Actions, Suggested Next Step."
        )
    if automation.kind == Automation.Kind.WEEKLY_REVIEW:
        return (
            f"[AUTOMATION:weekly_review run_id={run_id}] "
            "Prepare a concise weekly review. "
            "Respond with sections: Wins, Misses, Risks, Next Week Focus (top 3)."
        )
    raise AutomationValidationError(f"Unsupported automation kind: {automation.kind}")


def _build_synthetic_telegram_update(automation: Automation, run_id: uuid.UUID) -> dict:
    tenant = automation.tenant
    chat_id = tenant.user.telegram_chat_id
    if not chat_id:
        raise AutomationExecutionError("tenant user is missing telegram_chat_id")

    timestamp = int(timezone.now().timestamp())
    prompt = _build_automation_prompt(automation, run_id)
    sender_id = tenant.user.telegram_user_id or chat_id

    return {
        "update_id": int(f"{timestamp}{str(run_id.int)[-4:]}"),
        "message": {
            "message_id": timestamp,
            "date": timestamp,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": sender_id,
                "is_bot": False,
                "first_name": tenant.user.display_name or "User",
                "username": tenant.user.telegram_username or "",
            },
            "text": prompt,
        },
    }


def _dispatch_to_openclaw(automation: Automation, run_id: uuid.UUID) -> tuple[dict, dict]:
    tenant = automation.tenant
    if tenant.status != Tenant.Status.ACTIVE:
        raise AutomationExecutionError(f"tenant is not active: {tenant.status}")
    if not tenant.container_fqdn:
        raise AutomationExecutionError("tenant does not have an active container endpoint")

    synthetic_update = _build_synthetic_telegram_update(automation, run_id)

    loop = asyncio.new_event_loop()
    try:
        user_timezone = automation.timezone or tenant.user.timezone or "UTC"
        result = loop.run_until_complete(
            forward_to_openclaw(
                tenant.container_fqdn,
                synthetic_update,
                user_timezone=user_timezone,
            )
        )
    finally:
        loop.close()

    if result is None:
        raise AutomationExecutionError("OpenClaw dispatch returned no response")
    return synthetic_update, result


def _build_idempotency_key(
    automation: Automation,
    trigger_source: str,
    scheduled_for: datetime,
) -> str:
    timestamp = int(scheduled_for.timestamp())
    if trigger_source == AutomationRun.TriggerSource.SCHEDULE:
        return f"schedule:{automation.id}:{timestamp}"
    return f"manual:{automation.id}:{timestamp}:{uuid.uuid4()}"


def _build_input_payload(
    automation: Automation,
    trigger_source: str,
    scheduled_for: datetime,
) -> dict:
    return {
        "automation_id": str(automation.id),
        "kind": automation.kind,
        "trigger_source": trigger_source,
        "scheduled_for": scheduled_for.isoformat(),
        "timezone": automation.timezone,
    }


@dataclass
class _LimitCheck:
    allowed: bool
    reason: str = ""


def _check_limits(
    automation: Automation,
    *,
    reference_utc: datetime,
) -> _LimitCheck:
    if automation.last_run_at and (reference_utc - automation.last_run_at) < MIN_RUN_INTERVAL:
        return _LimitCheck(
            allowed=False,
            reason=f"min interval not met ({MIN_INTERVAL_MINUTES} minutes)",
        )

    runs_today = _runs_today_count(automation.tenant, automation.timezone, reference_utc)
    if runs_today >= MAX_RUNS_PER_DAY:
        return _LimitCheck(
            allowed=False,
            reason=f"daily run cap reached ({MAX_RUNS_PER_DAY})",
        )

    return _LimitCheck(allowed=True)


def _advance_schedule(automation: Automation, *, scheduled_for: datetime) -> None:
    automation.next_run_at = compute_next_run_at(
        timezone_name=automation.timezone,
        schedule_type=automation.schedule_type,
        schedule_time=automation.schedule_time,
        schedule_days=automation.schedule_days,
        reference_utc=scheduled_for + timedelta(seconds=1),
    )


def create_automation(*, tenant: Tenant, validated_data: dict) -> Automation:
    status_value = validated_data.get("status", Automation.Status.ACTIVE)
    schedule_type = validated_data["schedule_type"]
    schedule_days = validate_schedule(schedule_type, validated_data.get("schedule_days"))

    if status_value == Automation.Status.ACTIVE:
        if _active_automations_count(tenant) >= MAX_ACTIVE_AUTOMATIONS:
            raise AutomationLimitError(
                f"active automation limit reached ({MAX_ACTIVE_AUTOMATIONS})"
            )

    next_run_at = compute_next_run_at(
        timezone_name=validated_data["timezone"],
        schedule_type=schedule_type,
        schedule_time=validated_data["schedule_time"],
        schedule_days=schedule_days,
    )

    return Automation.objects.create(
        tenant=tenant,
        kind=validated_data["kind"],
        status=status_value,
        timezone=validated_data["timezone"],
        schedule_type=schedule_type,
        schedule_time=validated_data["schedule_time"],
        schedule_days=schedule_days,
        quiet_hours_start=validated_data.get("quiet_hours_start"),
        quiet_hours_end=validated_data.get("quiet_hours_end"),
        next_run_at=next_run_at,
    )


def update_automation(*, automation: Automation, validated_data: dict) -> Automation:
    status_value = validated_data.get("status", automation.status)
    schedule_type = validated_data.get("schedule_type", automation.schedule_type)
    schedule_days = validate_schedule(
        schedule_type,
        validated_data.get("schedule_days", automation.schedule_days),
    )
    timezone_name = validated_data.get("timezone", automation.timezone)
    schedule_time = validated_data.get("schedule_time", automation.schedule_time)

    validate_timezone_name(timezone_name)

    if status_value == Automation.Status.ACTIVE and automation.status != Automation.Status.ACTIVE:
        if _active_automations_count(automation.tenant, exclude_id=automation.id) >= MAX_ACTIVE_AUTOMATIONS:
            raise AutomationLimitError(
                f"active automation limit reached ({MAX_ACTIVE_AUTOMATIONS})"
            )

    automation.kind = validated_data.get("kind", automation.kind)
    automation.status = status_value
    automation.timezone = timezone_name
    automation.schedule_type = schedule_type
    automation.schedule_time = schedule_time
    automation.schedule_days = schedule_days
    automation.quiet_hours_start = validated_data.get("quiet_hours_start", automation.quiet_hours_start)
    automation.quiet_hours_end = validated_data.get("quiet_hours_end", automation.quiet_hours_end)

    schedule_fields_changed = any(
        field in validated_data
        for field in ("timezone", "schedule_type", "schedule_time", "schedule_days")
    )
    if automation.status == Automation.Status.ACTIVE and (schedule_fields_changed or "status" in validated_data):
        automation.next_run_at = compute_next_run_at(
            timezone_name=automation.timezone,
            schedule_type=automation.schedule_type,
            schedule_time=automation.schedule_time,
            schedule_days=automation.schedule_days,
        )

    automation.save()
    return automation


def pause_automation(automation: Automation) -> Automation:
    if automation.status != Automation.Status.PAUSED:
        automation.status = Automation.Status.PAUSED
        automation.save(update_fields=["status", "updated_at"])
    return automation


def resume_automation(automation: Automation) -> Automation:
    if automation.status != Automation.Status.ACTIVE:
        if _active_automations_count(automation.tenant, exclude_id=automation.id) >= MAX_ACTIVE_AUTOMATIONS:
            raise AutomationLimitError(
                f"active automation limit reached ({MAX_ACTIVE_AUTOMATIONS})"
            )
        automation.status = Automation.Status.ACTIVE
        automation.next_run_at = compute_next_run_at(
            timezone_name=automation.timezone,
            schedule_type=automation.schedule_type,
            schedule_time=automation.schedule_time,
            schedule_days=automation.schedule_days,
        )
        automation.save(update_fields=["status", "next_run_at", "updated_at"])
    return automation


def execute_automation(
    *,
    automation: Automation,
    trigger_source: str,
    scheduled_for: datetime | None = None,
) -> AutomationRun:
    reference_utc = scheduled_for or timezone.now()
    if timezone.is_naive(reference_utc):
        reference_utc = timezone.make_aware(reference_utc, dt_timezone.utc)
    else:
        reference_utc = reference_utc.astimezone(dt_timezone.utc)

    limit_check = _check_limits(automation, reference_utc=reference_utc)
    if not limit_check.allowed:
        if trigger_source == AutomationRun.TriggerSource.MANUAL:
            raise AutomationLimitError(limit_check.reason)

        run = AutomationRun.objects.create(
            automation=automation,
            tenant=automation.tenant,
            status=AutomationRun.Status.SKIPPED,
            trigger_source=trigger_source,
            scheduled_for=reference_utc,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            idempotency_key=_build_idempotency_key(automation, trigger_source, reference_utc),
            input_payload=_build_input_payload(automation, trigger_source, reference_utc),
            error_message=limit_check.reason,
        )
        _advance_schedule(automation, scheduled_for=reference_utc)
        automation.save(update_fields=["next_run_at", "updated_at"])
        return run

    idempotency_key = _build_idempotency_key(automation, trigger_source, reference_utc)
    defaults = {
        "automation": automation,
        "tenant": automation.tenant,
        "status": AutomationRun.Status.PENDING,
        "trigger_source": trigger_source,
        "scheduled_for": reference_utc,
        "input_payload": _build_input_payload(automation, trigger_source, reference_utc),
    }

    with transaction.atomic():
        run, created = AutomationRun.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults=defaults,
        )

    if not created:
        return run

    run.status = AutomationRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=["status", "started_at", "updated_at"])

    try:
        synthetic_update, dispatch_result = _dispatch_to_openclaw(automation, run.id)
        run.status = AutomationRun.Status.SUCCEEDED
        run.input_payload = {
            **run.input_payload,
            "synthetic_update": synthetic_update,
        }
        run.result_payload = {"router_response": dispatch_result}
    except Exception as exc:
        run.status = AutomationRun.Status.FAILED
        run.error_message = str(exc)

    run.finished_at = timezone.now()
    run.save(
        update_fields=[
            "status",
            "input_payload",
            "result_payload",
            "error_message",
            "finished_at",
            "updated_at",
        ]
    )

    updates = ["updated_at"]
    if run.status == AutomationRun.Status.SUCCEEDED:
        automation.last_run_at = run.finished_at
        updates.append("last_run_at")

    if trigger_source == AutomationRun.TriggerSource.SCHEDULE:
        _advance_schedule(automation, scheduled_for=reference_utc)
        updates.append("next_run_at")

    automation.save(update_fields=updates)
    return run
