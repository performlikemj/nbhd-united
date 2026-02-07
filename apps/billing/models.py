import uuid

from django.db import models

from apps.tenants.models import Tenant


class Plan(models.Model):
    """Subscription plan definition."""

    class Tier(models.TextChoices):
        FREE = "free", "Free"
        PAID = "paid", "Paid"
        SPONSOR = "sponsor", "Sponsor"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    stripe_price_id = models.CharField(max_length=255, blank=True, default="")
    tier = models.CharField(max_length=20, choices=Tier.choices, default=Tier.FREE)
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    token_budget = models.IntegerField(default=100_000, help_text="Daily token budget")
    features = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "plans"

    def __str__(self):
        return f"{self.name} ({self.tier})"


class UsageEvent(models.Model):
    """Granular usage tracking for billing and analytics."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="usage_events")
    event_type = models.CharField(max_length=50)  # llm_call, tool_use, etc.
    tokens = models.IntegerField(default=0)
    model_used = models.CharField(max_length=255, blank=True, default="")
    cost_estimate = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "usage_events"
        indexes = [
            models.Index(fields=["tenant", "created_at"]),
            models.Index(fields=["event_type", "created_at"]),
        ]

    def __str__(self):
        return f"{self.event_type}: {self.tokens} tokens ({self.tenant})"
