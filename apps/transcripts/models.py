"""Ciphertext-only transcript ledger models."""

import uuid

from django.db import models


class TranscriptEvent(models.Model):
    """One append-only user or assistant event in placeholder space."""

    class Role(models.TextChoices):
        USER = "user", "User"
        ASSISTANT = "assistant", "Assistant"

    class SourceType(models.TextChoices):
        IOS_QUEUED = "ios_queued", "iOS queued"
        IOS_ONDEVICE = "ios_ondevice", "iOS on-device"
        TELEGRAM_POLLER = "telegram_poller", "Telegram poller"
        TELEGRAM_WEBHOOK = "telegram_webhook", "Telegram webhook"
        LINE = "line", "LINE"
        BUFFERED = "buffered", "Buffered"
        ASSISTANT_REPLY = "assistant_reply", "Assistant reply"
        PROACTIVE = "proactive", "Proactive"

    class Channel(models.TextChoices):
        IOS = "ios", "iOS"
        TELEGRAM = "telegram", "Telegram"
        LINE = "line", "LINE"

    class DeliveryState(models.TextChoices):
        NONE = "", "Not applicable"
        SENT = "sent", "Sent"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        AMBIGUOUS = "ambiguous", "Ambiguous"

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="transcript_events",
    )
    turn_id = models.UUIDField(db_index=True)
    role = models.CharField(max_length=16, choices=Role.choices)
    source_type = models.CharField(max_length=24, choices=SourceType.choices)
    source_event_id = models.CharField(max_length=255)
    revision = models.PositiveSmallIntegerField(default=0)
    channel = models.CharField(max_length=16, choices=Channel.choices)
    thread_key = models.CharField(max_length=255, blank=True, default="")
    occurred_at = models.DateTimeField()
    captured_at = models.DateTimeField(auto_now_add=True)
    text_enc = models.BinaryField()
    content_hash = models.CharField(max_length=64)
    model_response_ref = models.CharField(max_length=255, blank=True, default="")
    delivery_state = models.CharField(
        max_length=16,
        choices=DeliveryState.choices,
        blank=True,
        default="",
    )
    delivered_chunks = models.PositiveSmallIntegerField(default=0)
    total_chunks = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "source_type", "source_event_id", "revision"),
                name="uq_tx_event_source_revision",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "thread_key", "occurred_at"),
                name="tx_event_thread_time_idx",
            ),
            models.Index(
                fields=("tenant", "occurred_at"),
                name="tx_event_tenant_time_idx",
            ),
        ]


class TranscriptCaptureQuarantine(models.Model):
    """Text-free record of content that could not be safely captured."""

    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="transcript_capture_quarantines",
    )
    source_type = models.CharField(max_length=24, choices=TranscriptEvent.SourceType.choices)
    source_event_id = models.CharField(max_length=255)
    turn_id = models.UUIDField(null=True, blank=True)
    channel = models.CharField(max_length=16, choices=TranscriptEvent.Channel.choices)
    thread_key = models.CharField(max_length=255, blank=True, default="")
    occurred_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.CharField(max_length=64)
    repair_ref = models.CharField(max_length=255, blank=True, default="")
    permanent_loss = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "source_type", "source_event_id"),
                name="uq_tx_quarantine_source",
            ),
        ]


class TranscriptIndexOutbox(models.Model):
    """Durable signal that a captured turn is ready for future indexing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="transcript_index_outbox_rows",
    )
    turn_id = models.UUIDField()
    thread_key = models.CharField(max_length=255, blank=True, default="")
    channel = models.CharField(max_length=16, choices=TranscriptEvent.Channel.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "turn_id"),
                name="uq_tx_outbox_turn",
            ),
        ]
