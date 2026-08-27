"""Shared signed-token helpers for side-effect-free confirmation previews."""

from __future__ import annotations

import hashlib
import hmac
import json

from django.core import signing

CONFIRM_TOKEN_MAX_AGE_SECONDS = 10 * 60


def confirmation_digest(context: dict) -> str:
    """Hash a canonical JSON context so tokens bind to exact parameters."""
    canonical = json.dumps(context, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def issue_confirm_token(context: dict, *, salt: str) -> str:
    return signing.TimestampSigner(salt=salt).sign(confirmation_digest(context))


def confirm_token_failure(
    confirm_token: str,
    context: dict,
    *,
    salt: str,
    max_age: int = CONFIRM_TOKEN_MAX_AGE_SECONDS,
) -> str | None:
    """Return an agent-facing failure reason, or ``None`` for a valid token."""
    if len(confirm_token) > 512:
        return "invalid"
    try:
        signed_digest = signing.TimestampSigner(salt=salt).unsign(confirm_token, max_age=max_age)
    except signing.SignatureExpired:
        return "expired"
    except signing.BadSignature:
        return "invalid"
    if not hmac.compare_digest(signed_digest, confirmation_digest(context)):
        return "mismatch"
    return None
