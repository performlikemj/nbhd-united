"""Router models — message buffering for idle-hibernated tenants and
per-tenant serialization for warm-tenant rapid-fire messages."""

import uuid

from django.db import models


class BufferedMessage(models.Model):
    """Messages received while a tenant's container was hibernated.

    When a user sends a message to a hibernated container, the raw webhook
    payload is stored here. After the container wakes (~45s), a QStash task
    forwards all buffered messages in order, then marks them delivered.
    """

    class Channel(models.TextChoices):
        TELEGRAM = "telegram"
        LINE = "line"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="buffered_messages",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    payload = models.JSONField(
        help_text="Raw webhook payload (Telegram update or LINE event)",
    )
    user_text = models.TextField(
        blank=True,
        default="",
        help_text="Extracted user message text for logging (truncated)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    delivered = models.BooleanField(default=False)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Status(models.TextChoices):
        PENDING = "pending"
        DELIVERED = "delivered"
        FAILED = "failed"

    delivery_attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of times deliver_buffered_messages has tried and failed to deliver this message.",
    )
    delivery_status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="Terminal state: 'delivered' on success, 'failed' after attempts cap reached.",
    )
    delivery_in_flight_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Soft lease: while now() < this timestamp, an in-progress "
            "deliver_buffered_messages task is mid-POST for this row. "
            "Concurrent QStash retries skip rows whose lease is still "
            "live so we don't fire duplicate /v1/chat/completions calls "
            "at the container while the first turn is still running."
        ),
    )

    class Meta:
        db_table = "buffered_messages"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"BufferedMessage({self.channel}, {self.delivery_status}, tenant={self.tenant_id})"


class PendingMessage(models.Model):
    """Messages awaiting forwarding to a warm tenant container, serialized
    per (tenant, channel, channel_user_id).

    Distinct from ``BufferedMessage`` (which covers hibernation buffering):
    this queue exists to prevent the OpenClaw claude-cli backend from
    receiving overlapping turns on the same live session. Claude rejects
    a second concurrent turn with "Claude CLI live session is already
    handling a turn", which previously caused a silent fallback to
    MiniMax (or a hard error post-#427).

    Flow:
      1. LINE/Telegram webhook receives message → row inserted, status
         ``pending``, ``delivery_in_flight_until=NULL``.
      2. QStash ``drain_pending_messages_for_tenant`` task fires (almost
         immediately for the typical case).
      3. Drain task claims the oldest pending row for
         ``(tenant, channel, channel_user_id)`` via
         ``SELECT ... FOR UPDATE SKIP LOCKED``, takes a soft lease
         (``delivery_in_flight_until``), POSTs to the container, relays
         the response back to the user, marks row ``delivered``.
      4. If more pending rows exist for the same key, the task
         re-schedules itself; otherwise it exits.

    The lease pattern mirrors PR #430's approach for ``BufferedMessage``:
    a concurrent QStash retry / cron tick observes the live lease and
    skips the row instead of firing a duplicate ``/v1/chat/completions``
    while the first turn is mid-flight.

    The ``channel_user_id`` is the per-channel user identifier
    (``line_user_id`` for LINE, the Telegram ``chat_id`` as a string for
    Telegram). Tenants are typically 1:1 with users today, but two
    different LINE users on the same tenant must not block each other —
    the queue is keyed by ``(tenant, channel, channel_user_id)``.
    """

    class Channel(models.TextChoices):
        TELEGRAM = "telegram"
        LINE = "line"

    class Status(models.TextChoices):
        PENDING = "pending"
        DELIVERED = "delivered"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="pending_messages",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    channel_user_id = models.CharField(
        max_length=128,
        help_text=(
            "Per-channel user identifier (line_user_id for LINE, Telegram "
            "chat_id stringified for Telegram). Used to scope the queue "
            "so two distinct users on the same tenant don't block each "
            "other."
        ),
    )
    payload = models.JSONField(
        help_text=(
            "Channel-specific bundle with everything the drain task needs "
            "to forward the message and relay the reply: prepared "
            "message_text (with workspace + datetime markers already "
            "injected), user_param, user_timezone, reply_token, is_voice, "
            "etc."
        ),
    )
    user_text = models.TextField(
        blank=True,
        default="",
        help_text="Raw user-facing excerpt for logging (truncated).",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="Terminal state: 'delivered' on success, 'failed' after attempts cap reached.",
    )
    delivery_attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of times the drain task has tried and failed to deliver this message.",
    )
    delivery_in_flight_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Soft lease: while now() < this timestamp, an in-progress "
            "drain task is mid-POST for this row. Concurrent QStash "
            "retries skip rows whose lease is still live so we don't "
            "fire duplicate /v1/chat/completions calls at the container "
            "while the first turn is still running."
        ),
    )

    class Meta:
        db_table = "pending_messages"
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["tenant", "channel", "channel_user_id", "delivery_status", "created_at"],
                name="pmsg_drain_lookup_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"PendingMessage({self.channel}, {self.delivery_status}, tenant={self.tenant_id})"
