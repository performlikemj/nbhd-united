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
        GMAIL = "gmail", "Gmail"
        GOOGLE_CALENDAR = "google-calendar", "Google Calendar"
        SAUTAI = "sautai", "Sautai"

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
        max_length=255, blank=True, default="",
        help_text="Key Vault secret name where tokens are stored",
    )
    token_expires_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations"
        unique_together = [("tenant", "provider")]

    def __str__(self) -> str:
        return f"{self.provider} ({self.tenant})"
