"""Per-tenant internal API key generation and hashing utilities."""
from __future__ import annotations

import hashlib
import secrets


def generate_internal_api_key() -> str:
    """Generate a cryptographically random internal API key.

    Returns a 43-character URL-safe base64 string (32 bytes of entropy).
    """
    return secrets.token_urlsafe(32)


def hash_internal_api_key(plaintext_key: str) -> str:
    """Compute SHA-256 hex digest of an internal API key.

    Returns a 64-character lowercase hex string.
    """
    return hashlib.sha256(plaintext_key.encode("utf-8")).hexdigest()
