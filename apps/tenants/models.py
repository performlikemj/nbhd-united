"""Tenant models — core of the control plane."""
import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models

# Import so Django discovers the model for migrations
from .telegram_models import TelegramLinkToken  # noqa: F401


class User(AbstractUser):
    """Custom user model with Telegram binding."""

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
        STARTER = "starter", "Starter (Kimi K2.5)"
        PREMIUM = "premium", "Premium (Sonnet/Opus)"
        BYOK = "byok", "Bring Your Own Key"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="tenant")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    model_tier = models.CharField(
        max_length=20, choices=ModelTier.choices, default=ModelTier.STARTER
    )

    # OpenClaw container
    container_id = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Azure Container App name (e.g. oc-usr-abc123)",
    )
    container_fqdn = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Internal FQDN of the container",
    )

    # Azure Key Vault
    key_vault_prefix = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Key Vault secret prefix (e.g. tenants-<uuid>)",
    )
    managed_identity_id = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Azure User-Assigned Managed Identity resource ID",
    )

    # Stripe (dj-stripe handles subscription objects; this is a quick-lookup cache)
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="")
    stripe_subscription_id = models.CharField(max_length=255, blank=True, default="")

    # Usage tracking
    messages_today = models.IntegerField(default=0)
    messages_this_month = models.IntegerField(default=0)
    tokens_this_month = models.IntegerField(default=0)
    estimated_cost_this_month = models.DecimalField(
        max_digits=10, decimal_places=4, default=0
    )
    monthly_token_budget = models.IntegerField(
        default=500_000,
        help_text="Per-user monthly token budget",
    )

    # Per-tenant internal API key
    internal_api_key_hash = models.CharField(
        max_length=64, blank=True, default="",
        help_text="SHA-256 hex digest of this tenant's internal API key",
    )
    internal_api_key_set_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the per-tenant internal API key was last generated",
    )

    # Metadata
    last_message_at = models.DateTimeField(null=True, blank=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenants"

    def __str__(self) -> str:
        return f"{self.user.display_name} ({self.status})"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def is_over_budget(self) -> bool:
        return self.tokens_this_month >= self.monthly_token_budget


class UserLLMConfig(models.Model):
    """Stores a user's BYOK (Bring Your Own Key) LLM configuration."""

    class Provider(models.TextChoices):
        OPENAI = "openai", "OpenAI"
        ANTHROPIC = "anthropic", "Anthropic"
        GROQ = "groq", "Groq"
        GOOGLE = "google", "Google Gemini"
        OPENROUTER = "openrouter", "OpenRouter"
        XAI = "xai", "xAI (Grok)"

    user = models.OneToOneField(
        "tenants.User",
        on_delete=models.CASCADE,
        related_name="llm_config",
    )
    provider = models.CharField(
        max_length=30,
        choices=Provider.choices,
        default=Provider.ANTHROPIC,
    )
    encrypted_api_key = models.TextField(
        blank=True,
        default="",
        help_text="Fernet-encrypted API key",
    )
    model_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Full model ID e.g. 'anthropic/claude-sonnet-4-20250514'",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_llm_configs"

    def __str__(self):
        return f"{self.user.display_name} - {self.provider}"
