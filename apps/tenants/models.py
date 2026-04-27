"""Tenant models — core of the control plane."""

import uuid
from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.orchestrator.tool_policy import OPENCLAW_CURRENT_VERSION  # noqa: I001

from .line_models import LineLinkToken  # noqa: F401

# Import so Django discovers the models for migrations
from .pat_models import PersonalAccessToken  # noqa: F401
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

    class Meta:
        db_table = "users"

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

    # NOTE: Per-tenant internal API keys were removed (2026-02-22).
    # All containers share a single key via Azure Key Vault. This is safe
    # because tenant containers are internal-only (external: false) — not
    # reachable from the public internet. The per-tenant scheme caused mass
    # auth failures and added unnecessary complexity.
    # Fields `internal_api_key_hash` and `internal_api_key_set_at` were
    # dropped in migration 0018.

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

    # Feature tips
    feature_tips_enabled = models.BooleanField(
        default=True,
        help_text="Whether the assistant proactively suggests platform features",
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

    # Model preference
    task_model_preferences = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-task model overrides. Keys: heartbeat, morning_briefing, "
        "evening_checkin, week_review, background_tasks. "
        "Values: model IDs.",
    )
    # Cron job backup — snapshot of the last-known cron.list response.
    # Used to restore user-created jobs after container restarts.
    cron_jobs_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text='Last-known cron job list from gateway. Format: {"jobs": [...], "snapshot_at": "ISO8601"}',
    )

    preferred_model = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="User's preferred primary model (overrides tier default when set)",
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
    def has_entitlement(self) -> bool:
        """True if tenant has a paid subscription or an unexpired trial."""
        from django.utils import timezone

        has_subscription = bool(self.stripe_subscription_id)
        on_valid_trial = bool(self.is_trial) and self.trial_ends_at and self.trial_ends_at > timezone.now()
        return has_subscription or on_valid_trial

    @classmethod
    def entitled_active(cls):
        """Active tenants with valid entitlement (paid or unexpired trial)."""
        from django.utils import timezone

        now = timezone.now()
        return cls.objects.filter(
            status=cls.Status.ACTIVE,
            container_id__gt="",
        ).exclude(
            is_trial=True,
            trial_ends_at__lte=now,
            stripe_subscription_id="",
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

    def bump_pending_config(self):
        """Signal that agent config needs refreshing."""
        self.pending_config_version = (self.pending_config_version or 0) + 1
        self.save(update_fields=["pending_config_version"])
