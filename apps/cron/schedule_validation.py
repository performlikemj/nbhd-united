"""Pure validation for cron schedule intake."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class ScheduleValidationError(ValueError):
    """A schedule cannot be safely accepted."""

    code = "invalid_schedule"


def validate_schedule(schedule: dict[str, Any]) -> None:
    """Validate a schedule without reading or writing external state."""
    if not isinstance(schedule, dict):
        raise ScheduleValidationError("schedule must be an object")

    kind = schedule.get("kind")
    if kind not in ("cron", "every", "at"):
        raise ScheduleValidationError(f"schedule.kind must be one of cron/every/at; got {kind!r}")

    if kind == "cron":
        expr = schedule.get("expr")
        if not expr:
            raise ScheduleValidationError("schedule.kind='cron' requires schedule.expr")
        if not isinstance(expr, str):
            raise ScheduleValidationError("cron expression must be a string containing exactly 5 fields")

        fields = expr.split()
        if len(fields) != 5:
            raise ScheduleValidationError(
                "cron expression must use exactly 5 fields; seconds precision "
                "is unsupported — use 5 fields: minute hour day-of-month month "
                "day-of-week"
            )

        day_of_month = fields[2]
        day_of_week = fields[4]
        if day_of_month != "*" and day_of_week != "*":
            raise ScheduleValidationError(
                "cron day-of-month and day-of-week are OR-ed and will fire on "
                "both; for a one-time reminder use kind:'at' (auto-deletes); "
                "for 'the 24th' set day-of-week to *; for 'Fridays' set "
                "day-of-month to *"
            )

    if kind == "at":
        at = schedule.get("at")
        if not at:
            raise ScheduleValidationError("schedule.kind='at' requires schedule.at (ISO-8601)")
        if not isinstance(at, str):
            raise ScheduleValidationError("schedule.kind='at' requires a parseable ISO-8601 timestamp")
        normalized = at[:-1] + "+00:00" if at.endswith("Z") else at
        try:
            datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ScheduleValidationError("schedule.kind='at' requires a parseable ISO-8601 timestamp") from exc

    if kind == "every" and not schedule.get("everyMs"):
        raise ScheduleValidationError("schedule.kind='every' requires schedule.everyMs")
