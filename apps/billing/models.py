"""Billing models — usage tracking alongside dj-stripe."""

import uuid

from django.db import models

from apps.billing.constants import DEEPSEEK_MODEL, NEMOTRON_FREE_MODEL
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
    # System events (Phase 4 weekly reflection, future platform-side LLM work)
    # are charged to platform spend, not the tenant's monthly budget. The row
    # still persists for observability — see record_usage(is_system=True).
    is_system_event = models.BooleanField(default=False)
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


class DonationLedger(models.Model):
    """Tracks monthly surplus donation calculations and disbursements."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="donation_ledger")
    month = models.DateField(help_text="First day of the month")
    surplus_amount = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=0,
        help_text="Total surplus for the month",
    )
    donation_amount = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=0,
        help_text="Amount allocated to donation (surplus * percentage)",
    )
    donation_percentage = models.IntegerField(
        default=100,
        help_text="Snapshot of tenant's donation_percentage at calculation time",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    receipt_reference = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="External receipt or transaction reference",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "donation_ledger"
        unique_together = [("tenant", "month")]
        indexes = [
            models.Index(fields=["month", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant} {self.month}: ${self.donation_amount} ({self.status})"


class InfraCostSnapshot(models.Model):
    """Daily snapshot of real infrastructure costs per tenant from Azure billing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="infra_costs")
    month = models.DateField(help_text="First day of the month")
    container_cost = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    storage_cost = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    database_share = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    total_cost = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    source = models.CharField(
        max_length=20,
        default="estimate",
        help_text="'azure' for real billing data, 'estimate' for fallback",
    )
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "infra_cost_snapshots"
        unique_together = [("tenant", "month")]
        indexes = [
            models.Index(fields=["month"]),
        ]

    def __str__(self) -> str:
        return f"{self.tenant} {self.month}: ${self.total_cost} ({self.source})"


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


class ModelHealth(models.Model):
    """Per-model availability state for OpenRouter-routed models.

    Populated two ways:
      - the ``model_health_check`` cron actively probes the free-offer model
        (a ``/models`` pricing read + a 1-token completion ping) and reads
        pricing for every monitored model from the ``/models`` listing;
      - the control-plane fallback wrapper (apps/common/openrouter.py) records
        success/failure for whatever model it actually calls, so the models we
        only use on demand (synthesis, extraction, arbiter, hints) self-report.

    Read by the AI-provider settings page (offer/health badges) and by the
    offer-transition logic that decides whether the free promo stays live.
    """

    model_id = models.CharField(
        max_length=255,
        unique=True,
        help_text="Canonical OpenClaw-form model id, e.g. openrouter/deepseek/deepseek-v4-pro",
    )
    is_reachable = models.BooleanField(default=True)
    is_free = models.BooleanField(
        default=False,
        help_text="True when OpenRouter reports prompt + completion price == 0.",
    )
    consecutive_failures = models.IntegerField(default=0)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    pricing = models.JSONField(
        default=dict,
        blank=True,
        help_text="Last-seen OpenRouter pricing block for this model.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "model_health"

    def __str__(self) -> str:
        flags = []
        flags.append("up" if self.is_reachable else "down")
        if self.is_free:
            flags.append("free")
        return f"{self.model_id} ({', '.join(flags)})"


class FreeModelOffer(models.Model):
    """Singleton (pk=1) tracking the limited-time free-model promotion.

    ``is_active`` is the *advertised* state — the source of truth the resolver
    (apps/billing/model_offers.py) reads to decide the default chat model. The
    ``model_health_check`` cron flips it only on a real transition (free→paid,
    reachable→down, or back), and on each flip it bumps affected tenant configs
    and notifies users. ``enabled`` is an operator kill-switch that forces the
    promo off regardless of health.
    """

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    model_id = models.CharField(
        max_length=255,
        default=NEMOTRON_FREE_MODEL,
        help_text="The model offered for free while the promo runs.",
    )
    fallback_model_id = models.CharField(
        max_length=255,
        default=DEEPSEEK_MODEL,
        help_text="Model tenants fall back to when the promo is not active.",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Operator kill-switch. When False the promo never activates regardless of health.",
    )
    is_active = models.BooleanField(
        default=False,
        help_text="Whether the free model is currently the advertised default. Driven by the health cron.",
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    last_transition_reason = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "free_model_offer"

    def __str__(self) -> str:
        state = "active" if (self.enabled and self.is_active) else "inactive"
        return f"FreeModelOffer({self.model_id}, {state})"

    @classmethod
    def load(cls) -> "FreeModelOffer":
        """Return the singleton row, creating it with defaults if absent."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
