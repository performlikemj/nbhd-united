from __future__ import annotations

import logging
from datetime import timedelta

from django.db import DatabaseError, IntegrityError, transaction
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
    """Atomically grant one outbound alert per fingerprint and cooldown window.

    Database failures fail open so a migration edge can increase noise but can
    never suppress an operational alert.
    """
    if not isinstance(fingerprint, str) or not fingerprint or len(fingerprint) > 128:
        raise ValueError("fingerprint must be a non-empty string of at most 128 characters.")
    if cooldown < timedelta(0):
        raise ValueError("cooldown must not be negative.")

    try:
        with transaction.atomic():
            state = _locked_state(fingerprint)
            now = timezone.now()
            if state.last_sent_at is not None and now - state.last_sent_at < cooldown:
                return False
            state.last_sent_at = now
            state.sent_count += 1
            state.save(update_fields=["last_sent_at", "sent_count"])
            return True
    except DatabaseError as exc:
        logger.warning(
            "Steward alert gate unavailable; failing open fingerprint=%s error_class=%s",
            fingerprint,
            type(exc).__name__,
        )
        return True
