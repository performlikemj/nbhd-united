"""Shared building blocks for deterministic operations behind LLM tools.

Two responsibilities:

  1. ``resolve_relative_date`` — translate phrases like "today",
     "yesterday", "Monday", or an ISO date string into a concrete
     ``date`` value **in the user's timezone**. The LLM used to do this
     from a ``[Now: ...]`` UTC header in the prompt, which mis-fired
     near midnight in the user's local time (Bug #3 in the
     2026-05-16 session).

  2. ``LLMValidationError`` — a uniform tool-result envelope for
     surfacing Pydantic validation failures back to the LLM in a shape
     it can read and self-correct from. Used by per-plugin runtime
     views that adopt the deterministic-ops pattern.

Both are used by ``apps.fuel`` first; rolling out to other plugins is
follow-up work tracked in ``CONTINUITY_deterministic-ops.md``.
"""

from __future__ import annotations

import re
import zoneinfo
from datetime import date, timedelta
from typing import Any

from django.utils import timezone as dj_tz
from pydantic import BaseModel, ValidationError

_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    # Common 3-letter abbreviations.
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

_DAYS_AGO_RE = re.compile(r"^(\d+)\s+days?\s+ago$")
_IN_N_DAYS_RE = re.compile(r"^(?:in\s+)?(\d+)\s+days?(?:\s+from\s+now)?$")


def resolve_relative_date(tenant: Any, phrase: str) -> date | None:
    """Resolve a phrase to an absolute ``date`` in the tenant user's tz.

    Recognised forms:

      - ``"today"`` / ``""`` / ``None``  → today in user's tz
      - ``"yesterday"``                  → today − 1 day
      - ``"tomorrow"``                   → today + 1 day
      - ``"Monday"``, ``"mon"``, etc.    → most recent past weekday
        (if today is that weekday, returns *last* week)
      - ``"3 days ago"``                 → today − 3 days
      - ``"in 5 days"`` / ``"5 days"``   → today + 5 days
      - ``"2026-05-17"``                 → that ISO date verbatim

    Returns ``None`` for anything else — caller should ask the user.

    The tenant's ``user.timezone`` field is consulted, falling back to
    UTC if the user has no timezone set or the value is not a known IANA
    zone. ``tenant`` is typed as ``Any`` to keep this module free of a
    direct apps.tenants import.
    """
    tz = _tenant_zone(tenant)
    today = dj_tz.now().astimezone(tz).date()

    if phrase is None:
        return today
    p = str(phrase).strip().lower()
    if p in ("", "today", "now"):
        return today
    if p == "yesterday":
        return today - timedelta(days=1)
    if p == "tomorrow":
        return today + timedelta(days=1)

    # ISO date — most specific, try before vague phrases.
    try:
        return date.fromisoformat(p)
    except ValueError:
        pass

    if p in _WEEKDAY_INDEX:
        days_back = (today.weekday() - _WEEKDAY_INDEX[p]) % 7
        if days_back == 0:
            days_back = 7  # "Monday" said on Monday means last Monday
        return today - timedelta(days=days_back)

    if m := _DAYS_AGO_RE.match(p):
        return today - timedelta(days=int(m.group(1)))

    if m := _IN_N_DAYS_RE.match(p):
        return today + timedelta(days=int(m.group(1)))

    return None


def today_in_tenant_tz(tenant: Any) -> date:
    """Shortcut for ``resolve_relative_date(tenant, "today")``."""
    return dj_tz.now().astimezone(_tenant_zone(tenant)).date()


def _tenant_zone(tenant: Any) -> zoneinfo.ZoneInfo:
    """Return the user's IANA timezone, or UTC if unset/invalid."""
    name = "UTC"
    user = getattr(tenant, "user", None)
    if user is not None:
        candidate = getattr(user, "timezone", None)
        if candidate:
            name = str(candidate)
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return zoneinfo.ZoneInfo("UTC")


# ── Validation error envelope ──────────────────────────────────────────


class LLMValidationError(BaseModel):
    """Tool-result envelope for validation failures the LLM can correct.

    Pydantic's default ``ValidationError`` is human-formatted. The LLM
    handles structured data better than English prose, so we serialise
    each error individually with the field path and Pydantic's machine
    type code (``missing``, ``value_error``, ``literal_error``, etc.).

    Returned by runtime views as JSON when an inbound tool call fails
    Pydantic validation. The LLM sees this in its tool-result message
    and is instructed (via ``rules/_principles.md``) to retry with a
    corrected payload.
    """

    error: str = "validation_failed"
    message: str
    details: list[dict[str, Any]]

    @classmethod
    def from_pydantic(cls, exc: ValidationError) -> LLMValidationError:
        details = [
            {
                "loc": list(err["loc"]),
                "msg": err["msg"],
                "type": err["type"],
            }
            for err in exc.errors()
        ]
        count = len(details)
        plural = "issue" if count == 1 else "issues"
        return cls(
            message=(
                f"Tool input failed validation: {count} {plural}. "
                "Read the `details` array, correct the offending fields, and retry."
            ),
            details=details,
        )

    def as_tool_result(self) -> dict[str, Any]:
        """Shape matches the existing apps.fuel runtime view error shape."""
        return self.model_dump()
