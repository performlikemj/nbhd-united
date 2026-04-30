"""Bring-your-own credential storage.

A `BYOCredential` row records that a tenant has connected an external AI
provider (Anthropic, OpenAI) using either an API key or a CLI subscription
OAuth token. The actual token value never lives in Postgres — only the
Key Vault secret name is stored. Tokens are read by the tenant's container
at boot via env-var-mapped Key Vault references.

See `CONTINUITY_byo_subscription_models.md` for the full design.
"""

from __future__ import annotations

import uuid

from django.db import models

from apps.tenants.models import Tenant


class BYOCredential(models.Model):
    class Provider(models.TextChoices):
        ANTHROPIC = "anthropic", "Anthropic"
        OPENAI = "openai", "OpenAI"

    class Mode(models.TextChoices):
        API_KEY = "api_key", "API Key"
        CLI_SUBSCRIPTION = "cli_subscription", "CLI Subscription"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending verification"
        VERIFIED = "verified", "Verified"
        EXPIRED = "expired", "Expired"
        ERROR = "error", "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="byo_credentials",
    )
    provider = models.CharField(max_length=20, choices=Provider.choices)
    mode = models.CharField(max_length=20, choices=Mode.choices)
    key_vault_secret_name = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    seed_version = models.PositiveIntegerField(
        default=0,
        help_text=(
            "Bumped on every paste/re-paste. Phase 2 Codex entrypoint "
            "will compare this against an on-disk marker to decide "
            "whether to overwrite the file-share-resident auth.json. "
            "Phase 1 (Anthropic env-var) ignores this field."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "provider"],
                name="byo_one_credential_per_provider",
            ),
        ]

    def __str__(self) -> str:
        return f"BYOCredential(tenant={self.tenant_id}, {self.provider}/{self.mode}, {self.status})"
