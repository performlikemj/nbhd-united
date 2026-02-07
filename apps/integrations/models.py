import uuid

from django.conf import settings
from django.db import models

from apps.tenants.models import Tenant


class Integration(models.Model):
    """OAuth integration per user per provider."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="integrations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="integrations")
    provider = models.CharField(max_length=50)  # google, slack, notion, etc.
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    scopes = models.JSONField(default=list, blank=True)
    access_token_ref = models.CharField(max_length=255, blank=True, default="")
    refresh_token_ref = models.CharField(max_length=255, blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    provider_user_id = models.CharField(max_length=255, blank=True, default="")
    provider_email = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations"
        unique_together = [("tenant", "provider")]

    def __str__(self):
        return f"{self.provider} ({self.tenant})"


class UserSecret(models.Model):
    """Non-OAuth secrets (API keys users provide)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="secrets")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="secrets")
    name = models.CharField(max_length=255)
    vault_ref = models.CharField(max_length=255)
    hint = models.CharField(max_length=10, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_secrets"
        unique_together = [("tenant", "name")]

    def __str__(self):
        return f"{self.name} (…{self.hint})"
