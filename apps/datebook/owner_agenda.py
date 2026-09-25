"""Owner-facing, read-only agenda for the web "This week" card.

Honest coverage: a day is ``covered`` only when the phone's last COMPLETE events
sync (full access, events consent) spans the whole tenant-local day. The web
may say "Free" only for covered days with no items; anything else is "Not
synced". Owner reads rehydrate placeholder-at-rest text for the owner only.
Calendars the owner excluded (``CalendarContext.included=False``) never show.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from django.utils import timezone

from apps.common.tenant_tz import tenant_tz
from apps.pii.redactor import rehydrate_for_tenant

from .agenda import agenda_items, agenda_window
from .models import AuthorizationStatus, CalendarContext, DatebookGateway

MAX_DAYS = 14
ITEM_LIMIT = 200
_REHYDRATED_KEYS = (
    "title",
    "location",
    "notes",
    "calendar_title",
    "list_title",
    "source_title",
    "display_text",
)


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _excluded_fingerprints(tenant) -> dict[str, set[str]]:
    rows = CalendarContext.objects.filter(tenant=tenant, included=False).values_list(
        "entity_scope", "calendar_fingerprint"
    )
    out: dict[str, set[str]] = {"event": set(), "reminder": set()}
    for scope, fingerprint in rows:
        out.setdefault(scope, set()).add(fingerprint)
    return out


def _events_coverage(tenant, gateway, start_at, end_at):
    """The honestly-covered part of [start_at, end_at) or None."""
    if not tenant.datebook_events_consent_at or gateway is None:
        return None
    if gateway.events_authorization != AuthorizationStatus.FULL_ACCESS:
        return None
    if not gateway.events_last_complete_sync_at or gateway.events_full_snapshot_required:
        return None
    if not (gateway.events_window_start and gateway.events_window_end):
        return None
    lo = max(start_at, gateway.events_window_start)
    hi = min(end_at, gateway.events_window_end)
    return (lo, hi) if lo < hi else None


def _covered_days(tenant, start_day, end_day, coverage) -> list[str]:
    if coverage is None:
        return []
    tz = tenant_tz(tenant)
    lo, hi = coverage
    days = []
    day = start_day
    while day < end_day:
        day_start = datetime.combine(day, time.min, tzinfo=tz)  # DST-safe: local midnights
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        if lo <= day_start and day_end <= hi:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def owner_agenda(tenant, *, days: int) -> dict:
    days = max(1, min(MAX_DAYS, days))
    gateway = DatebookGateway.objects.filter(tenant=tenant, status=DatebookGateway.Status.ACTIVE).first()
    start_day, end_day, start_at, end_at = agenda_window(tenant, days_back=0, days_ahead=days - 1)

    wants_events = bool(tenant.datebook_events_consent_at)
    wants_reminders = bool(tenant.datebook_reminders_consent_at)
    entity = "both" if wants_events and wants_reminders else "events" if wants_events else "reminders"
    items, truncated = agenda_items(tenant, days_back=0, days_ahead=days - 1, entity=entity, limit=ITEM_LIMIT)

    excluded = _excluded_fingerprints(tenant)
    visible = []
    for item in items:
        scope = "event" if item["entity"] == "event" else "reminder"
        if item.get("calendar_fingerprint") in excluded.get(scope, set()):
            continue
        represented = dict(item)
        for key in _REHYDRATED_KEYS:
            if isinstance(represented.get(key), str):
                represented[key] = rehydrate_for_tenant(tenant, represented[key])
        represented.pop("calendar_fingerprint", None)
        visible.append(represented)

    coverage = _events_coverage(tenant, gateway, start_at, end_at)
    return {
        "state": "ok",
        "server_now": _iso(timezone.now()),
        "timezone": str(tenant_tz(tenant)),
        "requested": {
            "start_day": start_day.isoformat(),
            "end_day_exclusive": end_day.isoformat(),
            "start_at": _iso(start_at),
            "end_at": _iso(end_at),
        },
        "covered": {"start_at": _iso(coverage[0]), "end_at": _iso(coverage[1])} if coverage else None,
        "covered_days": _covered_days(tenant, start_day, end_day, coverage),
        "freshness": {
            "events_last_complete_sync_at": _iso(gateway.events_last_complete_sync_at) if gateway else None,
            "reminders_last_complete_sync_at": _iso(gateway.reminders_last_complete_sync_at) if gateway else None,
            "events_authorization": gateway.events_authorization if gateway else "unavailable",
            "gateway_status": gateway.status if gateway else "unavailable",
        },
        "includes": {"events": wants_events, "reminders": wants_reminders},
        "items": visible,
        "truncated": truncated,
    }
