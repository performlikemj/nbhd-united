"""Canonical tenant-timezone resolution — the single front door.

Two helpers cover the two shapes the rest of the codebase reaches for:

  - ``tenant_tz_name(tenant)`` → IANA name string. Use when passing tz to
    something that takes a string (``resolve_window``, JSON metadata, DB
    queries that store tz as text).

  - ``tenant_tz(tenant)`` → ``ZoneInfo`` instance. Use for ``astimezone()``.

If you already have a string name from somewhere else and need a ZoneInfo,
use ``safe_zoneinfo(name)`` — it returns UTC for unknown / missing names
instead of raising.

Both fall back to ``"UTC"`` when ``tenant.user`` is missing, the timezone
field is empty, or the value is not a known IANA zone.

Do not write a fourth private ``_tenant_zone`` / ``_tenant_tz`` helper.
Range math lives in ``apps.common.windows``; point math lives in
``apps.common.llm_contracts``; both source their tz through this module.
"""

from __future__ import annotations

import datetime
import zoneinfo
from typing import Any

_UTC = zoneinfo.ZoneInfo("UTC")


def tenant_today(tenant: Any) -> datetime.date:
    """The current calendar date in the tenant's LOCAL timezone (UTC fallback).

    The "daily" boundary for per-tenant features — one meditation a day, the
    "today's sit" check, the weekly window — must be the user's local midnight,
    not the server's UTC midnight. Driven by ``tenant.user.timezone``; falls back
    to UTC when unset/invalid. (``tenant_tz`` is defined below; resolved at call time.)
    """
    from django.utils import timezone

    return timezone.now().astimezone(tenant_tz(tenant)).date()


def tenant_tz_name(tenant: Any) -> str:
    """Return the tenant user's IANA timezone name, or ``"UTC"``.

    ``tenant`` is typed ``Any`` to keep this module import-free of
    ``apps.tenants``; callers pass a ``Tenant`` in practice. An unknown
    IANA name is treated as missing and falls back to UTC.
    """
    user = getattr(tenant, "user", None)
    if user is None:
        return "UTC"
    candidate = getattr(user, "timezone", None)
    if not candidate:
        return "UTC"
    name = str(candidate)
    try:
        zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return "UTC"
    return name


def tenant_tz(tenant: Any) -> zoneinfo.ZoneInfo:
    """Return the tenant user's ``ZoneInfo``, or UTC if unset/invalid."""
    return safe_zoneinfo(tenant_tz_name(tenant))


def safe_zoneinfo(name: str | None) -> zoneinfo.ZoneInfo:
    """Return ``ZoneInfo(name)``, or UTC if ``name`` is missing or unknown."""
    if not name:
        return _UTC
    try:
        return zoneinfo.ZoneInfo(name)
    except zoneinfo.ZoneInfoNotFoundError:
        return _UTC
