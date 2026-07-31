from __future__ import annotations

from datetime import datetime

from django.db import transaction

from apps.steward.models import CollectorStatus
from apps.steward.sanitize import safe_text


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
