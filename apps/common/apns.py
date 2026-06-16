"""Token-based (JWT / .p8) Apple Push Notification client.

The cross-cutting "notify on fire-and-forget completion" gap from
``HER_SIRI_ARCHITECTURE.md``: when a Siri-escalated (Tier-3) or backgrounded
turn finishes, push an alert so the user doesn't have to reopen the app for the
reply to surface.

Fully gated and fail-soft:

* ``apns_configured()`` is False unless ``APNS_AUTH_KEY`` / ``APNS_KEY_ID`` /
  ``APNS_TEAM_ID`` / ``APNS_BUNDLE_ID`` are all set — until an operator
  provisions the .p8 auth key, every call is a logged no-op.
* APNs speaks HTTP/2 only. ``httpx`` is present but HTTP/2 needs the ``h2``
  package (``httpx[http2]``). If it's missing, ``send_push`` returns
  ``{"skipped": "http2_unavailable"}`` instead of raising — the feature stays
  dormant rather than breaking a reply.

So this ships safely now and lights up once the dep + key land. Nothing here
ever raises into a caller; ``notify_app_reply_ready`` (apps.router.push) wraps
it fail-open besides.
"""

from __future__ import annotations

import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

# Provider JWTs are valid up to 1h; Apple rejects tokens older than that and
# rate-limits regeneration. Refresh well inside the window.
_JWT_TTL_SECONDS = 3000  # 50 min

# Module-level cache of the signed provider JWT. {"token": str, "iat": int}.
_jwt_cache: dict = {"token": None, "iat": 0}


def apns_configured() -> bool:
    """True only when every credential needed to sign + address a push is set."""
    return bool(
        getattr(settings, "APNS_AUTH_KEY", "")
        and getattr(settings, "APNS_KEY_ID", "")
        and getattr(settings, "APNS_TEAM_ID", "")
        and getattr(settings, "APNS_BUNDLE_ID", "")
    )


def _apns_host(sandbox: bool) -> str:
    return "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"


def _provider_jwt(now: int) -> str:
    """Signed ES256 provider token (cached, refreshed before expiry)."""
    cached = _jwt_cache.get("token")
    if cached and (now - _jwt_cache.get("iat", 0)) < _JWT_TTL_SECONDS:
        return cached
    import jwt as pyjwt

    token = pyjwt.encode(
        {"iss": settings.APNS_TEAM_ID, "iat": now},
        settings.APNS_AUTH_KEY,
        algorithm="ES256",
        headers={"kid": settings.APNS_KEY_ID, "alg": "ES256"},
    )
    _jwt_cache["token"] = token
    _jwt_cache["iat"] = now
    return token


def _http2_client(sandbox: bool):
    """An HTTP/2 httpx client for the chosen APNs host, or None if HTTP/2 (the
    ``h2`` package) is absent."""
    import httpx

    try:
        return httpx.Client(http2=True, base_url=f"https://{_apns_host(sandbox)}", timeout=10.0)
    except Exception:  # noqa: BLE001 — h2 missing surfaces as a runtime error
        return None


def send_push(
    tokens,
    *,
    title: str,
    body: str,
    sandbox: bool | None = None,
    thread_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Send one alert to each device token on the matching APNs host. Never raises.

    ``sandbox`` picks the host (a token from a Debug build is only valid on the
    sandbox host, a TestFlight/App Store token only on production). Callers should
    pass it per the device's stored ``environment``; when omitted it falls back to
    the ``APNS_USE_SANDBOX`` global. All tokens in one call MUST share an
    environment — group by environment before calling.

    Returns ``{"sent", "failed", "unregistered": [tokens], "skipped": reason|None}``.
    ``unregistered`` carries tokens APNs rejected as stale (410 / BadDeviceToken)
    so the caller can prune them.
    """
    result: dict = {"sent": 0, "failed": 0, "unregistered": [], "skipped": None}
    tokens = [t for t in (tokens or []) if t]
    if not tokens:
        result["skipped"] = "no_tokens"
        return result
    if not apns_configured():
        result["skipped"] = "not_configured"
        return result
    if sandbox is None:
        sandbox = bool(getattr(settings, "APNS_USE_SANDBOX", False))

    try:
        import httpx  # noqa: F401
    except ImportError:
        result["skipped"] = "httpx_missing"
        return result

    client = _http2_client(sandbox)
    if client is None:
        logger.warning("apns: HTTP/2 unavailable (install httpx[http2]); push skipped")
        result["skipped"] = "http2_unavailable"
        return result

    now = int(time.time())
    try:
        jwt_token = _provider_jwt(now)
    except Exception:
        logger.exception("apns: failed to sign provider JWT")
        result["skipped"] = "jwt_error"
        client.close()
        return result

    aps: dict = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
    if thread_id:
        aps["aps"]["thread-id"] = str(thread_id)
        aps["thread_id"] = str(thread_id)
    if extra:
        aps.update(extra)

    headers = {
        "authorization": f"bearer {jwt_token}",
        "apns-topic": settings.APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }

    try:
        with client:
            for tok in tokens:
                try:
                    resp = client.post(f"/3/device/{tok}", json=aps, headers=headers)
                except Exception:
                    logger.warning("apns: transport error sending to a device", exc_info=True)
                    result["failed"] += 1
                    continue
                if resp.status_code == 200:
                    result["sent"] += 1
                elif resp.status_code == 410 or (resp.status_code == 400 and "BadDeviceToken" in resp.text):
                    result["unregistered"].append(tok)
                    result["failed"] += 1
                else:
                    logger.warning("apns: push rejected (%s): %s", resp.status_code, resp.text[:200])
                    result["failed"] += 1
    except Exception:
        logger.exception("apns: unexpected send failure")
    return result
