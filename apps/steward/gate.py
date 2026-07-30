from __future__ import annotations

import logging
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.steward.models import AlertState

logger = logging.getLogger(__name__)


def _locked_state(fingerprint: str) -> AlertState:
    try:
        return AlertState.objects.select_for_update().get(fingerprint=fingerprint)
    except AlertState.DoesNotExist:
        try:
            with transaction.atomic():
                return AlertState.objects.create(fingerprint=fingerprint)
        except IntegrityError:
            return AlertState.objects.select_for_update().get(fingerprint=fingerprint)


def should_send(fingerprint: str, cooldown: timedelta) -> bool:
    """Check whether an outbound alert is outside its cooldown window.

    Database failures fail open so a migration edge can increase noise but can
    never suppress an operational alert. This check does not reserve or stamp
    the window; callers must call ``record_sent`` only after successful delivery.
    """
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 128:
        raise ValueError("fingerprint must be a non-empty string of at most 128 characters.")
    if cooldown < timedelta(0):
        raise ValueError("cooldown must not be negative.")

    try:
        state = AlertState.objects.filter(fingerprint=fingerprint).first()
        if state is None or state.last_sent_at is None:
            return True
        return timezone.now() - state.last_sent_at >= cooldown
    except DatabaseError as exc:
        logger.warning(
            "Steward alert gate unavailable; failing open fingerprint=%s error_class=%s",
            fingerprint,
            type(exc).__name__,
        )
        return True


def record_sent(fingerprint: str) -> None:
    """Confirm a successful outbound delivery for future cooldown checks."""
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 128:
        raise ValueError("fingerprint must be a non-empty string of at most 128 characters.")
    try:
        with transaction.atomic():
            state = _locked_state(fingerprint)
            state.last_sent_at = timezone.now()
            state.sent_count += 1
            state.save(update_fields=["last_sent_at", "sent_count"])
    except DatabaseError as exc:
        logger.warning(
            "Steward alert delivery stamp unavailable fingerprint=%s error_class=%s",
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
