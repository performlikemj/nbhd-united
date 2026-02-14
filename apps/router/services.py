"""Telegram message router — forwards messages to correct OpenClaw instance."""
from __future__ import annotations

import asyncio
from collections import deque
import logging
from time import monotonic

import httpx
from django.conf import settings

from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)

# In-memory cache: chat_id → (container_fqdn, timestamp)
ROUTE_CACHE_TTL = 60  # seconds
_route_cache: dict[int, tuple[str, float]] = {}
_rate_limit_state: dict[int, deque[float]] = {}


def resolve_container(chat_id: int) -> str | None:
    """Look up the OpenClaw container FQDN for a chat_id.

    Uses in-memory cache (with TTL) backed by DB lookup.
    """
    cached = _route_cache.get(chat_id)
    if cached is not None:
        fqdn, ts = cached
        if (monotonic() - ts) < ROUTE_CACHE_TTL:
            return fqdn
        del _route_cache[chat_id]

    try:
        user = User.objects.get(telegram_chat_id=chat_id)
        tenant = Tenant.objects.get(user=user, status=Tenant.Status.ACTIVE)
        if tenant.container_fqdn:
            _route_cache[chat_id] = (tenant.container_fqdn, monotonic())
            return tenant.container_fqdn
    except (User.DoesNotExist, Tenant.DoesNotExist):
        pass

    return None


def resolve_tenant_by_chat_id(chat_id: int) -> Tenant | None:
    """Resolve an active tenant for the chat_id."""
    try:
        user = User.objects.select_related("tenant").get(telegram_chat_id=chat_id)
        tenant = user.tenant
        if tenant.status != Tenant.Status.ACTIVE or not tenant.container_fqdn:
            return None
        return tenant
    except (User.DoesNotExist, Tenant.DoesNotExist):
        return None


def resolve_user_timezone(chat_id: int) -> str:
    """Look up user's preferred timezone; return UTC when unknown."""
    try:
        user = User.objects.get(telegram_chat_id=chat_id)
        return user.timezone or "UTC"
    except User.DoesNotExist:
        return "UTC"


def invalidate_cache(chat_id: int) -> None:
    """Remove a chat_id from the route cache."""
    _route_cache.pop(chat_id, None)


def clear_cache() -> None:
    """Clear the entire route cache."""
    _route_cache.clear()


def clear_rate_limits() -> None:
    """Clear in-memory rate-limit state."""
    _rate_limit_state.clear()


def is_rate_limited(chat_id: int) -> bool:
    """Return True if the chat has exceeded the per-minute limit."""
    limit = getattr(settings, "ROUTER_RATE_LIMIT_PER_MINUTE", 30)
    if limit <= 0:
        return False

    now = monotonic()
    window_seconds = 60.0
    chat_hits = _rate_limit_state.setdefault(chat_id, deque())
    while chat_hits and (now - chat_hits[0]) > window_seconds:
        chat_hits.popleft()

    if len(chat_hits) >= limit:
        return True

    chat_hits.append(now)
    return False


def extract_chat_id(update: dict) -> int | None:
    """Extract chat_id from a Telegram update object."""
    # Try message, then callback_query, then edited_message
    for key in ("message", "callback_query", "edited_message"):
        obj = update.get(key)
        if obj:
            chat = obj.get("chat") or (obj.get("message", {}).get("chat") if key == "callback_query" else None)
            if chat:
                return chat.get("id")
    return None


async def forward_to_openclaw(
    container_fqdn: str,
    update: dict,
    *,
    user_timezone: str = "UTC",
    timeout: float = 10.0,
    max_retries: int = 0,
    retry_delay: float = 5.0,
) -> dict | None:
    """Forward a Telegram update to an OpenClaw instance's gateway.

    Uses a short timeout (10s) to stay well within gunicorn's 120s limit.
    When Azure Container Apps scales from zero, the initial request triggers
    the scale-up even if it times out. The user is told to retry in ~30s,
    by which time the container is warm.
    """
    url = f"https://{container_fqdn}/telegram-webhook"

    attempt = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    url,
                    json=update,
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token": settings.TELEGRAM_WEBHOOK_SECRET,
                        "X-User-Timezone": user_timezone,
                    },
                )
                resp.raise_for_status()
                return resp.json() if resp.content else None
        except httpx.TimeoutException:
            attempt += 1
            if attempt <= max_retries:
                logger.info(
                    "Timeout forwarding to %s (attempt %d/%d), retrying in %.0fs",
                    container_fqdn, attempt, max_retries + 1, retry_delay,
                )
                await asyncio.sleep(retry_delay)
                continue
            logger.warning(
                "Timeout forwarding to %s after %d attempts", container_fqdn, attempt,
            )
            return None
        except httpx.HTTPError as e:
            logger.error("Error forwarding to %s: %s", container_fqdn, e)
            return None


def send_onboarding_link(chat_id: int) -> dict:
    """Build a response telling an unregistered user to sign up."""
    frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": (
            "👋 Welcome! I don't recognize your account yet.\n\n"
            f"Sign up at {frontend_url} to get your own "
            "AI assistant for $5/month.\n\n"
            "Once subscribed, come back and send me a message!"
        ),
    }


def send_temporary_error(chat_id: int) -> dict:
    """Build a response telling the user their agent is temporarily unreachable."""
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": (
            "Grabbing my coffee \u2615 \u2014 send that again in about "
            "30 seconds and I'll be ready!"
        ),
    }


def handle_start_command(update: dict) -> dict | None:
    """Handle /start TOKEN for account linking.

    Returns a Telegram sendMessage response dict, or None if not a /start command.
    """
    message = update.get("message", {})
    text = message.get("text", "")

    if not text.startswith("/start "):
        return None

    token = text.split(" ", 1)[1].strip()
    if not token:
        return None

    from_user = message.get("from", {})
    chat_id = message.get("chat", {}).get("id")
    telegram_user_id = from_user.get("id")

    if not chat_id or not telegram_user_id:
        return None

    from apps.tenants.telegram_service import process_start_token

    success, reply = process_start_token(
        telegram_user_id=telegram_user_id,
        telegram_chat_id=chat_id,
        telegram_username=from_user.get("username", ""),
        telegram_first_name=from_user.get("first_name", ""),
        token=token,
    )

    if success:
        # Invalidate route cache so subsequent messages get routed
        invalidate_cache(chat_id)

    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": reply,
    }
