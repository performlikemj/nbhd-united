"""Validation + tenant-aware normalization for cron schedule intake.

Two entry points, and the difference matters:

  ``validate_schedule(schedule)``
      Pure. No clock, no tenant, no external state. Raises on anything the
      OpenClaw gateway would mis-execute rather than reject. Every write path
      calls this, so a schedule that survives it is safe to store anywhere.

  ``normalize_schedule(schedule, *, tz_name, now=None)``
      Validate PLUS the two repairs that need context the pure layer does not
      have: translating a relative ``at`` duration into an absolute UTC
      timestamp, and backfilling a missing cron ``tz`` from the tenant. It
      normalizes FIRST and validates the result, so the strict rules below
      still apply to whatever gets stored.

The rules here exist because the gateway is permissive in exactly the places a
language model is likely to be wrong. Each one is anchored to observed
behaviour in openclaw@2026.5.28 (croner 10.0.1):

  - ``everyMs`` has no floor at the runtime. ``coerceSchedule`` clamps it with
    ``Math.max(1, ...)`` — one millisecond. OpenClaw's spin guard only covers
    ``kind:"cron"``. A model that writes 3600 meaning "hourly" gets a full LLM
    turn every 3.6 seconds and nothing downstream stops it.
  - A naked ``at`` timestamp is silently read as UTC: the runtime's
    ``normalizeUtcIso`` appends a ``Z`` to any offset-less ISO string. For a
    Tokyo user that is a nine-hour shift, delivered without a word of warning.
  - Relative durations (``20m``) reach the gateway as literal strings. The
    duration parser is wired only into the CLI, so the gateway answers
    ``Invalid schedule.at: expected ISO-8601 timestamp (got 20m)``. We accept
    the grammar the manual teaches and translate it here instead.
  - ``Etc/*`` zone names invert the sign by POSIX convention — ``Etc/GMT+9``
    is UTC−9, the opposite of what a model picking a "+9" name intends.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# Below one minute an "every" schedule burns LLM turns faster than a turn can
# finish. This is the floor the runtime does not have.
MIN_EVERY_MS = 60_000

# The relative-duration grammar the cron manual teaches: "20m", "2h", "1d".
_RELATIVE_AT_RE = re.compile(r"\A(\d{1,6})\s*([mhd])\Z", re.IGNORECASE)
_RELATIVE_UNITS = {"m": "minutes", "h": "hours", "d": "days"}

# croner's day-of-week field: 0=Sunday .. 6=Saturday, with 7 also Sunday.
# Verified against croner 10.0.1 — 8 raises "Invalid value for dayOfWeek".
MAX_DAY_OF_WEEK = 7

DAY_OF_WEEK_CONVENTION = (
    "day-of-week is 0=Sunday, 1=Monday .. 6=Saturday (7 also means Sunday); names like MON,TUE,SUN work too"
)


class ScheduleValidationError(ValueError):
    """A schedule cannot be safely accepted."""

    code = "invalid_schedule"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


def parse_relative_at(value: Any) -> timedelta | None:
    """Return the offset for a relative ``at`` like ``20m``, else None."""
    if not isinstance(value, str):
        return None
    match = _RELATIVE_AT_RE.match(value.strip())
    if match is None:
        return None
    return timedelta(**{_RELATIVE_UNITS[match.group(2).lower()]: int(match.group(1))})


def _validate_timezone(tz: str) -> None:
    """Reject zone names the gateway accepts but the user did not mean."""
    if tz.startswith("Etc/"):
        raise ScheduleValidationError(
            f"schedule.tz {tz!r} is a POSIX name whose sign is INVERTED — "
            f"'Etc/GMT+9' is UTC MINUS 9, not Tokyo. Use the Area/Location "
            f"form instead, e.g. 'Asia/Tokyo' or 'America/New_York'.",
            code="tz_etc_rejected",
        )
    try:
        ZoneInfo(tz)
    except Exception as exc:
        raise ScheduleValidationError(
            f"schedule.tz {tz!r} is not a known IANA timezone. Use the "
            f"Area/Location form, e.g. 'Asia/Tokyo' or 'America/New_York'.",
            code="tz_invalid",
        ) from exc


def _usable_backfill_tz(tz_name: str | None) -> str:
    """Return a zone name that will pass ``_validate_timezone``, else UTC."""
    if not tz_name:
        return "UTC"
    try:
        _validate_timezone(tz_name)
    except ScheduleValidationError:
        return "UTC"
    return tz_name


def _validate_day_of_week(field: str) -> None:
    """Reject numeric day-of-week tokens outside croner's 0-7 range.

    Deliberately narrow: only all-numeric tokens are checked. Names, ``*``,
    and step syntax pass through untouched so this can never reject something
    croner would have accepted.
    """
    for chunk in field.split(","):
        base = chunk.split("/", 1)[0]
        for token in base.split("-"):
            token = token.strip()
            if not token.isdigit():
                continue
            if int(token) > MAX_DAY_OF_WEEK:
                raise ScheduleValidationError(
                    f"cron day-of-week {token} is out of range — {DAY_OF_WEEK_CONVENTION}. "
                    f"This is NOT the 0=Monday convention used elsewhere in nbhd.",
                    code="dow_out_of_range",
                )


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
        _validate_day_of_week(day_of_week)

        tz = schedule.get("tz")
        if isinstance(tz, str) and tz.strip():
            _validate_timezone(tz.strip())

    if kind == "at":
        at = schedule.get("at")
        if not at:
            raise ScheduleValidationError("schedule.kind='at' requires schedule.at (ISO-8601)")
        if not isinstance(at, str):
            raise ScheduleValidationError("schedule.kind='at' requires a parseable ISO-8601 timestamp")
        normalized = at[:-1] + "+00:00" if at.endswith("Z") else at
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ScheduleValidationError(
                "schedule.kind='at' requires a parseable ISO-8601 timestamp "
                "WITH a timezone offset, e.g. '2026-06-18T09:00:00+09:00'"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ScheduleValidationError(
                f"schedule.at {at!r} has no timezone offset, and an offset-less "
                f"timestamp is read as UTC — for a user in Asia/Tokyo that fires "
                f"9 hours off. Include the user's offset "
                f"(e.g. '2026-06-18T09:00:00+09:00'), or use a relative duration "
                f"('20m', '2h', '1d') on the nbhd_cron_create_* tools.",
                code="naive_at_rejected",
            )

    if kind == "every":
        every_ms = schedule.get("everyMs")
        if not every_ms:
            raise ScheduleValidationError("schedule.kind='every' requires schedule.everyMs")
        if isinstance(every_ms, bool) or not isinstance(every_ms, int):
            raise ScheduleValidationError(
                f"schedule.everyMs must be a whole number of MILLISECONDS; got {every_ms!r}",
                code="everyms_not_integer",
            )
        if every_ms < MIN_EVERY_MS:
            raise ScheduleValidationError(
                f"schedule.everyMs is in MILLISECONDS and must be at least "
                f"{MIN_EVERY_MS} (one minute); {every_ms} would fire every "
                f"{every_ms / 1000:g} seconds. Hourly = 3600000, "
                f"daily = 86400000, every 15 minutes = 900000.",
                code="everyms_too_small",
            )


def normalize_schedule(
    schedule: dict[str, Any],
    *,
    tz_name: str | None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate ``schedule`` and repair the two context-dependent shapes.

    Returns the schedule to store plus the reason codes for what changed, so
    the caller can emit telemetry. Raises ``ScheduleValidationError`` on
    anything ``validate_schedule`` refuses — normalization runs first, so the
    strict rules apply to the stored value, not the submitted one.
    """
    if not isinstance(schedule, dict):
        validate_schedule(schedule)  # raises the object-shape error
        return schedule, []

    normalized = dict(schedule)
    reasons: list[str] = []
    kind = normalized.get("kind")

    if kind == "at":
        offset = parse_relative_at(normalized.get("at"))
        if offset is not None:
            if now is None:
                from django.utils import timezone as django_timezone

                now = django_timezone.now()
            fires_at = now.astimezone(UTC) + offset
            normalized["at"] = fires_at.replace(microsecond=0).isoformat()
            reasons.append("relative_at_translated")

    elif kind == "cron":
        tz = normalized.get("tz")
        if not (isinstance(tz, str) and tz.strip()):
            # No tz means the gateway evaluates the expression in the CONTAINER
            # host timezone, which is UTC — the user's 7am fires at 4pm.
            #
            # The backfill is sanitized because profiles written before the
            # timezone gate could still hold an Etc/* name. Injecting one here
            # would fail validation below and hand the model a 400 about a
            # field it never sent — unfixable from its side.
            normalized["tz"] = _usable_backfill_tz(tz_name)
            reasons.append("tz_backfilled")

    validate_schedule(normalized)
    return normalized, reasons
