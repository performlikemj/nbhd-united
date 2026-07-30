"""Tenant models — core of the control plane."""

import uuid
from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.orchestrator.tool_policy import OPENCLAW_CURRENT_VERSION  # noqa: I001

from .agenda_models import AgendaEngagement  # noqa: F401
from .apple_models import AppleAuthTransaction, AppleRevocationOutbox, ExternalIdentity  # noqa: F401
from .line_models import LineLinkToken  # noqa: F401

# Import so Django discovers the models for migrations
from .oauth_models import OAuthAuthorizationCode  # noqa: F401
from .pat_models import PersonalAccessToken  # noqa: F401
from .promo_models import PromoCampaign, PromoRedemption  # noqa: F401
from .telegram_models import TelegramLinkToken  # noqa: F401


class User(AbstractUser):
    """Custom user model with Telegram and LINE binding."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    telegram_chat_id = models.BigIntegerField(unique=True, null=True, blank=True)
    telegram_user_id = models.BigIntegerField(null=True, blank=True)
    telegram_username = models.CharField(max_length=255, blank=True, default="")
    display_name = models.CharField(max_length=255, default="Friend")
    language = models.CharField(max_length=10, default="en")
    timezone = models.CharField(
        max_length=63,
        default="UTC",
        help_text="IANA timezone string, e.g. 'America/New_York'",
    )
    preferences = models.JSONField(default=dict, blank=True)

    # LINE channel fields
    line_user_id = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text="LINE user ID (per-bot, not global)",
    )
    line_display_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Display name from LINE profile",
    )
    preferred_channel = models.CharField(
        max_length=16,
        choices=[("telegram", "Telegram"), ("line", "LINE")],
        default="telegram",
        help_text="Primary channel for proactive messages (cron, alerts).",
    )

    # Location (for weather and local recommendations)
    location_city = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="User's city name, e.g. 'Osaka', 'Brooklyn'",
    )
    location_lat = models.FloatField(
        null=True,
        blank=True,
        help_text="Latitude for weather/location services",
    )
    location_lon = models.FloatField(
        null=True,
        blank=True,
        help_text="Longitude for weather/location services",
    )

    # Force-logout marker — JWTs carry `pw_iat` and are rejected when
    # ``pw_iat < password_last_changed_at``. Bumped automatically by
    # ``set_password`` (override below). Null means "never rotated" and
    # the JWT validator treats any token as valid for legacy users.
    password_last_changed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Stamp updated whenever ``set_password`` is called. Used by "
            "the JWT validator to invalidate access + refresh tokens "
            "issued before a password rotation, without needing the "
            "simplejwt token_blacklist app."
        ),
    )

    # Marketing-email opt-out. Set by the one-click unsubscribe view
    # (apps/tenants/unsubscribe_views.py). Every promo/campaign send
    # excludes opted-out users. This governs bulk marketing sends only —
    # transactional mail (password reset, provisioning status) ignores it.
    email_opt_out = models.BooleanField(
        default=False,
        help_text="True if the user unsubscribed from marketing/campaign emails.",
    )
    email_opt_out_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user unsubscribed (null if still subscribed).",
    )

    # Server read-cursor for the in-app chat, stamped by POST /api/v1/chat/read/.
    # Drives the server-authoritative APNs unread badge: an assistant reply or a
    # proactive/cron push after this instant is "unread". Null = never read (the
    # badge then counts only a recent window, not the whole history).
    chat_last_read_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user last marked the in-app chat read (POST /chat/read/). "
        "The APNs unread badge counts assistant replies / proactive pushes after this.",
    )

    class Meta:
        db_table = "users"

    def set_password(self, raw_password):
        """Override to bump ``password_last_changed_at`` on every change.

        Called by createsuperuser, password reset flow, admin password
        change, and ``rotate_all_passwords``. Bumping the stamp here
        catches every path. See ``apps/tenants/authentication.py`` for
        the JWT-side enforcement.
        """
        super().set_password(raw_password)
        from django.utils import timezone

        self.password_last_changed_at = timezone.now()

    def set_unusable_password(self):
        """Override to bump the stamp the same way ``set_password`` does.

        ``rotate_all_passwords`` calls this to force every user through
        the reset flow; without bumping the stamp here, existing JWTs
        would survive the rotation."""
        super().set_unusable_password()
        from django.utils import timezone

        self.password_last_changed_at = timezone.now()

    def __str__(self) -> str:
        return self.display_name or self.username


class Tenant(models.Model):
    """
    A tenant = one subscriber = one OpenClaw instance.
    This is the central record tying user, subscription, and container together.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROVISIONING = "provisioning", "Provisioning"
        ACTIVE = "active", "Active"
        SUSPENDED = "suspended", "Suspended"
        DEPROVISIONING = "deprovisioning", "Deprovisioning"
        DELETED = "deleted", "Deleted"

    class ModelTier(models.TextChoices):
        STARTER = "starter", "Standard"

    class TourGuideMode(models.TextChoices):
        CARDS = "cards", "Cards"
        LINKS = "links", "Links"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="tenant")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    model_tier = models.CharField(max_length=20, choices=ModelTier.choices, default=ModelTier.STARTER)

    # OpenClaw container
    container_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Azure Container App name (e.g. oc-usr-abc123)",
    )
    container_fqdn = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Internal FQDN of the container",
    )
    container_image_tag = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Current OpenClaw container image tag (git SHA)",
    )
    openclaw_version = models.CharField(
        max_length=20,
        default=OPENCLAW_CURRENT_VERSION,
        help_text="OpenClaw runtime version pinned to this tenant's config",
    )

    # Azure Key Vault
    key_vault_prefix = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Key Vault secret prefix (e.g. tenants-<uuid>)",
    )

    # Per-tenant OpenRouter sub-key (PR #1.6). Tenant has a dedicated
    # OpenRouter sub-key with a server-side spending limit so OR enforces
    # the per-tenant cap; the key string is stored in Key Vault at
    # ``openrouter_key_secret_name`` and injected into the container as
    # ``OPENROUTER_API_KEY``. ``openrouter_key_hash`` is OR's stable
    # identifier for the key (returned at create time) and is what
    # ``DELETE /api/v1/keys/{hash}`` takes on deprovision.
    openrouter_key_secret_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Key Vault secret name holding this tenant's OpenRouter sub-key (PR #1.6)",
    )
    openrouter_key_hash = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="OpenRouter-side hash identifying this tenant's sub-key (for management DELETE)",
    )
    managed_identity_id = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="Azure User-Assigned Managed Identity resource ID",
    )

    # Stripe (dj-stripe handles subscription objects; this is a quick-lookup cache)
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default="")

    # Scheduled deletion
    pending_deletion = models.BooleanField(
        default=False,
        help_text="Account is queued for deletion. Kept alive until deletion_scheduled_at.",
    )
    deletion_scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the account will be hard-deleted (end of paid period, or immediate if no subscription).",
    )

    # Free trial
    trial_started_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When free trial began",
    )
    trial_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When free trial expires",
    )
    is_trial = models.BooleanField(
        default=False,
        help_text="Currently on free trial",
    )

    # Usage tracking
    messages_today = models.IntegerField(default=0)
    messages_this_month = models.IntegerField(default=0)
    tokens_this_month = models.IntegerField(default=0)
    estimated_cost_this_month = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    monthly_token_budget = models.IntegerField(
        default=0,
        help_text="Per-user monthly token budget (0 = use tier default)",
    )
    monthly_cost_budget = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Monthly API cost cap in USD. 0 = use tier default.",
    )
    is_budget_exempt = models.BooleanField(
        default=False,
        help_text="Exempt from personal and global budget enforcement. Usage still tracked.",
    )
    is_synthetic = models.BooleanField(
        default=False,
        help_text=(
            "Eval-system synthetic tenant (see docs/evals-directive.md). Behaves EXACTLY "
            "like a real tenant operationally (provisions, drains, crons, hibernates/wakes) "
            "— that is the point — but is EXCLUDED from business-facing aggregates "
            "(revenue, donation, campaign audiences, usage/true-cost, growth counts) so it "
            "never distorts a number. Never set for a real subscriber."
        ),
    )
    is_eval_sink = models.BooleanField(
        default=False,
        help_text=(
            "Dedicated eval delivery/memory sink. When enabled, outbound messages "
            "are recorded as eval evidence but are not sent to user transports or "
            "surfaced in user/model history. Independent of is_synthetic: synthetic "
            "demo accounts keep normal assistant behavior unless explicitly enabled."
        ),
    )
    purchased_credit = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=0,
        help_text=(
            "Prepaid credit balance in USD. EXTENDS the monthly included allowance once it's "
            "spent; persists across months and does not expire. Granted by Stripe top-ups and "
            "drawn down for usage beyond the included cap. Source of truth for grants/refunds is "
            "apps.billing.models.CreditLedger; this is the denormalized hot-read balance."
        ),
    )

    # Quota-email idempotency markers (PR #1.8). Each is set when the
    # corresponding email goes out, and cleared by the monthly counter
    # reset. The reconcile cron checks these before sending to avoid
    # duplicate notifications when usage hovers around the threshold.
    cost_warn_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the 90%-of-cap warning email was last sent (cleared monthly).",
    )
    cost_exhausted_email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the cap-exhausted email was last sent (cleared monthly).",
    )

    # Dunning idempotency marker. Holds the Stripe invoice id of the most
    # recent failed subscription invoice we emailed the user about, so the
    # "payment failed, we'll retry" notice fires ONCE per invoice rather than
    # on every automatic-retry ``invoice.payment_failed`` event. Cleared when
    # the invoice is ultimately paid (reactivation) so a future decline on a
    # new invoice re-arms the notice.
    dunning_notice_invoice_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Stripe invoice id of the last failed invoice we sent a retry-notice email for.",
    )

    # Per-tenant internal API key. When non-empty, internal_auth.py validates
    # the X-NBHD-Internal-Key header against this value instead of the legacy
    # global settings.NBHD_INTERNAL_API_KEY. Restored 2026-05-12 (Phase 1)
    # to close the cross-tenant Django pivot — a prompt-injected tenant
    # holding the global key from process.env could otherwise call internal
    # endpoints with any tenant_id in headers and read that tenant's data.
    #
    # The previous "internal-only network" safety argument (used to justify
    # the 2026-02-22 collapse, migration 0018) was wrong for the LLM threat
    # model: the attacker is INSIDE the container, not outside. Dual-
    # validation (per-tenant if set, global as fallback) makes the migration
    # safe without a flag-day — see apps/integrations/internal_auth.py.
    internal_api_key = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text=(
            "Per-tenant secret used by the tenant's OpenClaw container to "
            "authenticate to Django internal endpoints. Stored raw; Container "
            "Apps secret reference (kv-nbhd-prod/secrets/tenant-<uuid>-internal-key) "
            "is the runtime source of truth for the container side."
        ),
    )

    # Onboarding
    onboarding_complete = models.BooleanField(
        default=False,
        help_text="Whether messaging onboarding has been completed",
    )
    onboarding_step = models.IntegerField(
        default=0,
        help_text="Current onboarding question index (0 = not started)",
    )

    # Heartbeat window ("On the Clock")
    heartbeat_enabled = models.BooleanField(
        default=True,
        help_text="Whether the hourly heartbeat check-in is active",
    )
    heartbeat_start_hour = models.IntegerField(
        default=8,
        validators=[MinValueValidator(0), MaxValueValidator(23)],
        help_text="Start hour of the heartbeat window (0-23, in user's timezone)",
    )
    heartbeat_window_hours = models.IntegerField(
        default=6,
        validators=[MinValueValidator(1), MaxValueValidator(6)],
        help_text="Duration of the heartbeat window in hours (1-6)",
    )

    # Experimental: OpenClaw built-in heartbeat (vs our cron-based heartbeat).
    # When True:
    #   - Django emits agents.defaults.heartbeat with every:"1h" + activeHours
    #     so OpenClaw runs the gateway-managed periodic turn that delivers
    #     inferred commitments. See docs/concepts/commitments and
    #     gateway/heartbeat in the OpenClaw docs.
    #   - _build_heartbeat_cron() returns None so the cron-based heartbeat
    #     doesn't fire alongside (the two would overlap during the morning
    #     window and we want a single mechanism while we observe canary).
    #   - The commitments block is emitted (commitments only deliver via
    #     OpenClaw's built-in heartbeat; enabling them without the heartbeat
    #     is wasted background extraction).
    # Off by default fleet-wide; flip on canary first, observe, then decide
    # whether to make the built-in heartbeat the default and retire the
    # cron-based one.
    experimental_built_in_heartbeat = models.BooleanField(
        default=False,
        help_text=(
            "Experimental: use OpenClaw's built-in heartbeat (which "
            "delivers inferred commitments) instead of our cron-based "
            "heartbeat. Canary-gated rollout."
        ),
    )

    # Experimental: OpenClaw's built-in memory engine (memory-core).
    # When True:
    #   - agents.defaults.memorySearch.enabled becomes True with the
    #     SQLite index pointed at the container-local ``index-cache``
    #     EmptyDir mount (see azure_client.py). Markdown files (MEMORY.md,
    #     memory/*.md) stay on the workspace share; only the SQLite
    #     cache lives ephemeral and rebuilds on cold start.
    #   - memory_search / memory_get tools are usable by the agent (the
    #     tool policy for the canary OC version already allows them).
    # Off by default fleet-wide; flip on canary first, measure cold-start
    # index rebuild cost on Azure SMB, then decide whether the agentic
    # memory benefit warrants fleet rollout. See PR #525 for the original
    # disable, and ``project_memory_search_disabled.md`` for context.
    experimental_memory_core_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Experimental: enable OpenClaw memory-core with the SQLite "
            "index on container-local ephemeral storage (markdown stays "
            "on the share). Canary-gated rollout."
        ),
    )

    # Experimental: OpenClaw active-memory plugin.
    # When True:
    #   - plugins.entries["active-memory"] is emitted with a blocking
    #     pre-reply recall sub-agent that injects relevant memory before
    #     the main agent composes its reply. Adds ~500ms-2s of latency
    #     to the reply path but catches the "agent forgot to search
    #     memory" failure mode this morning's diagnosis surfaced.
    # Requires ``experimental_memory_core_enabled`` to be True — the
    # active-memory plugin calls ``memory_search`` under the hood, which
    # has no backend without memory-core. config_generator enforces the
    # precondition and skips the plugin entry (with a logged warning) if
    # active-memory is on but memory-core is off.
    experimental_active_memory_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Experimental: enable the OpenClaw active-memory plugin "
            "(blocking pre-reply recall). Requires "
            "experimental_memory_core_enabled. Canary-gated rollout."
        ),
    )

    # Experimental: OpenClaw dreaming (memory consolidation).
    # When True:
    #   - plugins.entries["memory-core"].config.dreaming.enabled is set,
    #     activating the Light → Deep → REM phased consolidation that
    #     promotes high-signal short-term entries into MEMORY.md and
    #     writes a Dream Diary into DREAMS.md for review.
    # Requires ``experimental_memory_core_enabled`` to be True — dreaming
    # IS the consolidation layer of memory-core; toggling it without the
    # engine is meaningless.
    experimental_dreaming_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Experimental: enable OpenClaw memory-core dreaming for "
            "background consolidation. Requires "
            "experimental_memory_core_enabled. Canary-gated rollout."
        ),
    )

    # Experimental: typed journal lifecycle (Goal/Task models).
    # When True:
    #   - System prompt + memoryFlush prompt teach the agent to use
    #     nbhd_goal_* / nbhd_task_* tools (typed lifecycle) instead of
    #     writing goal/task content as Document(kind=goal|tasks) markdown.
    #   - Readers (envelope.py, agenda_envelope.py) prefer Goal/Task rows
    #     over legacy Document markdown.
    #   - memory_sync.py stops mirroring Document(kind in [goal, tasks])
    #     to the file share — those are now owned by typed tables.
    # Fleet-wide changes (new models, endpoints, tool registrations) ship
    # ungated; only the prompt + reader behavior is flag-gated so stale
    # tenants whose OpenClaw image lacks the new tools don't get prompted
    # to call them.
    experimental_typed_journal_lifecycle = models.BooleanField(
        default=False,
        help_text=(
            "Experimental: route goal/task lifecycle through typed Goal/Task "
            "tables and corresponding nbhd_goal_*/nbhd_task_* tools instead "
            "of free-form Document markdown. Canary-gated rollout."
        ),
    )

    experimental_reply_artifacts_to_journal = models.BooleanField(
        default=False,
        help_text=(
            "Experimental: move oversized GFM tables from persisted assistant "
            "history into Journal documents. Canary-gated rollout."
        ),
    )

    # Experimental: typed cron patterns.
    # When True:
    #   - The nbhd-automation-tools plugin is loaded, giving the agent
    #     typed cron-create tools (nbhd_cron_create_pure_reminder,
    #     nbhd_cron_create_quote_user_intent,
    #     nbhd_cron_create_domain_summary).
    #   - System-defined patterns (daily_briefing) are used for system
    #     crons; CronJob rows carry pattern + typed_payload so fire-time
    #     content is rendered against the pattern contract instead of
    #     freeform prose.
    #   - The nbhd-cron-enforcement plugin's hooks validate fire-time
    #     output against the pattern's contract.
    # Eventually the agent's raw ``cron`` tool will be added to the deny
    # list (apps/orchestrator/tool_policy.py) so the only path to create
    # a cron is the typed wrapper. That cutover is gated on this flag
    # being True fleet-wide.
    experimental_typed_crons = models.BooleanField(
        default=True,
        help_text=(
            "Typed cron patterns (pure_reminder, quote_user_intent, "
            "domain_summary, daily_briefing). Loads nbhd-automation-tools + "
            "nbhd-cron-enforcement plugins. Fleet-wide since 2026-07-13; "
            "default True so a new tenant gets the cron-create tools the base "
            "AGENTS.md capability list advertises — a flag-off tenant would "
            "read that it can set reminders and have no tool to do it."
        ),
    )

    # When the most recent ``nightly_extraction_task`` run completed for this
    # tenant. Used by the hourly per-tenant-tz dispatcher to skip tenants
    # whose extraction has already run today (in their local timezone) — so
    # a tenant whose local-21:xx hour ticks twice (DST boundary, manual
    # backfill) doesn't pay for two LLM calls or get two morning summaries.
    last_nightly_extraction_at = models.DateTimeField(null=True, blank=True)

    # Feature tips
    feature_tips_enabled = models.BooleanField(
        default=True,
        help_text="Whether the assistant proactively suggests platform features",
    )

    # Tour-guide capability — the AGENTS.md gate points enabled tenants at one
    # mode-specific guide doc. Cards are for the dev/TestFlight client that
    # renders nbhd-guide itinerary cards; links are safe for the App Store app.
    tour_guide_enabled = models.BooleanField(
        default=False,
        help_text="Enable server-side tour-guide instructions for this tenant",
    )
    tour_guide_manifest_ok = models.BooleanField(
        default=False,
        help_text=(
            "Runtime image's settings-tools manifest declares tourGuide config keys; "
            "set per-tenant after image verification, fleet-wide at fleet image rollout. "
            "Nothing reconciles this field."
        ),
    )
    places_search_manifest_ok = models.BooleanField(
        default=False,
        help_text=(
            "Runtime image's settings-tools manifest declares/registers nbhd_places_search; "
            "set per-tenant after image verification, nothing reconciles this."
        ),
    )
    journal_shaping_enabled = models.BooleanField(default=False)
    digest_thread_attribution_enabled = models.BooleanField(
        default=False,
        help_text="Label non-main iOS chat content in the shared conversation digest with its source thread",
    )
    situational_context_enabled = models.BooleanField(
        default=False,
        help_text="Capture and render structured current-situation signals for this tenant",
    )
    tour_guide_mode = models.CharField(
        choices=TourGuideMode.choices,
        default=TourGuideMode.LINKS,
        max_length=8,
        help_text=(
            "Cards: the dev/TestFlight client renders nbhd-guide itinerary cards. "
            "Links: the standard App Store client receives plain maps links only."
        ),
    )

    # Donation preferences
    donation_enabled = models.BooleanField(
        default=False,
        help_text="Opt-in to donate surplus subscription revenue",
    )
    donation_percentage = models.IntegerField(
        default=100,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Percentage of surplus to donate (0-100)",
    )

    # PII redaction entity mapping for rehydrating outgoing messages
    pii_entity_map = models.JSONField(
        default=dict,
        blank=True,
        help_text="Maps PII placeholders to original values, e.g. "
        '{"[PERSON_1]": "Sarah Chen", "[EMAIL_ADDRESS_1]": "sarah@example.com"}',
    )

    # Canonical-keyed strings the user has marked as "not PII for me". The
    # redactor short-circuits both Step 1 (existing-map regex) and the
    # post-NER mint loop for these keys. Empty = today's behavior.
    pii_denylist = models.JSONField(
        default=dict,
        blank=True,
        help_text="Canonical-keyed denylist of false-positive PII spans. "
        'Shape: {"goal": {}, "calendar": {"reason": "manual"}}.',
    )

    # Per-type monotonic high-water mark for PII placeholder numbering.
    # Shape: {"PERSON": 537, "EMAIL_ADDRESS": 12} — the HIGHEST suffix EVER
    # minted for each type, NOT the current count of live bindings.
    #
    # INVARIANT: these counters only ever increase. Deletion of a binding
    # (bulk-delete endpoint, junk sweep, single-entry delete) NEVER lowers
    # them. This field exists because mint numbering used to re-derive the
    # next suffix from ``max(pii_entity_map suffix per type) + 1`` alone.
    # Deleting bindings lowered that max, so a freed number was RECYCLED:
    # in prod ``[ACCOUNT_4]`` was a temperature range one morning and a
    # shipping-tracking number by afternoon, and any stale ``[TYPE_N]`` token
    # still sitting in agent-side workspace files then rehydrated to the WRONG
    # new value. Seeding every mint from ``max(map-derived, this counter)``
    # makes a freed number unreachable, so numbers stay stable for the life of
    # the tenant. Empty ({}) is the legacy pre-migration shape and mints fall
    # back to the map maxima — still correct, just without recycle protection
    # until the first post-migration mint records a high-water here.
    pii_type_counters = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-type monotonic high-water mark for PII placeholder numbering "
        '(highest suffix ever minted per type, e.g. {"PERSON": 537}). Never lowered '
        "on deletion — prevents freed placeholder numbers from being recycled.",
    )

    # Model preference
    task_model_preferences = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-task model overrides. Keys: heartbeat, morning_briefing, "
        "evening_checkin, week_review, background_tasks. "
        "Values: model IDs.",
    )
    # Cron job backup — snapshot of the last-known cron.list response.
    # Used to restore user-created jobs after container restarts. Retired
    # in Phase 2 of the Postgres-canonical cutover (replaced by the CronJob
    # table — see apps/cron/models.py).
    cron_jobs_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text='Last-known cron job list from gateway. Format: {"jobs": [...], "snapshot_at": "ISO8601"}',
    )

    # Per-tenant flag for the Postgres-canonical cron rollout. The dashboard,
    # runtime endpoints, and provisioning paths read/write the apps.cron.CronJob
    # table directly; the gateway's SQLite is a derived view rebuilt by
    # apps.orchestrator.cron_reconcile. Migration 0058 flipped every existing
    # tenant to True; new tenants default to True so they join the canonical
    # flow at creation time. The False branches remain for emergency rollback.
    postgres_cron_canonical = models.BooleanField(
        default=True,
        help_text=(
            "Cutover flag for the Postgres-canonical cron model. "
            "When True, the CronJob table is the source of truth and "
            "OpenClaw's SQLite is a derived view kept in sync by the "
            "regenerate_tenant_crons reconciler."
        ),
    )

    preferred_model = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="User's preferred primary model (overrides tier default when set)",
    )
    applied_model = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "Model the running container will serve at next restart, stamped "
            "when the regenerated openclaw.json is written to the file share. "
            "Diverges from preferred_model between the picker change and the "
            "next container warmup; the frontend uses the difference to render "
            "a 'Switching…' state instead of an immediate 'Active' badge."
        ),
    )
    applied_model_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Timestamp of the last successful applied_model write. Used by the "
            "frontend to detect 'still applying, taking longer than usual'."
        ),
    )

    # Action gating
    gate_all_actions = models.BooleanField(
        default=True,
        help_text="Master switch: require confirmation for all irreversible actions",
    )
    gate_acknowledged_risk = models.BooleanField(
        default=False,
        help_text="User has explicitly acknowledged the risk of disabling gates",
    )

    # Finance module
    finance_enabled = models.BooleanField(
        default=False,
        help_text="Enable budget tracking and debt payoff tools",
    )

    # Fuel module (workout tracking)
    fuel_enabled = models.BooleanField(
        default=False,
        help_text="Enable workout tracking and fitness logging",
    )
    fuel_version = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Monotonic counter bumped on every Workout / WorkoutPlan write. "
            "Embedded in schedule / calendar responses so the frontend can "
            "detect an out-of-band write by the assistant runtime and prompt "
            "the user to refresh before saving an open drawer."
        ),
    )

    # sautai integration (nutrition/meal-plan generation) — Phase 0. Gates the
    # nbhd-sautai-tools plugin (config_generator) and the imperative AGENTS.md
    # gate (personas.py) that tells the agent to call nbhd_generate_meal_plan.
    # Diet profile lives on sautai itself, not here — this only turns on the
    # tool. See docs/sautai-phase0-contract.md.
    sautai_enabled = models.BooleanField(
        default=False,
        help_text="Enable sautai meal-plan generation tools (nutrition sibling of Fuel)",
    )

    # Core module (mindfulness — AI-composed guided meditations)
    core_enabled = models.BooleanField(
        default=False,
        help_text="Enable the Core mindfulness pillar (on-demand guided meditations)",
    )

    # Constellation module — a pure client-side visualization of the tenant's
    # journal/data graph. No assistant plugin, no config bump, no restart:
    # toggling this only gates the tab in the client apps. Default False for
    # everyone, including existing tenants — deliberate, not a backfill gap.
    constellation_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Gate the Constellation visualization tab in the client apps. Pure "
            "client-side feature — involves no assistant plugin, config bump, "
            "or container restart."
        ),
    )

    # Encryption-at-rest Phase 2 (expand/contract) — chat content columns.
    # Two independent gates so write and read flip separately per tenant:
    #   encrypt_chat_writes — dual-write the sealed ``*_enc`` envelope alongside
    #     the plaintext column (PR-2). Dark default; nothing writes _enc until on.
    #   read_encrypted_chat — read back through the ``*_enc`` column when present,
    #     falling back to plaintext (PR-4). Kept OFF until a tenant's backfill has
    #     populated _enc for every row, so a read never misses ciphertext-only data.
    encrypt_chat_writes = models.BooleanField(
        default=False,
        help_text="Dual-write sealed *_enc envelopes for chat content (encryption-at-rest Phase 2).",
    )
    read_encrypted_chat = models.BooleanField(
        default=False,
        help_text="Read chat content back through the *_enc column when present (encryption-at-rest Phase 2).",
    )

    # Encryption-at-rest Phase 3 (expand/contract) — journal-group + fuel content.
    # Same two-gate shape as the chat pair, one pair per store-group (plan §3.1):
    #   encrypt_journal_writes / read_encrypted_journal — the journal group PLUS
    #     lessons + insights + core (they co-feed the USER.md envelope / memory_sync
    #     and read as one memory surface, so they flip together).
    #   encrypt_fuel_writes / read_encrypted_fuel — the fuel free-text surface
    #     (independent envelope/runtime views; its own rollback lever).
    # All default False; the sidecar *_enc columns ship DARK until PR-2 dual-writes.
    encrypt_journal_writes = models.BooleanField(
        default=False,
        help_text="Dual-write sealed *_enc envelopes for journal/lessons/insights/core content (encryption-at-rest Phase 3).",
    )
    read_encrypted_journal = models.BooleanField(
        default=False,
        help_text="Read journal/lessons/insights/core content back through the *_enc column when present (encryption-at-rest Phase 3).",
    )
    encrypt_fuel_writes = models.BooleanField(
        default=False,
        help_text="Dual-write sealed *_enc envelopes for fuel content (encryption-at-rest Phase 3).",
    )
    read_encrypted_fuel = models.BooleanField(
        default=False,
        help_text="Read fuel content back through the *_enc column when present (encryption-at-rest Phase 3).",
    )

    # Site publishing module — lets the assistant push portfolio images to the
    # subscriber's own website (Azure Blob + Cosmos) via the tenant managed
    # identity. Gated per tenant in config_generator; the plugin self-gates on
    # site_config, so a flagged-but-unconfigured tenant just gets an inert tool.
    site_publishing_enabled = models.BooleanField(
        default=False,
        help_text="Enable the assistant to publish images to the subscriber's own website",
    )
    site_config = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Per-tenant website publishing targets for the nbhd-site-publishing "
            "plugin. Keys: cosmosEndpoint, cosmosDatabase, cosmosContainer, "
            "blobAccount, blobContainer, blobPathPrefix."
        ),
    )

    # Neighborhood (Friends) module — cross-tenant sharing, wormholes, chat,
    # Missions. Dark by default; rolled out per-tenant like every other pillar.
    # Product surface is "Neighborhood"; the flag/app stay ``friends_*``.
    friends_enabled = models.BooleanField(
        default=False,
        help_text="Enable the Neighborhood (Friends) layer — waves, shared sparks, wormholes, chat",
    )
    friends_agent_propose_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Let the assistant PROPOSE shares/mission-tasks (human still approves every one). "
            "Off + friends_enabled = an absorb-only agent: it reads neighbors' sparks but never "
            "proposes. Gates the propose plugin tools, the AGENTS.md propose rules, and the "
            "runtime propose endpoints."
        ),
    )

    # Document information-keeping (docs/document-information-keeping-directive.md).
    # When on: the nbhd-document-keep plugin loads (record/list/forget tools), the
    # flag-gated DOCUMENT_KEEP_REMOVAL_GATE lands in AGENTS.md, and the D8 same-turn
    # write backstop is armed. Off keeps the base behavioral gate + generic rules
    # file (fleet-wide, tool-name-free) but never names a tool the tenant lacks nor
    # blocks a write. Canary-scoped for Phase 2; default-on at the fleet flip.
    document_ingestion_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Enable document information-keeping: the record/list/forget tools, the "
            "AGENTS.md tool-language block, and the same-turn write backstop that "
            "blocks a durable write on the turn a document arrived."
        ),
    )

    # Email/calendar/Reddit ingestion provenance (continuity-directive P3, Phase 5).
    # When on: the AGENTS.md email-provenance gate lands — teaching the agent to
    # PROPOSE before saving anything learned from a Gmail/calendar/Reddit read
    # (that text is attacker-controllable, D8) and to stamp such saves onto the
    # SAME document-keeping ledger with source_kind + a "gmail:<id>" source_ref, so
    # "forget everything from that email" works like forgetting a PDF. Reuses the
    # nbhd_document_keep tools, so enable this only alongside document_ingestion_enabled.
    # Held OFF (incl. canary) until the AGENTS.md budget headroom is resolved and the
    # OpenClaw image ships the plugin's source_kind/source_ref params (next image roll).
    email_provenance_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Enable email/calendar/Reddit ingestion provenance: the AGENTS.md gate "
            "teaching propose-then-stamp for information saved from a read, recorded on "
            "the document-keeping ledger with a source_kind + source_ref. Requires "
            "document_ingestion_enabled (the record/list/forget tools) and the plugin image roll."
        ),
    )

    # Welcome-cron delivery telemetry. Keys are feature names ("fuel",
    # "finance"), values are ISO-8601 timestamps of successful welcome
    # delivery. The welcome prompt instructs the agent to call
    # /api/internal/welcomes/mark/<feature>/ after nbhd_send_to_user
    # succeeds; the deploy-time backfill skips tenants where the flag is
    # set and re-schedules for those where it isn't (closing the
    # "scheduled but failed silently" gap).
    welcomes_sent = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-feature welcome-delivery timestamps (e.g. {'fuel': '2026-05-07T...', 'finance': ...})",
    )

    # Rollback capture for the sentinel-split SOUL.md/IDENTITY.md migration.
    # ``backfill_identity_growth`` stores each container tenant's pre-migration
    # SOUL/IDENTITY verbatim under ``identity_growth['pre_migration_snapshot']``
    # ({'soul': ..., 'identity': ..., 'captured_at': iso}) BEFORE the first
    # managed-region push, so a bad splice can be reverted from Postgres truth.
    identity_growth = models.JSONField(
        null=True,
        blank=True,
        default=dict,
        help_text="Identity rollback + growth snapshots for SOUL.md/IDENTITY.md (see backfill_identity_growth).",
    )

    # Constellation cluster-name cache for the async LLM naming pass
    # (apps.lessons.cluster_naming). Keyed on a hash of a cluster's SORTED
    # member lesson ids — cluster_id numbers are reassigned every recluster, so
    # they can't be the key. A cache hit reuses the stored name with no LLM
    # call; entries whose member-hash no longer exists are pruned each run.
    # Shape: {"<sha1-of-sorted-ids>": "Weight Tracking"}.
    cluster_label_cache = models.JSONField(
        default=dict,
        blank=True,
        help_text="Cached LLM cluster names keyed by hash of sorted member lesson ids.",
    )

    # BYO subscription mode — Phase 1 gates Anthropic Claude Pro/Max CLI
    # behind this flag. After fleet rollout (PR #434, 2026-05-02) the default
    # is True; existing rows are flipped via migration 0051. Newly provisioned
    # tenants are auto-enabled and can connect from day one.
    byo_models_enabled = models.BooleanField(
        default=True,
        help_text="Enable bring-your-own Anthropic/OpenAI subscription mode for this tenant",
    )

    # Idle hibernation
    hibernated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the container was idle-hibernated. Null = running normally.",
    )
    cron_wake_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the container was woken for a scheduled cron job. "
        "Null = not a cron wake. Used to apply the shorter 30-min idle window.",
    )
    last_wake_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the container was last woken from hibernation. The "
        "message drain treats container-down errors within a grace window "
        "of this as 'still booting' (retry soon, don't burn delivery "
        "attempts) instead of delivery failures.",
    )

    # Workspace routing
    active_workspace = models.ForeignKey(
        "journal.Workspace",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Currently active conversation workspace. Null = no workspaces.",
    )

    # Metadata
    last_message_at = models.DateTimeField(null=True, blank=True)
    first_message_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "Timestamp of the tenant's first-ever inbound message across "
            "any channel. Set once at the first PendingMessage insert and "
            "never bumped again — used to measure onboarding activation."
        ),
    )
    welcome_email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When the Day-0 welcome email was delivered to the tenant's "
            "User.email. Set after a successful send; checked as the "
            "idempotency guard so provisioning retries don't re-send."
        ),
    )
    config_version = models.IntegerField(
        default=0,
        help_text="Current applied config version",
    )
    pending_config_version = models.IntegerField(
        default=0,
        help_text="Latest available config version; > config_version means update pending",
    )
    provisioned_at = models.DateTimeField(null=True, blank=True)
    config_refreshed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenants"

    def __str__(self) -> str:
        return f"{self.user.display_name} ({self.status})"

    def clean(self):
        super().clean()
        if self.heartbeat_window_hours is not None and self.heartbeat_window_hours > 6:
            raise ValidationError({"heartbeat_window_hours": "Heartbeat window cannot exceed 6 hours."})

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def finance_active(self) -> bool:
        """Effective Gravity state: the per-tenant flag AND the platform gate.

        When ``settings.GRAVITY_ENABLED`` is False (the production default),
        Gravity is paused platform-wide for privacy regardless of the stored
        ``finance_enabled`` flag. All finance egress + surfacing gates read this
        property, not ``finance_enabled`` directly, so flipping the env var off
        is an authoritative kill switch. See ``GRAVITY_ENABLED`` in settings.
        """
        from django.conf import settings

        return bool(self.finance_enabled) and bool(getattr(settings, "GRAVITY_ENABLED", False))

    @property
    def has_entitlement(self) -> bool:
        """True if tenant has a paid subscription, an unexpired trial, or is budget-exempt.

        Budget-exempt tenants (canary/internal accounts that sit outside the
        billing lifecycle) are always entitled — this mirrors ``entitled_active()``
        below and ``_unentitled_active_tenants()`` in apps/cron/views.py, which
        both already include ``is_budget_exempt``.
        """
        from django.utils import timezone

        has_subscription = bool(self.stripe_subscription_id)
        on_valid_trial = bool(self.is_trial) and self.trial_ends_at and self.trial_ends_at > timezone.now()
        return has_subscription or on_valid_trial or bool(self.is_budget_exempt)

    @classmethod
    def entitled_active(cls):
        """Active tenants with valid entitlement (paid, unexpired trial, or budget-exempt).

        Uses positive inclusion logic to mirror the exact inverse of
        _unentitled_active_tenants() in apps/cron/views.py. The previous
        negative-exclude approach missed "ghost" tenants whose is_trial flag was
        flipped to False without a SUSPENDED transition — they passed the narrow
        .exclude(is_trial=True, ...) filter even though has_entitlement returns
        False for them, causing spurious cron seeding and config applies for up
        to one day until the daily expire_trials sweep caught them.
        """
        from django.utils import timezone

        now = timezone.now()
        return cls.objects.filter(
            status=cls.Status.ACTIVE,
            container_id__gt="",
        ).filter(
            models.Q(stripe_subscription_id__gt="")
            | models.Q(is_trial=True, trial_ends_at__gt=now)
            | models.Q(is_budget_exempt=True),
        )

    @property
    def effective_token_budget(self) -> int:
        """Resolve the active budget: explicit override or tier default.  0 = unlimited."""
        from apps.billing.constants import TIER_TOKEN_BUDGETS

        if self.monthly_token_budget > 0:
            return self.monthly_token_budget
        return TIER_TOKEN_BUDGETS.get(self.model_tier, 5_000_000)

    @property
    def effective_cost_budget(self) -> Decimal:
        """Resolve the active cost cap in USD: explicit override or tier default.  0 = unlimited."""
        from apps.billing.constants import TIER_COST_BUDGETS

        if self.monthly_cost_budget > 0:
            return self.monthly_cost_budget
        budget = TIER_COST_BUDGETS.get(self.model_tier, 5.00)
        return Decimal(str(budget)) if budget else Decimal("0")

    @property
    def is_over_budget(self) -> bool:
        budget = self.effective_cost_budget
        if budget == 0:
            return False
        return self.estimated_cost_this_month >= budget

    @property
    def has_spendable_budget(self) -> bool:
        """Single source of truth for "is this tenant allowed to spend right now".

        Allowed when budget-exempt, OR still within the monthly included
        allowance, OR holding prepaid credit. Purchased credit is kept SEPARATE
        from ``is_over_budget``/``effective_cost_budget`` on purpose: those drive
        the included-allowance threshold emails and the OpenRouter 402 breaker,
        which must keep their "included cap" meaning. See check_budget.
        """
        return self.is_budget_exempt or not self.is_over_budget or self.purchased_credit > 0

    def bump_pending_config(self):
        """Signal that agent config needs refreshing."""
        self.pending_config_version = (self.pending_config_version or 0) + 1
        self.save(update_fields=["pending_config_version"])


class UserSituation(models.Model):
    """Structured, short-lived observations about a tenant's current situation.

    Writes must go through :mod:`apps.tenants.situation`; capture callers never
    update this row directly. The timestamps distinguish when a value first
    changed from when the same value was most recently observed so renderers can
    decay stale context without erasing its history.
    """

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="situation")
    current_place_label = models.CharField(max_length=64, blank=True, default="")
    current_place_since = models.DateTimeField(null=True)
    current_place_last_observed_at = models.DateTimeField(null=True)
    current_place_source = models.CharField(max_length=16, blank=True, default="")
    device_tz = models.CharField(max_length=64, blank=True, default="")
    device_tz_since = models.DateTimeField(null=True)
    device_tz_last_observed_at = models.DateTimeField(null=True)
    device_tz_source_device = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_situations"

    def __str__(self) -> str:
        return str(self.tenant_id)


class TenantDek(models.Model):
    """A tenant's Data Encryption Key, wrapped under its Key Encryption Key.

    Encryption-at-rest Phase 1 (envelope encryption, see
    ``CONTINUITY_encryption-phase1.md``): each tenant gets a 32-byte DEK
    used to encrypt content columns directly; the DEK itself never touches
    disk in plaintext — it is wrapped (RSA-OAEP-256) under the tenant's KEK
    in Azure Key Vault (``apps.orchestrator.azure_client.wrap_dek``) and only
    that ciphertext is stored here. ``apps.crypto.keys`` is the only code
    that should read/write this table directly.

    ``dek_epoch`` is the rotation counter (Phase 5) — epoch 0 is minted at
    provisioning time and is the only epoch Phase 1 ever creates. A future
    rotation inserts a new row at ``dek_epoch + 1`` rather than overwriting
    this one, so old ciphertext (still tagged with its original epoch in the
    envelope header) keeps decrypting under the DEK that encrypted it.

    RLS posture: intentionally NOT force-RLS, NOT in the friends
    ``RLS_KEEP_ENABLED`` keep-set — this table runs RLS-off in prod like the
    rest of the fleet default (see ``apps/tenants/management/commands/
    disable_rls.py``); Django itself is the tenant boundary here, same as
    every other control-plane table. The migration-time relock (see the
    migration immediately after this table's creation migration) exists only
    to satisfy ``apps.tenants.test_public_schema_lockdown`` at migration
    time, before the boot-time ``disable_rls`` sweep runs. Do not add a
    FORCE-RLS policy for this table as part of Phase 1.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="deks")
    dek_epoch = models.PositiveSmallIntegerField(default=0)
    wrapped_dek = models.BinaryField()
    kek_version = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = (("tenant", "dek_epoch"),)

    def __str__(self) -> str:
        return f"{self.tenant_id}:epoch{self.dek_epoch}"
