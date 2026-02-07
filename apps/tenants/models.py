import uuid

from django.contrib.auth.models import AbstractUser
from django.db import models


class Tenant(models.Model):
    """A tenant represents a single user's isolated environment."""

    class PlanTier(models.TextChoices):
        FREE = "free", "Free"
        PAID = "paid", "Paid"
        SPONSOR = "sponsor", "Sponsor"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    plan_tier = models.CharField(
        max_length=20, choices=PlanTier.choices, default=PlanTier.FREE
    )
    is_active = models.BooleanField(default=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenants"

    def __str__(self):
        return self.name


class User(AbstractUser):
    """Custom user model — every user belongs to a tenant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="users", null=True, blank=True
    )
    telegram_chat_id = models.BigIntegerField(unique=True, null=True, blank=True)
    telegram_user_id = models.BigIntegerField(null=True, blank=True)
    display_name = models.CharField(max_length=255, default="Friend")
    language = models.CharField(max_length=10, default="en")
    preferences = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "users"

    def __str__(self):
        return self.display_name or self.username


class AgentConfig(models.Model):
    """Per-tenant agent configuration."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name="agent_config"
    )
    name = models.CharField(max_length=255, default="Assistant")
    system_prompt = models.TextField(
        default="You are a helpful assistant for the Neighborhood United community."
    )
    model_tier = models.CharField(max_length=20, default="free")
    model_override = models.CharField(max_length=255, blank=True, default="")
    temperature = models.FloatField(default=0.7)
    max_tokens_per_message = models.IntegerField(default=2048)
    tools_enabled = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agent_configs"

    def __str__(self):
        return f"{self.name} ({self.tenant})"
