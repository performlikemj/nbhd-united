"""Bounded, tenant-timezone agenda projection over active mirror rows."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from django.db.models import Q

from apps.common.tenant_tz import tenant_today, tenant_tz

from .models import CalendarContext, DueKind, MirrorEvent, MirrorReminder, TimeKind


def agenda_window(tenant, *, days_back: int, days_ahead: int):
    tz = tenant_tz(tenant)
    today = tenant_today(tenant)
    start_day = today - timedelta(days=days_back)
    end_day = today + timedelta(days=days_ahead + 1)
    start_at = datetime.combine(start_day, time.min, tzinfo=tz).astimezone(UTC)
    end_at = datetime.combine(end_day, time.min, tzinfo=tz).astimezone(UTC)
    return start_day, end_day, start_at, end_at


def event_window_filter(*, start_day, end_day, start_at, end_at) -> Q:
    return (
        Q(
            time_kind=TimeKind.ALL_DAY,
            all_day_start_date__lt=end_day,
            all_day_end_date_exclusive__gt=start_day,
        )
        | Q(
            time_kind=TimeKind.ZONED,
            zoned_start_at__lt=end_at,
            zoned_end_at__gt=start_at,
        )
        | Q(
            time_kind=TimeKind.FLOATING,
            floating_start_date__lt=end_day,
            floating_end_date__gte=start_day,
        )
    )


def reminder_window_filter(*, start_day, end_day, start_at, end_at) -> Q:
    return (
        Q(due_kind=DueKind.ALL_DAY, due_date__gte=start_day, due_date__lt=end_day)
        | Q(due_kind=DueKind.ZONED, zoned_due_at__gte=start_at, zoned_due_at__lt=end_at)
        | Q(
            due_kind=DueKind.FLOATING,
            floating_due_date__gte=start_day,
            floating_due_date__lt=end_day,
        )
    )


def _iso_utc(value) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _event_projection(event: MirrorEvent, tenant) -> tuple[datetime, dict]:
    tz = tenant_tz(tenant)
    if event.time_kind == TimeKind.ALL_DAY:
        sort_at = datetime.combine(event.all_day_start_date, time.min, tzinfo=tz)
        bucket = event.all_day_start_date.isoformat()
        tagged_time = {
            "kind": "all_day",
            "start_date": event.all_day_start_date.isoformat(),
            "end_date_exclusive": event.all_day_end_date_exclusive.isoformat(),
        }
    elif event.time_kind == TimeKind.ZONED:
        sort_at = event.zoned_start_at.astimezone(tz)
        bucket = sort_at.date().isoformat()
        tagged_time = {
            "kind": "zoned",
            "start_at": _iso_utc(event.zoned_start_at),
            "end_at": _iso_utc(event.zoned_end_at),
            "tz_id": event.tz_id,
        }
    else:
        start_local = datetime.combine(event.floating_start_date, event.floating_start_time)
        end_local = datetime.combine(event.floating_end_date, event.floating_end_time)
        sort_at = start_local.replace(tzinfo=tz)
        bucket = event.floating_start_date.isoformat()
        tagged_time = {
            "kind": "floating",
            "start_local": start_local.isoformat(),
            "end_local": end_local.isoformat(),
        }
    display_text = event.title
    if event.calendar_title:
        display_text = f"{display_text} — {event.calendar_title}" if display_text else event.calendar_title
    return sort_at, {
        "entity": "event",
        "id": str(event.id),
        "day": bucket,
        "time": tagged_time,
        "title": event.title,
        "location": event.location,
        "notes": event.notes,
        "calendar_title": event.calendar_title,
        "calendar_fingerprint": event.calendar_fingerprint,
        "source_title": event.source_title,
        "display_text": display_text,
        "authorization": event.authorization_status,
        "read_only": event.is_read_only,
    }


def _reminder_projection(reminder: MirrorReminder, tenant) -> tuple[datetime, dict]:
    tz = tenant_tz(tenant)
    if reminder.due_kind == DueKind.ALL_DAY:
        sort_at = datetime.combine(reminder.due_date, time.min, tzinfo=tz)
        bucket = reminder.due_date.isoformat()
        due = {"kind": "all_day", "date": reminder.due_date.isoformat()}
    elif reminder.due_kind == DueKind.ZONED:
        sort_at = reminder.zoned_due_at.astimezone(tz)
        bucket = sort_at.date().isoformat()
        due = {
            "kind": "zoned",
            "due_at": _iso_utc(reminder.zoned_due_at),
            "tz_id": reminder.due_tz_id,
        }
    else:
        due_local = datetime.combine(reminder.floating_due_date, reminder.floating_due_time)
        sort_at = due_local.replace(tzinfo=tz)
        bucket = reminder.floating_due_date.isoformat()
        due = {"kind": "floating", "due_local": due_local.isoformat()}
    display_text = reminder.title
    if reminder.list_title:
        display_text = f"{display_text} — {reminder.list_title}" if display_text else reminder.list_title
    return sort_at, {
        "entity": "reminder",
        "id": str(reminder.id),
        "day": bucket,
        "due": due,
        "title": reminder.title,
        "location": reminder.location,
        "notes": reminder.notes,
        "list_title": reminder.list_title,
        "calendar_fingerprint": reminder.calendar_fingerprint,
        "source_title": reminder.source_title,
        "display_text": display_text,
        "priority": reminder.priority,
        "authorization": reminder.authorization_status,
        "read_only": reminder.is_read_only,
    }


def _kind_rows(queryset, kind_field: str, kinds: tuple[str, ...], orders: dict[str, tuple[str, ...]], limit: int):
    rows = []
    hit_bound = False
    for kind in kinds:
        chunk = list(queryset.filter(**{kind_field: kind}).order_by(*orders[kind])[: limit + 1])
        hit_bound = hit_bound or len(chunk) > limit
        rows.extend(chunk[:limit])
    return rows, hit_bound


def agenda_items(tenant, *, days_back: int, days_ahead: int, entity: str, limit: int):
    start_day, end_day, start_at, end_at = agenda_window(
        tenant,
        days_back=days_back,
        days_ahead=days_ahead,
    )
    projected: list[tuple[datetime, dict]] = []
    hit_bound = False
    if entity in {"events", "both"}:
        events = MirrorEvent.objects.filter(tenant=tenant, active=True).filter(
            event_window_filter(
                start_day=start_day,
                end_day=end_day,
                start_at=start_at,
                end_at=end_at,
            )
        )
        event_rows, event_bound = _kind_rows(
            events,
            "time_kind",
            (TimeKind.ALL_DAY, TimeKind.ZONED, TimeKind.FLOATING),
            {
                TimeKind.ALL_DAY: ("all_day_start_date", "all_day_end_date_exclusive", "id"),
                TimeKind.ZONED: ("zoned_start_at", "zoned_end_at", "id"),
                TimeKind.FLOATING: ("floating_start_date", "floating_start_time", "id"),
            },
            limit,
        )
        projected.extend(_event_projection(row, tenant) for row in event_rows)
        hit_bound = hit_bound or event_bound
    if entity in {"reminders", "both"}:
        reminders = MirrorReminder.objects.filter(tenant=tenant, active=True, completed=False).filter(
            reminder_window_filter(
                start_day=start_day,
                end_day=end_day,
                start_at=start_at,
                end_at=end_at,
            )
        )
        reminder_rows, reminder_bound = _kind_rows(
            reminders,
            "due_kind",
            (DueKind.ALL_DAY, DueKind.ZONED, DueKind.FLOATING),
            {
                DueKind.ALL_DAY: ("due_date", "id"),
                DueKind.ZONED: ("zoned_due_at", "id"),
                DueKind.FLOATING: ("floating_due_date", "floating_due_time", "id"),
            },
            limit,
        )
        projected.extend(_reminder_projection(row, tenant) for row in reminder_rows)
        hit_bound = hit_bound or reminder_bound
    projected.sort(key=lambda item: (item[0], item[1]["entity"], item[1]["id"]))
    truncated = hit_bound or len(projected) > limit
    return [item for _sort, item in projected[:limit]], truncated


def agenda_calendar_context(tenant, *, entity: str) -> list[dict]:
    scopes = []
    if entity in {"events", "both"}:
        scopes.append(CalendarContext.EntityScope.EVENT)
    if entity in {"reminders", "both"}:
        scopes.append(CalendarContext.EntityScope.REMINDER)
    rows = CalendarContext.objects.filter(
        tenant=tenant,
        included=True,
        entity_scope__in=scopes,
    ).exclude(context_note="")
    return [
        {
            "calendar_fingerprint": row.calendar_fingerprint,
            "entity_scope": row.entity_scope,
            "container_title": row.container_title,
            "source_title": row.source_title,
            "context_note": row.context_note,
        }
        for row in rows.order_by("entity_scope", "calendar_fingerprint")
    ]
