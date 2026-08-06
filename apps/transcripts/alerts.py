"""Metadata-only quarantine-rate and permanent-capture-loss alerts."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db import connection, transaction
from django.db.models import Count
from django.utils import timezone

from apps.steward.gate import (
    record_sent,
    record_suppressed,
    release_failed,
    should_send,
)

from .models import TranscriptCaptureQuarantine, TranscriptEvent

logger = logging.getLogger(__name__)
_RATE_COOLDOWN = timedelta(hours=24)
_PERMANENT_LOSS_COOLDOWN = timedelta(hours=1)


def _send_alert(*, fingerprint: str, cooldown: timedelta, subject: str, body: str) -> bool:
    owner_email = getattr(settings, "PLATFORM_OWNER_EMAIL", "")
    if not owner_email:
        logger.warning("transcript quarantine alert skipped: PLATFORM_OWNER_EMAIL is not set")
        return False

    reservation = should_send(fingerprint, cooldown)
    if reservation is None:
        record_suppressed(fingerprint)
        return False
    try:
        sent = send_mail(
            subject=subject,
            message=body,
            from_email=None,
            recipient_list=[owner_email],
            fail_silently=False,
        )
    except Exception:
        release_failed(fingerprint, reservation)
        logger.exception("transcript quarantine alert delivery failed fingerprint=%s", fingerprint)
        return False
    if sent == 0:
        release_failed(fingerprint, reservation)
        logger.error("transcript quarantine alert backend returned zero fingerprint=%s", fingerprint)
        return False
    record_sent(fingerprint)
    return True


def check_quarantine_alerts(tenant) -> None:
    """Check the trailing-24h rate and permanent-loss conditions for a tenant."""
    since = timezone.now() - timedelta(hours=24)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                [str(tenant.id)],
            )
        quarantines = TranscriptCaptureQuarantine.objects.filter(
            tenant=tenant,
            created_at__gte=since,
        )
        quarantine_count = quarantines.count()
        event_count = TranscriptEvent.objects.filter(
            tenant=tenant,
            captured_at__gte=since,
        ).count()
        permanent_by_source = list(
            quarantines.filter(permanent_loss=True)
            .values("source_type")
            .annotate(count=Count("id"))
            .order_by("source_type")
        )
    denominator = quarantine_count + event_count

    if denominator >= 20 and quarantine_count * 100 > denominator:
        _send_alert(
            fingerprint=f"transcript-quarantine-rate:{tenant.id}",
            cooldown=_RATE_COOLDOWN,
            subject="[TRANSCRIPTS] Quarantine rate above 1%",
            body=(
                f"Tenant ID: {tenant.id}\n"
                f"Window: trailing 24h\n"
                f"Captured events: {event_count}\n"
                f"Quarantines: {quarantine_count}\n"
                f"Total attempts: {denominator}\n"
            ),
        )

    for item in permanent_by_source:
        source_type = item["source_type"]
        _send_alert(
            fingerprint=f"transcript-permanent-loss:{tenant.id}:{source_type}",
            cooldown=_PERMANENT_LOSS_COOLDOWN,
            subject="[TRANSCRIPTS] Permanent capture loss detected",
            body=(
                f"Tenant ID: {tenant.id}\n"
                f"Window: trailing 24h\n"
                f"Source type: {source_type}\n"
                f"Permanent-loss quarantines: {item['count']}\n"
            ),
        )
