from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.steward.models import AlertState

logger = logging.getLogger(__name__)
RESERVATION_TTL = timedelta(minutes=5)


def _locked_state(fingerprint: str) -> AlertState:
    try:
        return AlertState.objects.select_for_update().get(fingerprint=fingerprint)
    except AlertState.DoesNotExist:
        try:
            with transaction.atomic():
                return AlertState.objects.create(fingerprint=fingerprint)
        except IntegrityError:
            return AlertState.objects.select_for_update().get(fingerprint=fingerprint)


def should_send(fingerprint: str, cooldown: timedelta) -> datetime | None:
    """Atomically reserve an outbound alert outside its cooldown window.

    Database failures fail open so a migration edge can increase noise but can
    never suppress an operational alert. A successful caller must convert the
    reservation with ``record_sent`` or clear it with ``release_failed``.
    """
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 128:
        raise ValueError("fingerprint must be a non-empty string of at most 128 characters.")
    if cooldown < timedelta(0):
        raise ValueError("cooldown must not be negative.")

    try:
        now = timezone.now()
        with transaction.atomic():
            state = _locked_state(fingerprint)
            claimed = (
                AlertState.objects.filter(pk=state.pk)
                .filter(Q(last_sent_at__isnull=True) | Q(last_sent_at__lte=now - cooldown))
                .filter(Q(last_reserved_at__isnull=True) | Q(last_reserved_at__lt=now - RESERVATION_TTL))
                .update(last_reserved_at=now)
            )
        return now if claimed == 1 else None
    except DatabaseError as exc:
        logger.warning(
            "Steward alert gate unavailable; failing open fingerprint=%s error_class=%s",
            fingerprint,
            type(exc).__name__,
        )
        return now


def record_sent(fingerprint: str) -> None:
    """Confirm a successful outbound delivery for future cooldown checks."""
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 128:
        raise ValueError("fingerprint must be a non-empty string of at most 128 characters.")
    try:
        with transaction.atomic():
            state = _locked_state(fingerprint)
            state.last_sent_at = timezone.now()
            state.last_reserved_at = None
            state.sent_count += 1
            state.save(
                update_fields=[
                    "last_sent_at",
                    "last_reserved_at",
                    "sent_count",
                ]
            )
    except DatabaseError as exc:
        logger.warning(
            "Steward alert delivery stamp unavailable fingerprint=%s error_class=%s",
            fingerprint,
            type(exc).__name__,
        )


def release_failed(fingerprint: str, reservation: datetime) -> None:
    """Release only the caller's reservation after an unsuccessful delivery."""
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 128:
        raise ValueError("fingerprint must be a non-empty string of at most 128 characters.")
    if not isinstance(reservation, datetime):
        raise ValueError("reservation must be the datetime returned by should_send.")
    try:
        AlertState.objects.filter(
            fingerprint=fingerprint,
            last_reserved_at=reservation,
        ).update(
            last_reserved_at=None,
        )
    except DatabaseError as exc:
        logger.warning(
            "Steward alert reservation release unavailable fingerprint=%s error_class=%s",
            fingerprint,
            type(exc).__name__,
        )


def record_suppressed(fingerprint: str, *, count: int = 1) -> None:
    """Record eval runs withheld by a confirmed delivery cooldown."""
    if not isinstance(count, int) or count < 1:
        raise ValueError("count must be a positive integer.")
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 128:
        raise ValueError("fingerprint must be a non-empty string of at most 128 characters.")
    try:
        with transaction.atomic():
            state = _locked_state(fingerprint)
            AlertState.objects.filter(pk=state.pk).update(
                suppressed_count=F("suppressed_count") + count,
            )
    except DatabaseError as exc:
        logger.warning(
            "Steward alert suppression counter unavailable fingerprint=%s error_class=%s",
            fingerprint,
            type(exc).__name__,
        )
