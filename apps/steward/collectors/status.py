from __future__ import annotations

from datetime import datetime, timedelta

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.steward.models import CollectorStatus
from apps.steward.sanitize import safe_text

COLLECTOR_LEASE_TTL = timedelta(minutes=10)


def _locked_status(collector: str, *, now: datetime) -> CollectorStatus:
    try:
        return CollectorStatus.objects.select_for_update().get(collector=collector)
    except CollectorStatus.DoesNotExist:
        try:
            with transaction.atomic():
                return CollectorStatus.objects.create(
                    collector=collector,
                    last_attempt_at=now,
                )
        except IntegrityError:
            return CollectorStatus.objects.select_for_update().get(collector=collector)


def acquire_collector_lease(
    collector: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    claimed_at = now or timezone.now()
    with transaction.atomic():
        status = _locked_status(collector, now=claimed_at)
        if status.held_until is not None and status.held_until > claimed_at:
            return None
        held_until = claimed_at + COLLECTOR_LEASE_TTL
        status.held_until = held_until
        status.save(update_fields=["held_until"])
    return held_until


def release_collector_lease(collector: str, held_until: datetime) -> None:
    CollectorStatus.objects.filter(
        collector=collector,
        held_until=held_until,
    ).update(held_until=None)


def set_persistence_timeouts() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout = '5s'")
        cursor.execute("SET LOCAL statement_timeout = '30s'")


def collector_succeeded(
    collector: str,
    *,
    attempted_at: datetime,
    detail: str = "",
) -> CollectorStatus:
    status, _ = CollectorStatus.objects.update_or_create(
        collector=collector,
        defaults={
            "last_success_at": attempted_at,
            "last_attempt_at": attempted_at,
            "last_error_class": "",
            "consecutive_failures": 0,
            "consecutive_truncations": 0,
            "detail": safe_text(detail, 200),
        },
    )
    return status


@transaction.atomic
def collector_failed(
    collector: str,
    *,
    attempted_at: datetime,
    error_class: str,
    detail: str = "",
) -> CollectorStatus:
    status, _ = CollectorStatus.objects.select_for_update().get_or_create(
        collector=collector,
        defaults={"last_attempt_at": attempted_at},
    )
    status.last_attempt_at = attempted_at
    status.last_error_class = safe_text(error_class, 60)
    status.consecutive_failures += 1
    if error_class == "truncated":
        status.consecutive_truncations += 1
    else:
        status.consecutive_truncations = 0
    status.detail = safe_text(detail, 200)
    status.save(
        update_fields=[
            "last_attempt_at",
            "last_error_class",
            "consecutive_failures",
            "consecutive_truncations",
            "detail",
        ]
    )
    return status
