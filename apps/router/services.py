"""Telegram message router — forwards messages to correct OpenClaw instance."""
from __future__ import annotations

from collections import deque
import logging
from time import monotonic

import httpx
from django.conf import settings

from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)

# In-memory cache: chat_id → container_fqdn
_route_cache: dict[int, str] = {}
_rate_limit_state: dict[int, deque[float]] = {}


def resolve_container(chat_id: int) -> str | None:
    """Look up the OpenClaw container FQDN for a chat_id.

    Uses in-memory cache backed by DB lookup.
    """
    if chat_id in _route_cache:
        return _route_cache[chat_id]

    try:
        user = User.objects.get(telegram_chat_id=chat_id)
        tenant = Tenant.objects.get(user=user, status=Tenant.Status.ACTIVE)
        if tenant.container_fqdn:
            _route_cache[chat_id] = tenant.container_fqdn
            return tenant.container_fqdn
    except (User.DoesNotExist, Tenant.DoesNotExist):
        pass

    return None


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


async def forward_to_openclaw(container_fqdn: str, update: dict) -> dict | None:
    """Forward a Telegram update to an OpenClaw instance's gateway.

    OpenClaw's Telegram channel plugin expects to receive webhook
    updates at its gateway. We proxy them through.
    """
    url = f"http://{container_fqdn}:18789/telegram-webhook"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=update)
            resp.raise_for_status()
            return resp.json() if resp.content else None
    except httpx.TimeoutException:
        logger.warning("Timeout forwarding to %s", container_fqdn)
        return None
    except httpx.HTTPError as e:
        logger.error("Error forwarding to %s: %s", container_fqdn, e)
        return None


def send_onboarding_link(chat_id: int) -> dict:
    """Build a response telling an unregistered user to sign up."""
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": (
            "👋 Welcome! I don't recognize your account yet.\n\n"
            "Sign up at https://nbhdunited.com to get your own "
            "AI assistant for $5/month.\n\n"
            "Once subscribed, come back and send me a message!"
        ),
    }
