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
        IOS = "ios"

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
        # iOS-only users have no Telegram/LINE chat id — the message is delivered
        # as an APNs push + a ?since= feed row, so the iOS app is its own channel.
        APP = "app"

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
    notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Set when an APNs push was first claimed for this proactive / cron "
            "send so the iOS app pings the user (the counterpart to "
            "``AppChatMessage.notified_at``). The atomic isnull→now claim makes "
            "the push idempotent. Distinct from ``consumed_at`` (inbound-envelope "
            "thread continuity), which is deliberately re-surfaced."
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
            # Ascending walk for the ?since= cross-channel history feed.
            models.Index(fields=["tenant", "created_at"], name="proactive_tenant_created_idx"),
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


class LineQuotaState(models.Model):
    """Fleet-wide LINE Messaging API Push-message quota state.

    Singleton row (``pk=1``) updated by the daily poll task in
    ``apps.router.line_quota`` and by the 429 tripwire in the Push send
    paths. Frontend channel-selector + state-transition handlers read
    this row to decide whether LINE is currently selectable and whether
    to fire user-facing emails (90% pre-warn, exhaustion fan-out,
    recovery fan-out).

    Why a singleton, not per-tenant: every tenant shares the *same*
    LINE Messaging API channel (one bot, one access token, one monthly
    Push allowance). When the cap is hit, no tenant can receive Push.
    Per-tenant rows would invert the model — quota is a property of the
    bot, not the tenant.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)

    line_quota_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=("Monthly Push-message cap from /v2/bot/message/quota. Null before the first successful poll."),
    )
    line_quota_used = models.PositiveIntegerField(
        default=0,
        help_text="Push messages used this month from /v2/bot/message/quota/consumption.",
    )
    line_quota_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the last successful poll.",
    )
    line_quota_pre_warn_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the 90% pre-warn email was last sent. Cleared when usage "
            "drops back below the threshold (next month) so the next event "
            "fires fresh."
        ),
    )
    line_quota_exhausted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the system entered the exhausted state. Set by the 429 "
            "tripwire or by the daily poll seeing used >= limit. Cleared "
            "by the poll when usage drops back below the cap. Presence "
            "drives the frontend LINE-disabled gate."
        ),
    )
    line_quota_exhausted_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the exhaustion fan-out (user emails + channel flips) "
            "last completed. Compared against ``line_quota_exhausted_at`` "
            "for idempotency: if the handler is dispatched twice for the "
            "same exhaustion event (tripwire + poll race), the second "
            "dispatch bails. Cleared on recovery so the next event fires "
            "fresh."
        ),
    )
    line_quota_recovered_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the 'LINE is back — want to switch?' fan-out emails "
            "last completed. Cleared the next time we enter exhausted."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "line_quota_state"

    def save(self, *args, **kwargs):
        # Enforce singleton — pk=1 always.
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "LineQuotaState":
        """Return the singleton row, creating it on first access."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_exhausted(self) -> bool:
        return self.line_quota_exhausted_at is not None

    @property
    def usage_ratio(self) -> float | None:
        """Fraction of the monthly cap consumed (0.0–1.0+), or None if
        the cap is unknown."""
        if not self.line_quota_limit:
            return None
        return self.line_quota_used / self.line_quota_limit

    def __str__(self) -> str:
        if not self.line_quota_limit:
            return "LineQuotaState(uninitialized)"
        return (
            f"LineQuotaState({self.line_quota_used}/{self.line_quota_limit}"
            f"{', exhausted' if self.is_exhausted else ''})"
        )


class ChatThread(models.Model):
    """A conversation thread the user owns, independent of channel.

    Threads decouple "a conversation" from "a channel/device". The shared
    ``is_main`` thread is the default conversation that every channel
    (Telegram, LINE, iOS) resumes — so a conversation continues seamlessly
    across devices. Rich clients (iOS/web) may create additional named
    threads for topic separation.

    The OpenClaw ``user`` param for a thread is ``thread:<id>`` (see
    ``apps/router/chat_views.py``), so each thread hashes to its own
    OpenClaw session/sessionKey while ``USER.md``/memory stays shared
    tenant-wide (assembled channel-blind in
    ``apps/orchestrator/workspace_envelope.py``). That is what lets a new
    surface "know who you are" without copying any state — identity is the
    tenant's, the thread is just the transcript.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="chat_threads",
    )
    user = models.ForeignKey(
        "tenants.User",
        on_delete=models.CASCADE,
        related_name="chat_threads",
    )
    title = models.CharField(max_length=120, blank=True, default="")
    is_main = models.BooleanField(
        default=False,
        help_text="The shared default thread resumed by every channel. One per tenant.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "chat_threads"
        ordering = ["-last_active_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant"],
                condition=models.Q(is_main=True),
                name="uniq_main_thread_per_tenant",
            ),
        ]

    def __str__(self) -> str:
        label = "main" if self.is_main else (self.title or str(self.id))
        return f"ChatThread({label}, tenant={self.tenant_id})"


class AppChatMessage(models.Model):
    """A single rich-client (iOS/web) chat turn: the user's message and the
    assistant's reply, persisted so the client can POLL for the reply.

    Telegram/LINE relay replies via their push APIs; rich clients have no
    push transport, so the drain (``_drain_ios_batch`` in
    ``apps/router/pending_queue.py``) writes the assistant reply here keyed
    by the client-supplied ``client_msg_id`` and the client polls
    ``GET /api/v1/chat/messages/<client_msg_id>/`` until ``status`` flips to
    ``ready`` (or ``error``).

    ``client_msg_id`` is the idempotency key: a retried POST with the same
    id returns the existing turn instead of enqueuing a duplicate.
    """

    class Status(models.TextChoices):
        PENDING = "pending"
        READY = "ready"
        ERROR = "error"

    class Source(models.TextChoices):
        # Reply produced by the tenant's OpenClaw runtime (the normal
        # POST → enqueue → drain → poll flow).
        TENANT = "tenant"
        # Turn ran entirely on the client's local model (iOS private mode)
        # and was recorded here AFTER the fact so thread history and the
        # USER.md conversation digest still see it. Never enqueued.
        ON_DEVICE = "on_device"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="app_chat_messages",
    )
    user = models.ForeignKey(
        "tenants.User",
        on_delete=models.CASCADE,
        related_name="app_chat_messages",
    )
    thread = models.ForeignKey(
        ChatThread,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    client_msg_id = models.CharField(
        max_length=64,
        help_text="Client-supplied stable id. Idempotency key + poll key.",
    )
    user_text = models.TextField()
    reply_text = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    source = models.CharField(
        max_length=16,
        choices=Source.choices,
        default=Source.TENANT,
        help_text="Where the assistant reply was produced: the tenant runtime, "
        "or the client's on-device model (recorded post-hoc).",
    )
    error = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Machine-readable error reason when status=error (e.g. 'budget_exhausted').",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    replied_at = models.DateTimeField(null=True, blank=True)
    waking_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set while the tenant container is waking from hibernation "
        "so polling clients can show honest 'assistant is waking up' copy "
        "instead of an indefinite typing indicator. Meaningless once "
        "status leaves 'pending'.",
    )
    # Live agent-activity narration while status=pending. The container's
    # tool-call hooks report progress to ProgressEventView, which updates these
    # in place; polling clients render them (in-app "searching your journal…"
    # instead of dumb dots; the iOS-27 Siri Live Activity maps `phase` to
    # progress.localizedDescription). Meaningless once status leaves 'pending'.
    phase = models.CharField(
        max_length=24,
        blank=True,
        default="",
        help_text="Coarse activity phase: '', 'waking', 'thinking', 'tool', 'composing'.",
    )
    phase_detail = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Human-readable detail for the current phase, e.g. 'searching your journal'.",
    )
    notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when an APNs 'reply ready' / 'error' push was first claimed "
        "for this turn. The atomic isnull→now claim makes the push idempotent: a "
        "re-drained batch (QStash retry, re-leased batch) won't push twice.",
    )

    class Meta:
        db_table = "app_chat_messages"
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "client_msg_id"],
                name="uniq_app_chat_client_msg",
            ),
        ]
        indexes = [
            models.Index(fields=["thread", "created_at"], name="appchat_thread_idx"),
            # Ascending cross-channel walk for the ?since= history feed (both the
            # user and assistant message rows of a turn key off created_at, so a
            # single (tenant, created_at) index covers the keyset pagination).
            models.Index(fields=["tenant", "created_at"], name="appchat_tenant_created_idx"),
        ]

    def __str__(self) -> str:
        return f"AppChatMessage({self.status}, thread={self.thread_id})"


class ConversationTurn(models.Model):
    """One captured chat turn (user message + assistant reply) for a Telegram
    or LINE conversation, persisted control-plane-side so ISOLATED cron
    sessions and proactive comms can see what was actually discussed today and
    on recent days.

    Why this exists: Telegram/LINE conversations are relayed to the per-tenant
    OpenClaw container and never otherwise persisted in Postgres —
    ``PendingMessage`` is an ephemeral queue row that's gone after delivery.
    Cron sessions (Evening Check-in, Heartbeat, Morning Briefing, …) run in a
    SEPARATE OpenClaw session that cannot read the main chat transcript, and the
    only "today" surfaces they CAN read (daily-note ``Document`` rows,
    ``nbhd_journal_context``) are empty unless the agent voluntarily journaled —
    which it often doesn't. The result was crons reporting "quiet day on the
    chat front" on days with substantive conversations (e.g. a job interview).

    This table is the deterministic record the USER.md "Conversation so far"
    digest renders from (see ``apps.router.conversation_capture``). USER.md is
    auto-loaded by OpenClaw on EVERY agent turn — cron, chat, or proactive — so
    a section sourced from this table reaches even the isolated cheap-model
    crons that never call a context tool.

    iOS / web app chat is NOT stored here — it is already durably persisted in
    :class:`AppChatMessage`; the digest reads that table for the iOS slice to
    avoid double-storage.

    Rows are pruned probabilistically on insert (35-day window) so the table is
    self-bounding without a janitor cron — same pattern as
    :class:`ProcessedInboundEvent` / :class:`LineOutboundMessage`. Storage is
    raw (real content) by design decision — consistent with the at-rest posture
    of ``AppChatMessage`` / journal ``Document`` and bounded shorter than either.
    """

    class Channel(models.TextChoices):
        TELEGRAM = "telegram"
        LINE = "line"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="conversation_turns",
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    channel_user_id = models.CharField(
        max_length=128,
        help_text="Per-channel user identifier (telegram chat_id stringified, line_user_id for LINE).",
    )
    local_date = models.DateField(
        db_index=True,
        help_text=(
            "Tenant-LOCAL calendar date of the turn (via apps.common.tenant_tz.tenant_today). "
            "The digest groups by this so 'today' matches the user's day, not the server's UTC day."
        ),
    )
    user_text = models.TextField(
        blank=True,
        default="",
        help_text="The user's message(s) for this turn, joined for a coalesced batch. Raw/real content.",
    )
    reply_text = models.TextField(
        blank=True,
        default="",
        help_text="The assistant's reply, PII-rehydrated and marker-stripped. May be empty on a failed turn.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "conversation_turns"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tenant", "local_date"], name="conv_turn_tenant_date_idx"),
            models.Index(
                fields=["tenant", "channel", "channel_user_id", "-created_at"],
                name="conv_turn_thread_idx",
            ),
            models.Index(fields=["created_at"], name="conv_turn_created_idx"),
            # Ascending walk for the ?since= cross-channel history feed.
            models.Index(fields=["tenant", "created_at"], name="conv_turn_tenant_created_idx"),
        ]

    def __str__(self) -> str:
        return f"ConversationTurn({self.channel}, {self.local_date}, tenant={self.tenant_id})"


class DeviceToken(models.Model):
    """An APNs device token for a user's iOS install.

    Lets the control plane push "your answer is ready" when a fire-and-forget
    (Siri-escalated) or backgrounded turn completes — the cross-cutting APNs gap
    in ``HER_SIRI_ARCHITECTURE.md``. Without it, a fire-and-forget reply only
    surfaces on the next app foreground (``SiriCaptureSync``).

    A token belongs to one device install; ``(user, token)`` is unique and
    upserted on registration. Tokens APNs reports as Unregistered (HTTP 410) are
    pruned by the sender so the table self-heals on reinstall / token rotation.
    """

    class Environment(models.TextChoices):
        SANDBOX = "sandbox"
        PRODUCTION = "production"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        "tenants.Tenant",
        on_delete=models.CASCADE,
        related_name="device_tokens",
    )
    user = models.ForeignKey(
        "tenants.User",
        on_delete=models.CASCADE,
        related_name="device_tokens",
    )
    # APNs device tokens are 32-byte (64 hex char) today, but Apple has signalled
    # they may grow; bound generously rather than pin to 64.
    token = models.CharField(max_length=200)
    environment = models.CharField(
        max_length=16,
        choices=Environment.choices,
        default=Environment.PRODUCTION,
        help_text="Which APNs host the token is valid for (sandbox builds vs App Store / TestFlight).",
    )
    bundle_id = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "device_tokens"
        ordering = ["-last_seen_at"]
        constraints = [
            # A device token identifies one physical install, which is owned by
            # exactly one (current) user. Global uniqueness on the token makes
            # registration a single atomic upsert that re-points the token to the
            # registering user — no cross-user/-tenant delete (which would let a
            # token-holder evict another tenant's row), no delete+create race, and
            # it guarantees a push for user A never reaches a device now used by
            # user B (the prior owner's row is overwritten, not duplicated).
            models.UniqueConstraint(fields=["token"], name="uniq_device_token"),
        ]
        indexes = [
            models.Index(fields=["tenant"], name="device_token_tenant_idx"),
        ]

    def __str__(self) -> str:
        return f"DeviceToken({self.environment}, user={self.user_id})"
