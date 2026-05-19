"""Deterministic time-window resolution for parameterized query tools.

Used by ``apps.common.query_view.BaseQueryView`` and per-domain query views
to translate an agent-supplied ``Window`` request into a closed date interval
``[from_date, to_date]`` in the tenant's local timezone.

The set of supported windows is a fixed enum, not a free-form string — the
agent picks one, the backend resolves deterministically. Closed-interval
contract means SQL filters use ``date >= from_date AND date <= to_date``
and single-day windows have ``from_date == to_date``.

``last_n_days(N)``, ``last_n_weeks(N)``, and ``last_n_months(N)`` all return
the trailing N-period interval **inclusive of today**, where N is the number
of calendar days, weeks (7-day rolling), or same-day-of-month spans:

  last_n_days(7)   on 2026-05-19 → (2026-05-13, 2026-05-19)  # 7 days
  last_n_weeks(2)  on 2026-05-19 → (2026-05-06, 2026-05-19)  # 14 days
  last_n_months(1) on 2026-05-19 → (2026-04-20, 2026-05-19)  # ~30 days

Same-day-of-month math clamps to the last day of the target month for short
months (e.g. ``last_n_months(1)`` on March 31 lands on Feb 28/29). Tenant
timezone is sourced via ``tenant.user.timezone`` by callers; this module
just takes the IANA name.

See ``CONTINUITY_agent-context-via-queries.md`` for the rationale.
"""

from __future__ import annotations

import calendar
import zoneinfo
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, model_validator

WindowKind = Literal[
    "today",
    "yesterday",
    "tomorrow",
    "all",
    "last_n_days",
    "next_n_days",
    "last_n_weeks",
    "last_n_months",
    "this_week",
    "last_week",
    "month_to_date",
    "last_month",
    "year_to_date",
    "last_year",
    "since",
    "between",
]


_KINDS_NO_VALUE = frozenset(
    {
        "today",
        "yesterday",
        "tomorrow",
        "all",
        "this_week",
        "last_week",
        "month_to_date",
        "last_month",
        "year_to_date",
        "last_year",
    }
)


class Window(BaseModel):
    """Agent-supplied time window.

    The discriminator is ``kind``; ``value`` is typed-by-kind and validated
    in ``_validate_value_per_kind``. Single Pydantic class (not a discriminated
    union of one-per-kind classes) keeps the JSON Schema the agent sees small
    and the tool description tractable.
    """

    kind: WindowKind
    value: int | date | list[date] | None = None

    @model_validator(mode="after")
    def _validate_value_per_kind(self) -> Window:
        k = self.kind
        if k in _KINDS_NO_VALUE:
            if self.value is not None:
                raise ValueError(f"kind={k!r} does not take a value")
            return self
        if k in ("last_n_days", "next_n_days"):
            if not isinstance(self.value, int) or isinstance(self.value, bool):
                raise ValueError(f"kind={k!r} requires integer value")
            if not (1 <= self.value <= 730):
                raise ValueError(f"kind={k!r} value must be 1..730")
            return self
        if k == "last_n_weeks":
            if not isinstance(self.value, int) or isinstance(self.value, bool):
                raise ValueError(f"kind={k!r} requires integer value")
            if not (1 <= self.value <= 104):
                raise ValueError(f"kind={k!r} value must be 1..104")
            return self
        if k == "last_n_months":
            if not isinstance(self.value, int) or isinstance(self.value, bool):
                raise ValueError(f"kind={k!r} requires integer value")
            if not (1 <= self.value <= 24):
                raise ValueError(f"kind={k!r} value must be 1..24")
            return self
        if k == "since":
            if not isinstance(self.value, date) or isinstance(self.value, datetime):
                raise ValueError("kind='since' requires a date value (YYYY-MM-DD)")
            return self
        if k == "between":
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError("kind='between' requires [from_date, to_date]")
            if not all(isinstance(d, date) and not isinstance(d, datetime) for d in self.value):
                raise ValueError("kind='between' requires two YYYY-MM-DD dates")
            if self.value[0] > self.value[1]:
                raise ValueError("kind='between' requires from_date <= to_date")
            return self
        raise ValueError(f"unhandled window kind: {k!r}")


def resolve_window(
    w: Window,
    tz_name: str,
    *,
    now: datetime | None = None,
) -> tuple[date, date] | None:
    """Resolve a ``Window`` into a closed date interval in the given timezone.

    Returns ``(from_date, to_date)`` inclusive, or ``None`` for ``kind='all'``
    (caller skips the date filter entirely in the resulting query).

    ``now`` is for testability — defaults to ``datetime.now(tz)``. When
    supplied naive, it is interpreted in ``tz_name``; when supplied aware,
    it is converted into ``tz_name``.
    """
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    today = now.date()
    k = w.kind

    if k == "all":
        return None
    if k == "today":
        return (today, today)
    if k == "yesterday":
        d = today - timedelta(days=1)
        return (d, d)
    if k == "tomorrow":
        d = today + timedelta(days=1)
        return (d, d)
    if k == "last_n_days":
        n = int(w.value)  # type: ignore[arg-type]
        return (today - timedelta(days=n - 1), today)
    if k == "next_n_days":
        n = int(w.value)  # type: ignore[arg-type]
        return (today, today + timedelta(days=n - 1))
    if k == "last_n_weeks":
        n = int(w.value) * 7  # type: ignore[arg-type]
        return (today - timedelta(days=n - 1), today)
    if k == "last_n_months":
        n = int(w.value)  # type: ignore[arg-type]
        return (_subtract_months(today, n) + timedelta(days=1), today)
    if k == "this_week":
        monday = today - timedelta(days=today.weekday())
        return (monday, monday + timedelta(days=6))
    if k == "last_week":
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        return (last_monday, last_monday + timedelta(days=6))
    if k == "month_to_date":
        return (today.replace(day=1), today)
    if k == "last_month":
        first_this_month = today.replace(day=1)
        last_last_month = first_this_month - timedelta(days=1)
        return (last_last_month.replace(day=1), last_last_month)
    if k == "year_to_date":
        return (date(today.year, 1, 1), today)
    if k == "last_year":
        return (date(today.year - 1, 1, 1), date(today.year - 1, 12, 31))
    if k == "since":
        return (w.value, today)  # type: ignore[return-value]
    if k == "between":
        return (w.value[0], w.value[1])  # type: ignore[index]

    raise ValueError(f"unhandled window kind: {k!r}")


def _subtract_months(d: date, months: int) -> date:
    """Return the date ``months`` months before ``d``, clamping the day to
    the last valid day of the target month.

    Used by ``last_n_months`` — the "from" date is then this result + 1 day,
    yielding a trailing same-calendar-period interval.
    """
    year = d.year
    month = d.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
