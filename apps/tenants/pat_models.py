"""Personal Access Token models for external app authentication."""

import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

TOKEN_PREFIX = "pat_"
TOKEN_BYTE_LENGTH = 32  # 32 bytes → 43 chars base64url


def generate_pat() -> tuple[str, str, str]:
    """Generate a new PAT.

    Returns (raw_token, token_prefix, token_hash).
    raw_token is shown to the user once; token_hash is stored.
    """
    raw_secret = secrets.token_urlsafe(TOKEN_BYTE_LENGTH)
    raw_token = f"{TOKEN_PREFIX}{raw_secret}"
    prefix = raw_secret[:8]
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, prefix, token_hash


def hash_token(raw_token: str) -> str:
    """Hash a raw PAT for lookup."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


class PersonalAccessToken(models.Model):
    """A long-lived, revocable token for external app authentication.

    Users create these from the subscriber console to authorize apps
    like YardTalk to push data to their NU account.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="personal_access_tokens")
    name = models.CharField(
        max_length=255,
        help_text="Human label, e.g. 'YardTalk on MacBook Pro'",
    )
    token_prefix = models.CharField(
        max_length=8,
        help_text="First 8 chars of the secret (for identification in UI)",
    )
    token_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 hex digest of the full pat_... token",
    )
    scopes = models.JSONField(
        default=list,
        blank=True,
        help_text='Allowed scopes, e.g. ["sessions:write", "sessions:read"]',
    )
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Null = never expires",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "personal_access_tokens"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["token_hash"]),
            models.Index(fields=["user", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.name} (pat_{self.token_prefix}...)"

    @property
    def is_valid(self) -> bool:
        """Token is usable: not revoked and not expired."""
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return False
        return True

    def touch(self):
        """Update last_used_at without triggering full save overhead."""
        PersonalAccessToken.objects.filter(pk=self.pk).update(last_used_at=timezone.now())
