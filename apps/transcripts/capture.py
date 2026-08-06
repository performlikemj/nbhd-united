"""Fail-closed transcript capture and text-free quarantine APIs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from django.db import transaction

from apps.crypto import box
from apps.pii.redactor import (
    _CONFIRMED_REDACTION_TOKEN,
    ConfirmedRedaction,
    RedactionOutcome,
)

from .enc_columns import TRANSCRIPT_EVENT_TEXT
from .models import TranscriptCaptureQuarantine, TranscriptEvent, TranscriptIndexOutbox

_PERMANENT_LOSS_SOURCES = frozenset(
    {
        TranscriptEvent.SourceType.TELEGRAM_POLLER,
        TranscriptEvent.SourceType.TELEGRAM_WEBHOOK,
        TranscriptEvent.SourceType.LINE,
    }
)


@dataclass(frozen=True)
class EncryptedText:
    """Pre-transaction transcript ciphertext derived from confirmed text."""

    ciphertext: bytes
    content_hash: str
    _provenance: object = field(repr=False, compare=False)


def _require_confirmed(redaction: ConfirmedRedaction) -> None:
    if not isinstance(redaction, ConfirmedRedaction):
        raise TypeError("redaction must be an engine-issued ConfirmedRedaction")
    if redaction._provenance is not _CONFIRMED_REDACTION_TOKEN:
        raise ValueError("redaction provenance is invalid")


def _require_encrypted(value: EncryptedText) -> None:
    if value._provenance is not _CONFIRMED_REDACTION_TOKEN:
        raise ValueError("encrypted text provenance is invalid")


def encrypt_transcript_text(tenant, confirmed: ConfirmedRedaction) -> EncryptedText:
    """Encrypt confirmed text before a caller opens its durable turn transaction."""
    _require_confirmed(confirmed)
    table, column = TRANSCRIPT_EVENT_TEXT
    ciphertext = box.encrypt(tenant.id, table, column, confirmed.text)
    if ciphertext is None:  # pragma: no cover - ConfirmedRedaction.text is a str
        raise ValueError("confirmed transcript text could not be encrypted")
    return EncryptedText(
        ciphertext=ciphertext,
        content_hash=hashlib.sha256(confirmed.text.encode("utf-8")).hexdigest(),
        _provenance=_CONFIRMED_REDACTION_TOKEN,
    )


def capture_transcript_event(
    *,
    tenant,
    source_type: str,
    source_event_id: str,
    role: str,
    channel: str,
    turn_id: UUID,
    occurred_at: datetime,
    redaction: ConfirmedRedaction | EncryptedText,
    revision: int = 0,
    thread_key: str = "",
    delivery_state: str = "",
    delivered_chunks: int = 0,
    total_chunks: int = 0,
    model_response_ref: str = "",
) -> TranscriptEvent | None:
    """Idempotently persist one event and its per-turn index outbox row.

    Callers that already own a durable-turn transaction must call
    :func:`encrypt_transcript_text` before entering it and pass the returned
    :class:`EncryptedText`, keeping the external key broker out of the open
    transaction.
    """
    if not getattr(tenant, "recall_capture_enabled", False):
        return None

    if isinstance(redaction, ConfirmedRedaction):
        encrypted = encrypt_transcript_text(tenant, redaction)
    elif isinstance(redaction, EncryptedText):
        _require_encrypted(redaction)
        encrypted = redaction
    else:
        raise TypeError("redaction must be ConfirmedRedaction or EncryptedText")

    with transaction.atomic():
        event, _created = TranscriptEvent.objects.get_or_create(
            tenant=tenant,
            source_type=source_type,
            source_event_id=source_event_id,
            revision=revision,
            defaults={
                "turn_id": turn_id,
                "role": role,
                "channel": channel,
                "thread_key": thread_key,
                "occurred_at": occurred_at,
                "text_enc": encrypted.ciphertext,
                "content_hash": encrypted.content_hash,
                "model_response_ref": model_response_ref,
                "delivery_state": delivery_state,
                "delivered_chunks": delivered_chunks,
                "total_chunks": total_chunks,
            },
        )
        TranscriptIndexOutbox.objects.get_or_create(
            tenant=tenant,
            turn_id=event.turn_id,
            defaults={"thread_key": event.thread_key, "channel": event.channel},
        )
    return event


def quarantine_transcript_event(
    *,
    tenant,
    source_type: str,
    source_event_id: str,
    channel: str,
    outcome: RedactionOutcome,
    turn_id: UUID | None = None,
    occurred_at: datetime | None = None,
    thread_key: str = "",
    repair_ref: str = "",
) -> TranscriptCaptureQuarantine | None:
    """Persist metadata about an unconfirmed outcome without touching its text."""
    if not getattr(tenant, "recall_capture_enabled", False):
        return None
    if not isinstance(outcome, RedactionOutcome):
        raise TypeError("outcome must be a RedactionOutcome")
    if outcome.confirmed is True:
        raise ValueError("confirmed outcomes must use capture_transcript_event")

    row, created = TranscriptCaptureQuarantine.objects.get_or_create(
        tenant=tenant,
        source_type=source_type,
        source_event_id=source_event_id,
        defaults={
            "turn_id": turn_id,
            "channel": channel,
            "thread_key": thread_key,
            "occurred_at": occurred_at,
            "reason": outcome.reason,
            "repair_ref": repair_ref,
            "permanent_loss": source_type in _PERMANENT_LOSS_SOURCES,
        },
    )
    if created:
        tenant_id = tenant.id

        def alert_after_commit() -> None:
            from apps.tenants.models import Tenant

            from .alerts import check_quarantine_alerts

            check_quarantine_alerts(Tenant.objects.get(pk=tenant_id))

        transaction.on_commit(alert_after_commit)
    return row
