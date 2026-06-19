"""OAuth/PKCE authorization code for the web→app sign-in handoff.

The iOS app ("Create an account") runs an RFC 7636 PKCE flow through the
hosted web frontend: the device keeps a ``code_verifier`` and sends only its
S256 ``code_challenge`` to the web ``/app/authorize`` page. After the user
registers/signs in on the web, the SPA mints a one-time, short-TTL ``code``
(this model) and redirects ``nbhd://auth/callback?code=…&state=…``. The app
then exchanges the code (plus the verifier) over the authenticated HTTPS
back-channel for a SimpleJWT pair.

Mirrors ``PersonalAccessToken`` hashing (``pat_models.py``) — only the SHA-256
of the code is stored, never the raw code — and ``TelegramLinkToken``'s
single-use + TTL shape (``telegram_models.py``).
"""

import base64
import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

CODE_BYTE_LENGTH = 32  # 32 bytes → 43-char base64url


def generate_authorization_code() -> tuple[str, str]:
    """Generate a new authorization code.

    Returns ``(raw_code, code_hash)``. ``raw_code`` is handed to the SPA once
    (and travels through the ``nbhd://`` redirect); only ``code_hash`` is
    stored, so a leaked DB row cannot be replayed.
    """
    raw = secrets.token_urlsafe(CODE_BYTE_LENGTH)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def hash_authorization_code(raw_code: str) -> str:
    """Hash a raw authorization code for lookup."""
    return hashlib.sha256(raw_code.encode()).hexdigest()


def pkce_s256(verifier: str) -> str:
    """RFC 7636 S256: ``BASE64URL(SHA256(ASCII(verifier)))`` with no padding.

    Byte-for-byte identical to the iOS ``PKCE.challenge(for:)`` derivation
    (``nbhd-ios`` ``PKCE.swift``): SHA-256 of the verifier's bytes, urlsafe
    base64 with ``=`` stripped. Compare against the stored challenge with
    ``hmac.compare_digest`` (constant time).
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OAuthAuthorizationCode(models.Model):
    """Single-use, short-TTL PKCE authorization code for the web→app handoff."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_authorization_codes",
    )
    code_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 hex digest of the raw code handed to the SPA.",
    )
    code_challenge = models.CharField(
        max_length=128,
        help_text="S256 base64url challenge (~43 chars). Re-derived from the "
        "verifier at exchange and compared constant-time.",
    )
    code_challenge_method = models.CharField(max_length=8, default="S256")
    redirect_uri = models.CharField(max_length=255)
    client = models.CharField(max_length=32, default="ios")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "oauth_authorization_codes"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["code_hash"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.code_hash[:8]}… ({self.client})"

    @property
    def is_valid(self) -> bool:
        """Code is usable: not yet consumed and not expired."""
        return self.consumed_at is None and timezone.now() < self.expires_at
