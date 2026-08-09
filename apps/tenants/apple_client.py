"""Apple OAuth client-secret, token exchange, and ID-token verification."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .apple_crypto import validate_apple_token_keyring
from .apple_models import APPLE_ISSUER

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"
APPLE_HTTP_TIMEOUT_SECONDS = 5
APPLE_CLIENT_SECRET_TTL_SECONDS = 300
UNKNOWN_KID_TTL_SECONDS = 60
FORCED_JWKS_REFRESH_COOLDOWN_SECONDS = 60


class AppleInvalidGrant(Exception):
    """Apple rejected the grant or its server-verified claims were invalid."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AppleUnavailable(Exception):
    """Apple's token/JWKS/revocation service could not be used safely."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AppleGrant:
    subject: str
    issuer: str
    audience: str
    email: str
    email_verified: bool
    email_is_relay: bool
    refresh_token: str | None


@dataclass(frozen=True)
class AppleRawExchange:
    """Apple token response before any ID-token claim is trusted."""

    id_token: object
    refresh_token: str | None


_jwks_client = jwt.PyJWKClient(
    APPLE_JWKS_URL,
    cache_keys=True,
    lifespan=300,
    timeout=APPLE_HTTP_TIMEOUT_SECONDS,
)
_unknown_kids: dict[str, float] = {}
_unknown_kids_lock = threading.RLock()
_last_forced_jwks_refresh_at: float | None = None


def _canonical_redirect_uri(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or port == 443:
        return False
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        return False
    hostname = parsed.hostname.lower()
    canonical_host = f"{hostname}:{port}" if port is not None else hostname
    return value == f"https://{canonical_host}"


def _p256_private_key_is_valid(value: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        key = serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        return False
    return isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1)


def apple_readiness_error() -> str | None:
    """Return a non-secret readiness reason, or ``None`` when fully ready."""

    required = (
        ("services_id", getattr(settings, "APPLE_SIWA_SERVICES_ID", "")),
        ("team_id", getattr(settings, "APPLE_SIWA_TEAM_ID", "")),
        ("key_id", getattr(settings, "APPLE_SIWA_KEY_ID", "")),
    )
    for reason, value in required:
        if not isinstance(value, str) or not value.strip():
            return f"missing_{reason}"
    if not _p256_private_key_is_valid(getattr(settings, "APPLE_SIWA_PRIVATE_KEY", "")):
        return "invalid_private_key"
    if not _canonical_redirect_uri(getattr(settings, "APPLE_SIWA_REDIRECT_URI", "")):
        return "invalid_redirect_uri"
    ttl = getattr(settings, "APPLE_SIWA_TRANSACTION_TTL_SECONDS", 0)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
        return "invalid_transaction_ttl"
    if not validate_apple_token_keyring():
        return "invalid_token_keyring"
    return None


def apple_native_bundle_id() -> str:
    """Return the normalized native SIWA audience."""

    value = getattr(settings, "APPLE_SIWA_BUNDLE_ID", "")
    return value.strip() if isinstance(value, str) else ""


def apple_native_readiness_error() -> str | None:
    """Return readiness for native SIWA without affecting the web lane."""

    readiness_error = apple_readiness_error()
    if readiness_error is not None:
        return readiness_error
    if not apple_native_bundle_id():
        return "missing_bundle_id"
    return None


def generate_apple_client_secret(client_id: str | None = None) -> str:
    """Mint the five-minute ES256 client assertion Apple requires."""

    now = int(datetime.now(UTC).timestamp())
    payload = {
        "iss": settings.APPLE_SIWA_TEAM_ID,
        "sub": client_id or settings.APPLE_SIWA_SERVICES_ID,
        "aud": APPLE_ISSUER,
        "iat": now,
        "exp": now + APPLE_CLIENT_SECRET_TTL_SECONDS,
    }
    return jwt.encode(
        payload,
        settings.APPLE_SIWA_PRIVATE_KEY,
        algorithm="ES256",
        headers={"kid": settings.APPLE_SIWA_KEY_ID},
    )


def _negative_kid_is_live(kid: str) -> bool:
    now = time.monotonic()
    with _unknown_kids_lock:
        expires_at = _unknown_kids.get(kid)
        if expires_at is None:
            return False
        if expires_at <= now:
            _unknown_kids.pop(kid, None)
            return False
        return True


def _negative_cache_kid(kid: str) -> None:
    with _unknown_kids_lock:
        _unknown_kids[kid] = time.monotonic() + UNKNOWN_KID_TTL_SECONDS


def _safe_string_compare(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def _invalidate_jwks_cache() -> None:
    cache = getattr(_jwks_client, "jwk_set_cache", None)
    if cache is not None:
        cache.put(None)


def _get_signing_keys(*, refresh: bool, unavailable_reason: str):
    try:
        signing_keys = _jwks_client.get_signing_keys(refresh=refresh)
    except jwt.PyJWKClientConnectionError as exc:
        raise AppleUnavailable(unavailable_reason) from exc
    except (OSError, TimeoutError) as exc:
        raise AppleUnavailable(unavailable_reason) from exc
    except (jwt.PyJWTError, TypeError, ValueError, OverflowError, KeyError) as exc:
        _invalidate_jwks_cache()
        raise AppleUnavailable(unavailable_reason) from exc
    except Exception as exc:
        _invalidate_jwks_cache()
        raise AppleUnavailable(unavailable_reason) from exc
    if not signing_keys:
        _invalidate_jwks_cache()
        raise AppleUnavailable(unavailable_reason)
    return signing_keys


def _signing_key_for_kid(kid: str):
    # Serialize the complete cached lookup -> refresh -> negative-cache
    # decision. Otherwise two simultaneous rotation requests can race so one
    # thread negatively caches a kid that the other just fetched successfully.
    global _last_forced_jwks_refresh_at

    with _unknown_kids_lock:
        if _negative_kid_is_live(kid):
            raise AppleInvalidGrant("unknown_kid_cached")

        signing_keys = _get_signing_keys(
            refresh=False,
            unavailable_reason="jwks_fetch_failed",
        )
        for signing_key in signing_keys:
            if _safe_string_compare(signing_key.key_id, kid):
                return signing_key.key

        # Apple pre-publishes rotated keys, so one forced refresh per process
        # per minute handles normal rotation without letting novel attacker
        # kids serialize every SIWA request behind live HTTPS under this lock.
        now = time.monotonic()
        if (
            _last_forced_jwks_refresh_at is not None
            and now - _last_forced_jwks_refresh_at < FORCED_JWKS_REFRESH_COOLDOWN_SECONDS
        ):
            _negative_cache_kid(kid)
            raise AppleInvalidGrant("unknown_kid")
        _last_forced_jwks_refresh_at = now

        # A successful, nonempty refresh that still lacks the key is eligible
        # for negative caching. Malformed and unavailable sets clear poisoned
        # JWKS cache entries; the process-wide cooldown still bounds retries.
        refreshed_keys = _get_signing_keys(
            refresh=True,
            unavailable_reason="jwks_refresh_failed",
        )
        for signing_key in refreshed_keys:
            if _safe_string_compare(signing_key.key_id, kid):
                return signing_key.key

        _negative_cache_kid(kid)
        raise AppleInvalidGrant("unknown_kid")


def _normalise_email(claim) -> str:
    if not isinstance(claim, str):
        return ""
    email = claim.strip().lower()
    if len(email) > 254:
        return ""
    try:
        email.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    try:
        validate_email(email)
    except ValidationError:
        return ""
    return email


def verify_apple_id_token(
    id_token: str,
    nonce_hash: str,
    allowed_audiences: set[str],
) -> AppleGrant:
    allowed_audiences = {audience for audience in allowed_audiences if isinstance(audience, str) and audience}
    if not allowed_audiences:
        raise AppleInvalidGrant("invalid_issuer_or_audience")

    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise AppleInvalidGrant("invalid_token_header") from exc

    kid = header.get("kid")
    if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
        raise AppleInvalidGrant("invalid_token_header")
    signing_key = _signing_key_for_kid(kid)

    try:
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=["RS256"],
            options={
                "require": ["iss", "aud", "exp", "sub", "nonce"],
                "verify_aud": False,
                "verify_iss": False,
            },
        )
    except (jwt.PyJWTError, TypeError, ValueError, OverflowError) as exc:
        raise AppleInvalidGrant("invalid_id_token") from exc

    audience = claims.get("aud")
    audience_allowed = False
    for allowed in allowed_audiences:
        audience_allowed |= _safe_string_compare(audience, allowed)
    if not _safe_string_compare(claims.get("iss"), APPLE_ISSUER) or not audience_allowed:
        raise AppleInvalidGrant("invalid_issuer_or_audience")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
        raise AppleInvalidGrant("invalid_subject")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str):
        raise AppleInvalidGrant("invalid_nonce")
    try:
        subject.encode("utf-8")
        nonce_bytes = nonce.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AppleInvalidGrant("invalid_subject_or_nonce") from exc
    computed_nonce_hash = hashlib.sha256(nonce_bytes).hexdigest()
    if not hmac.compare_digest(computed_nonce_hash, nonce_hash):
        raise AppleInvalidGrant("nonce_mismatch")

    email = _normalise_email(claims.get("email"))
    email_verified_claim = claims.get("email_verified")
    email_verified = email_verified_claim is True or email_verified_claim == "true"
    return AppleGrant(
        subject=subject,
        issuer=APPLE_ISSUER,
        audience=audience,
        email=email,
        email_verified=email_verified,
        email_is_relay=email.endswith("@privaterelay.appleid.com"),
        refresh_token=None,
    )


def raw_exchange_apple_code(
    code: str,
    client_id: str,
    *,
    include_redirect_uri: bool,
) -> AppleRawExchange:
    """Exchange a code without trusting any claim in the returned ID token."""

    data = {
        "client_id": client_id,
        "client_secret": generate_apple_client_secret(client_id),
        "code": code,
        "grant_type": "authorization_code",
    }
    if include_redirect_uri:
        data["redirect_uri"] = settings.APPLE_SIWA_REDIRECT_URI
    try:
        response = httpx.post(
            APPLE_TOKEN_URL,
            data=data,
            timeout=APPLE_HTTP_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, OSError) as exc:
        raise AppleUnavailable("token_request_failed") from exc

    if response.status_code >= 500:
        raise AppleUnavailable("token_server_error")
    if 400 <= response.status_code < 500:
        raise AppleInvalidGrant("token_rejected")
    if response.status_code != 200:
        raise AppleUnavailable("token_unexpected_status")

    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise AppleUnavailable("token_malformed_json") from exc
    if not isinstance(body, dict):
        raise AppleUnavailable("token_malformed_json")

    refresh_token = body.get("refresh_token")
    if refresh_token is not None and (not isinstance(refresh_token, str) or not refresh_token):
        raise AppleInvalidGrant("invalid_refresh_token")
    if refresh_token is not None:
        try:
            refresh_token.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AppleInvalidGrant("invalid_refresh_token") from exc

    return AppleRawExchange(
        id_token=body.get("id_token"),
        refresh_token=refresh_token,
    )


def verify_apple_exchange(
    raw_exchange: AppleRawExchange,
    nonce_hash: str,
    audience: str,
) -> AppleGrant:
    """Verify an exchanged ID token against one exact audience and nonce."""

    if not isinstance(raw_exchange.id_token, str) or not raw_exchange.id_token:
        raise AppleInvalidGrant("missing_id_token")
    verified = verify_apple_id_token(
        raw_exchange.id_token,
        nonce_hash,
        {audience},
    )
    return AppleGrant(
        subject=verified.subject,
        issuer=verified.issuer,
        audience=verified.audience,
        email=verified.email,
        email_verified=verified.email_verified,
        email_is_relay=verified.email_is_relay,
        refresh_token=raw_exchange.refresh_token,
    )


def revoke_apple_refresh_token(
    refresh_token: str,
    *,
    client_id: str | None = None,
) -> int:
    """POST one refresh token and return only terminal-success statuses."""

    client_id = client_id or settings.APPLE_SIWA_SERVICES_ID
    try:
        response = httpx.post(
            APPLE_REVOKE_URL,
            data={
                "client_id": client_id,
                "client_secret": generate_apple_client_secret(client_id),
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            },
            timeout=APPLE_HTTP_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, OSError) as exc:
        raise AppleUnavailable("revoke_request_failed") from exc
    if response.status_code == 200:
        return response.status_code
    if 400 <= response.status_code < 500:
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise AppleUnavailable("revoke_rejected") from exc
        error = body.get("error") if isinstance(body, dict) else None
        if error == "invalid_token":
            return response.status_code
        if error == "invalid_client":
            raise AppleUnavailable("revoke_invalid_client")
        if error == "invalid_request":
            raise AppleUnavailable("revoke_invalid_request")
        raise AppleUnavailable("revoke_rejected")
    if response.status_code >= 500:
        raise AppleUnavailable("revoke_server_error")
    raise AppleUnavailable("revoke_unexpected_status")
