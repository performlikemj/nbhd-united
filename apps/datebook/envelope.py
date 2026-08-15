"""Metadata-only Calendar & Reminders state for the workspace envelope."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from django.db.models import Q

from apps.common.tenant_tz import tenant_today, tenant_tz
from apps.orchestrator.envelope_registry import register_section

from .agenda import event_window_filter
from .models import DatebookGateway, DueKind, MirrorEvent, MirrorReminder, TimeKind
from .readiness import datebook_delivery_ready

_DAY_COUNT = 8
_QUERY_CAP = 1000
_HARD_BUDGET = 1200
_OMITTED_DAYS_LINE = "- (further days omitted — call nbhd_datebook_read)"


def _time_label(value: datetime, *, end_of_day: bool = False) -> str:
    return "24:00" if end_of_day and value.time() == time.min else value.strftime("%H:%M")


def _busy_blocks(tenant) -> list[str]:
    today = tenant_today(tenant)
    tz = tenant_tz(tenant)
    end_day = today + timedelta(days=_DAY_COUNT)
    start_at = datetime.combine(today, time.min, tzinfo=tz).astimezone(UTC)
    end_at = datetime.combine(end_day, time.min, tzinfo=tz).astimezone(UTC)
    events = list(
        MirrorEvent.objects.filter(tenant=tenant, active=True)
        .filter(
            event_window_filter(
                start_day=today,
                end_day=end_day,
                start_at=start_at,
                end_at=end_at,
            )
        )
        .order_by("id")[:_QUERY_CAP]
    )
    by_day: dict = {today + timedelta(days=offset): [] for offset in range(_DAY_COUNT)}
    for event in events:
        for day, blocks in by_day.items():
            day_end = day + timedelta(days=1)
            if event.time_kind == TimeKind.ALL_DAY:
                if event.all_day_start_date < day_end and event.all_day_end_date_exclusive > day:
                    blocks.append("all-day")
                continue
            if event.time_kind == TimeKind.ZONED:
                local_start = event.zoned_start_at.astimezone(tz)
                local_end = event.zoned_end_at.astimezone(tz)
            else:
                local_start = datetime.combine(
                    event.floating_start_date,
                    event.floating_start_time,
                    tzinfo=tz,
                )
                local_end = datetime.combine(
                    event.floating_end_date,
                    event.floating_end_time,
                    tzinfo=tz,
                )
            day_start_at = datetime.combine(day, time.min, tzinfo=tz)
            day_end_at = datetime.combine(day_end, time.min, tzinfo=tz)
            if local_start < day_end_at and local_end > day_start_at:
                clipped_start = max(local_start, day_start_at)
                clipped_end = min(local_end, day_end_at)
                blocks.append(f"{_time_label(clipped_start)}–{_time_label(clipped_end, end_of_day=True)}")

    lines = []
    for day in by_day:
        blocks = sorted(by_day[day])
        if not blocks:
            lines.append(f"- {day.isoformat()}: free (0 busy)")
            continue
        visible = ", ".join(blocks[:12])
        if len(blocks) > 12:
            visible += f", +{len(blocks) - 12} more"
        lines.append(f"- {day.isoformat()}: {len(blocks)} busy — {visible}")
    return lines


def _reminder_counts(tenant) -> tuple[int, int]:
    today = tenant_today(tenant)
    tz = tenant_tz(tenant)
    day_start = datetime.combine(today, time.min, tzinfo=tz).astimezone(UTC)
    day_end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
    base = MirrorReminder.objects.filter(tenant=tenant, active=True, completed=False)
    overdue = base.filter(
        Q(due_kind=DueKind.ALL_DAY, due_date__lt=today)
        | Q(due_kind=DueKind.ZONED, zoned_due_at__lt=day_start)
        | Q(due_kind=DueKind.FLOATING, floating_due_date__lt=today)
    ).count()
    due_today = base.filter(
        Q(due_kind=DueKind.ALL_DAY, due_date=today)
        | Q(due_kind=DueKind.ZONED, zoned_due_at__gte=day_start, zoned_due_at__lt=day_end)
        | Q(due_kind=DueKind.FLOATING, floating_due_date=today)
    ).count()
    return overdue, due_today


def _freshness_line(tenant) -> str:
    gateway = DatebookGateway.objects.filter(tenant=tenant, status=DatebookGateway.Status.ACTIVE).first()
    if gateway is None:
        return "- Sync: gateway unavailable; Calendar unavailable, synced never; Reminders unavailable, synced never"
    event_sync = gateway.events_last_complete_sync_at.isoformat() if gateway.events_last_complete_sync_at else "never"
    reminder_sync = (
        gateway.reminders_last_complete_sync_at.isoformat() if gateway.reminders_last_complete_sync_at else "never"
    )
    return (
        f"- Sync: gateway {gateway.status}; Calendar {gateway.events_authorization}, synced {event_sync}; "
        f"Reminders {gateway.reminders_authorization}, synced {reminder_sync}"
    )


@register_section(
    key="datebook",
    heading="## Calendar & Reminders",
    enabled=datebook_delivery_ready,
    refresh_on=(MirrorEvent, MirrorReminder),
    order=45,
)
def render_datebook(tenant, *, max_chars: int = _HARD_BUDGET) -> str:
    """Render no content fields: only busy metadata, counts, and absolute freshness."""

    overdue, due_today = _reminder_counts(tenant)
    prefix = [
        "These blocks are availability metadata only — no titles, not answerable content.",
        "For ANY question about calendar, schedule, events, availability, birthdays, reminders, "
        "to-dos, or task completion, you MUST call `nbhd_datebook_read` this turn and answer only "
        "from its result.",
        "Never answer schedule questions from memory or from these blocks.",
        'For user-authored "remind me" requests — including bare or ambiguous wording — '
        "you MUST call `nbhd_datebook_add_apple_reminder` by default.",
        "Use `nbhd_cron_create_pure_reminder` ONLY for an explicit in-chat ping, nudge, or message, "
        "or an inherently conversational recurring check-in. If genuinely unsure, choose the "
        "approval-gated Apple reminder.",
        "**Busy blocks — today + next 7 days**",
    ]
    suffix = [
        f"- Reminders: {overdue} overdue; {due_today} due today",
        _freshness_line(tenant),
    ]
    busy_lines = _busy_blocks(tenant)
    budget = max(0, min(max_chars, _HARD_BUDGET))

    body = "\n".join([*prefix, *busy_lines, *suffix])
    if len(body) <= budget:
        return body

    while busy_lines:
        busy_lines.pop()
        body = "\n".join([*prefix, *busy_lines, _OMITTED_DAYS_LINE, *suffix])
        if len(body) <= budget:
            return body

    return "\n".join([*prefix, _OMITTED_DAYS_LINE, *suffix])
