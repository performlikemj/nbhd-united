"""Persistence for the Sign in with Apple web flow."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_PROVIDER = "apple"


class ExternalIdentity(models.Model):
    """A server-verified external identity linked to one local user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="external_identities",
    )
    provider = models.CharField(max_length=32, default=APPLE_PROVIDER, editable=False)
    subject = models.CharField(max_length=255)
    issuer = models.CharField(max_length=255, default=APPLE_ISSUER, editable=False)
    audience = models.CharField(max_length=255)
    email_at_auth = models.EmailField(blank=True, default="")
    email_is_relay = models.BooleanField(default=False)
    email_verified_at_auth = models.BooleanField(default=False)
    refresh_token_encrypted = models.TextField()
    refresh_token_updated_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "external_identities"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "subject"],
                name="uq_external_identity_provider_subject",
            ),
            models.UniqueConstraint(
                fields=["user", "provider"],
                name="uq_external_identity_user_provider",
            ),
        ]


class AppleGrant(models.Model):
    """One encrypted Apple refresh token scoped to its issuing client."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity = models.ForeignKey(
        ExternalIdentity,
        on_delete=models.CASCADE,
        related_name="grants",
    )
    client_id = models.CharField(max_length=255, db_index=True)
    refresh_token_encrypted = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "apple_grants"
        constraints = [
            models.UniqueConstraint(
                fields=["identity", "client_id"],
                name="uq_apple_grant_identity_client",
            ),
        ]


class AppleAuthTransaction(models.Model):
    """Short-lived, single-use state and nonce binding for one Apple popup."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=64)
    nonce_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    purpose = models.CharField(max_length=32, default="web_auth")

    class Meta:
        db_table = "apple_auth_transactions"


class AppleRevocationOutbox(models.Model):
    """Durable Apple refresh-token revocation work, independent of a user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token_ciphertext = models.TextField()
    subject = models.CharField(max_length=255, null=True)
    subject_verified = models.BooleanField(default=True)
    client_id = models.CharField(max_length=255, null=True)
    backfill_source = models.CharField(max_length=32, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, db_index=True)
    claimed_at = models.DateTimeField(null=True)
    consecutive_invalid_client = models.PositiveSmallIntegerField(default=0)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        db_table = "apple_revocation_outbox"
