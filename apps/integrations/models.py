"""Integration models — tracks OAuth connections per tenant.

Actual tokens are stored in Azure Key Vault, not in the database.
This model tracks metadata about connections.
"""

import uuid

from django.db import models

from apps.tenants.models import Tenant


class Integration(models.Model):
    """An OAuth integration connecting a tenant to an external service."""

    class Provider(models.TextChoices):
        GOOGLE = "google", "Google Workspace"
        SAUTAI = "sautai", "Sautai"
        REDDIT = "reddit", "Reddit"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="integrations")
    provider = models.CharField(max_length=50, choices=Provider.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    scopes = models.JSONField(default=list, blank=True)
    provider_email = models.CharField(max_length=255, blank=True, default="")
    key_vault_secret_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Key Vault secret name where tokens are stored",
    )
    composio_connected_account_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Composio connected account ID (for Composio-managed providers)",
    )
    token_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations"
        unique_together = [("tenant", "provider")]

    def __str__(self) -> str:
        return f"{self.provider} ({self.tenant})"


class SautaiMealPlanJobStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class SautaiMealPlanJob(models.Model):
    """One meal-plan generation requested against sautai's M2M API (Phase 0).

    Created PENDING by ``RuntimeSautaiGeneratePlanView`` (fast, <20s — the
    plugin's own timeout), rendered by the async QStash task
    ``generate_sautai_meal_plan_task`` (sautai blocks 30-60s), which flips
    status to READY/FAILED and — on READY — fires the meditation-style
    completion path (``notify_sautai_plan_ready`` -> ``record_proactive_outbound``
    -> APNs + ``?since=`` feed row). See docs/sautai-phase0-contract.md.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="sautai_meal_plan_jobs")
    status = models.CharField(
        max_length=20,
        choices=SautaiMealPlanJobStatus.choices,
        default=SautaiMealPlanJobStatus.PENDING,
    )
    week_start = models.DateField(
        null=True,
        blank=True,
        help_text="Requested week (Monday). Blank lets sautai default to the current week.",
    )
    number_of_days = models.PositiveSmallIntegerField(default=7)
    # Placeholder-space at rest (pseudonymize-at-rest — same posture as
    # ProactiveOutbound.message_text): stored exactly as the plugin passed it
    # (may carry [PERSON_N] placeholders); rehydrated only at sautai egress in
    # the QStash task, never persisted rehydrated.
    user_prompt = models.TextField(blank=True, default="")
    result = models.JSONField(default=dict, blank=True, help_text="sautai's plan payload, once READY")
    web_link = models.CharField(max_length=500, blank=True, default="")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sautai_meal_plan_jobs"
        indexes = [models.Index(fields=["tenant", "status"])]

    def __str__(self) -> str:
        return f"SautaiMealPlanJob({self.id}, {self.status})"
