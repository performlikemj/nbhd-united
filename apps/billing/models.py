"""Billing models — usage tracking alongside dj-stripe."""
import uuid

from django.db import models

from apps.tenants.models import Tenant


class UsageRecord(models.Model):
    """Per-message usage tracking for billing and analytics."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="usage_records")
    event_type = models.CharField(max_length=50)  # message, tool_call, etc.
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    model_used = models.CharField(max_length=255, blank=True, default="")
    cost_estimate = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "usage_records"
        indexes = [
            models.Index(fields=["tenant", "created_at"]),
            models.Index(fields=["event_type", "created_at"]),
        ]

    def __str__(self) -> str:
        total = self.input_tokens + self.output_tokens
        return f"{self.event_type}: {total} tokens ({self.tenant})"


class MonthlyBudget(models.Model):
    """Global monthly budget cap — safety net."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    month = models.DateField(unique=True, help_text="First day of the month")
    budget_dollars = models.DecimalField(max_digits=10, decimal_places=2, default=100)
    spent_dollars = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    is_capped = models.BooleanField(
        default=False,
        help_text="If True, all non-essential API calls are blocked",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "monthly_budgets"

    def __str__(self) -> str:
        return f"{self.month}: ${self.spent_dollars}/{self.budget_dollars}"

    @property
    def remaining(self) -> float:
        return float(self.budget_dollars - self.spent_dollars)

    @property
    def is_over_budget(self) -> bool:
        return self.spent_dollars >= self.budget_dollars
