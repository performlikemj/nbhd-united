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


class ProcessedInboundEvent(models.Model):
    """Idempotency ledger for inbound provider events.

    LINE and Telegram both deliver webhooks *at least once*: LINE
    redelivers an event (same ``webhookEventId``, ``deliveryContext.
    isRedelivery=true``) whenever our endpoint is slow to 200 or on its
    internal retry; Telegram replays any ``update_id`` that wasn't
    acknowledged before the poller process restarted (the offset is
    in-memory only). Without a dedupe gate every redelivery spawns a
    fresh ``PendingMessage`` → a fresh drain → a duplicate assistant
    reply. The ``PendingMessage`` SKIP-LOCKED lease only protects a
    single row; two rows born from the same logical event are processed
    independently.

    Each channel claims its event here *before* any side effect, keyed
    by the provider's stable id (``line:<webhookEventId>`` /
    ``tg:<update_id>``). The unique constraint makes the claim race-safe
    across the LINE webhook's per-event daemon threads and concurrent
    redelivery. Rows are pruned probabilistically (see
    ``apps.router.inbound_dedup``) so the table can't grow unbounded
    without standing up new cron infrastructure.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_key = models.CharField(
        max_length=160,
        unique=True,
        help_text=(
            "Provider-namespaced stable event id: 'line:<webhookEventId>' "
            "or 'tg:<update_id>'. The unique constraint is the dedupe."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "processed inbound event"
        verbose_name_plural = "processed inbound events"

    def __str__(self) -> str:
        return f"ProcessedInboundEvent({self.event_key})"


class ProactiveOutbound(models.Model):
    """Records every proactive ``nbhd_send_to_user`` push so the next
    inbound from that user can surface it as conversation context.

    The conversation-state-loss problem this solves: cron-fired sessions
    and main-chat sessions are separate OpenClaw sessions. When a cron
    job sends a 3-bullet check-in via ``nbhd_send_to_user`` and the
    container then hibernates, the user's reply arrives on a fresh main-
    chat session with no record of what was asked — so the agent
    conflates a multi-point reply because it can't anchor each paragraph
    to the original question. The legacy mitigation
    (``_phase2_sync_block``) prompts the agent to create a hidden
    ``_sync:`` cron that injects a summary into the main session, but
    that path is LLM-mediated and unreliably fired.

    This model is the deterministic replacement: every successful push
    from ``CronDeliveryView`` writes a row here, and the inbound
    envelope composer (LINE webhook, Telegram webhook, Telegram poller)
    pulls unconsumed rows from the last 24h and prepends them as a
    ``[earlier-from-you ...]`` block before the user's text.

    ``parsed_items`` stores markdown bullets / numbered items extracted
    from ``message_text`` so the envelope can render a structured
    "previous question 1/2/3" block; the agent's chat marker then knows
    to map reply paragraphs by index when the counts line up.

    Distinct from ``LineOutboundMessage``: that table keys by LINE
    ``message_id`` for quote-reply lookups and is LINE-specific. This
    table is channel-agnostic and keys by ``(tenant, channel,
    channel_user_id, created_at)`` for thread-continuity lookups.
    """

    class Channel(models.TextChoices):
        TELEGRAM = "telegram"
        LINE = "line"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="proactive_outbounds",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    channel_user_id = models.CharField(
        max_length=128,
        help_text=("Per-channel user identifier (line_user_id for LINE, Telegram chat_id stringified for Telegram)."),
    )
    message_text = models.TextField(
        help_text="Full proactive message body as sent (post-PII-rehydration).",
    )
    job_name = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=(
            "Cron job name if the send originated from a cron session "
            "(read from X-NBHD-Job-Name header). Empty for main-session "
            "or ad-hoc proactive sends."
        ),
    )
    parsed_items = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Markdown bullets / numbered items extracted from "
            "``message_text`` as list[str]. Empty list when the body has "
            "no list structure. Used by the envelope to render structured "
            "anchors so a multi-paragraph reply can be mapped by index."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set when this row was first surfaced into an inbound "
            "envelope. Kept for audit even after consumption — the "
            "envelope still surfaces consumed rows within a short "
            "follow-up window so back-to-back replies still see context."
        ),
    )

    class Meta:
        db_table = "proactive_outbounds"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["tenant", "channel", "channel_user_id", "-created_at"],
                name="proactive_outb_thread_idx",
            ),
            models.Index(
                fields=["created_at"],
                name="proactive_outb_created_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"ProactiveOutbound({self.channel}, tenant={self.tenant_id}, job={self.job_name or '-'})"


class LineOutboundMessage(models.Model):
    """Records LINE messages we've sent so quote-reply lookups can resolve
    a ``quotedMessageId`` on inbound webhook events back to the original
    text we sent.

    LINE's webhook delivers only the *id* of the quoted message on a
    quote-reply (``TextMessageContent.quotedMessageId``), unlike Telegram
    which inlines ``reply_to_message.text``. To prepend
    ``[Replying to: "..."]`` context to the user's message before
    forwarding to the container, we have to look up what we said.

    Rows are pruned probabilistically on insert so the table can't grow
    unbounded (see ``apps.router.line_webhook._record_line_outbound``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="line_outbound_messages",
    )
    line_user_id = models.CharField(max_length=128)
    line_message_id = models.CharField(
        max_length=64,
        unique=True,
        help_text="ID returned by LINE's push/reply API in sentMessages[].id.",
    )
    text_excerpt = models.TextField(
        blank=True,
        default="",
        help_text="First ~500 chars of the message we sent — used as the quoted excerpt.",
    )
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "line_outbound_messages"
        ordering = ["-sent_at"]
        indexes = [
            models.Index(
                fields=["tenant", "line_user_id", "-sent_at"],
                name="line_outb_tenant_user_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"LineOutboundMessage({self.line_message_id})"
