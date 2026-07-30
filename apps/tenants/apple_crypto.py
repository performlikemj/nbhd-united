"""Standalone encryption for Apple refresh tokens.

Apple identities can exist before a tenant does, so the tenant-DEK helper in
``apps.crypto.box`` is intentionally not used here.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


class AppleTokenCryptoError(Exception):
    """The configured keyring cannot encrypt or decrypt an Apple token."""


def _multi_fernet() -> MultiFernet:
    keys = getattr(settings, "APPLE_SIWA_TOKEN_ENC_KEYS", [])
    if not keys:
        raise AppleTokenCryptoError("empty_keyring")
    try:
        return MultiFernet([Fernet(key.encode("ascii")) for key in keys])
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise AppleTokenCryptoError("invalid_keyring") from exc


def validate_apple_token_keyring() -> bool:
    """Return whether the configured keyring is non-empty and parseable."""

    try:
        _multi_fernet()
    except AppleTokenCryptoError:
        return False
    return True


def encrypt_apple_refresh_token(token: str) -> str:
    try:
        ciphertext = _multi_fernet().encrypt(token.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError) as exc:
        raise AppleTokenCryptoError("invalid_token") from exc
    return ciphertext.decode("ascii")


def decrypt_apple_refresh_token(ciphertext: str) -> str:
    try:
        plaintext = _multi_fernet().decrypt(ciphertext.encode("ascii"))
        return plaintext.decode("utf-8")
    except (InvalidToken, AttributeError, UnicodeError, ValueError) as exc:
        raise AppleTokenCryptoError("decrypt_failed") from exc
