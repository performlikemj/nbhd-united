"""Telegram message router — forwards messages to correct OpenClaw instance."""

from __future__ import annotations

import asyncio
import logging
import random
import zoneinfo
from collections import deque
from datetime import datetime
from time import monotonic
from typing import Any

import httpx
from django.conf import settings

from apps.tenants.models import Tenant, User

logger = logging.getLogger(__name__)

_UTC = zoneinfo.ZoneInfo("UTC")


def build_datetime_context(user_timezone: str) -> str:
    """Build a timestamp header for interactive messages.

    Returns a single-line prefix like:
        [Now: 2026-04-21 06:47 JST (Monday)]\n

    Injected before every user message so the agent always knows the current
    time without needing to call a tool.
    """
    tz_str = user_timezone or "UTC"
    try:
        tz = zoneinfo.ZoneInfo(tz_str)
    except (KeyError, Exception):
        tz = _UTC
        tz_str = "UTC"
    now = datetime.now(tz)
    abbrev = now.strftime("%Z") or tz_str
    return f"[Now: {now.strftime('%Y-%m-%d %H:%M')} {abbrev} ({now.strftime('%A')})]\n"


# Lightweight "this is a user chat, not a cron job" marker. AGENTS.md treats
# this as a signal to skip the heavy session-start context-load (daily note,
# journal_context, channel-formatting docs) which is appropriate for cron
# turns where loading context IS the work, but pure overhead for a "hi how
# are you" style message. Cron prompts go through `_CRON_CONTEXT_PREAMBLE`
# in `apps/orchestrator/config_generator.py` which keeps the full
# load-everything directive — only conversational turns get this marker.
_CHAT_CONTEXT_MARKER = (
    "[chat: user is mid-conversation, reply concisely without loading "
    "workspace docs unless the question explicitly requires it]\n"
)


def build_chat_context_marker() -> str:
    """Single-line marker injected before ad-hoc user messages.

    Tells the agent the turn is a conversational message (LINE/Telegram chat),
    not a scheduled-task run, so it can skip the AGENTS.md "Session Start"
    silent context-load. The agent still has SOUL/USER/MEMORY/IDENTITY/TOOLS
    in its context window — it just doesn't pre-fetch the daily note, journal
    history, and formatting docs before every chat.

    Significantly reduces tool-call count on first-message-of-session for
    BYO Claude tenants: observed ~377 raw output lines on cold start
    (mostly memory/journal/document tool calls before the actual reply);
    with this marker the agent only fetches context when the user's message
    actually needs it.
    """
    return _CHAT_CONTEXT_MARKER


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


def resolve_tenant_by_line_user_id(line_user_id: str) -> Tenant | None:
    """Resolve tenant by LINE user ID, including suspended/provisioning users."""
    try:
        user = User.objects.select_related("tenant").get(line_user_id=line_user_id)
        tenant = user.tenant

        if tenant.status == Tenant.Status.SUSPENDED:
            return tenant
        if tenant.status in (Tenant.Status.PENDING, Tenant.Status.PROVISIONING):
            return tenant
        if tenant.status == Tenant.Status.ACTIVE and tenant.container_fqdn:
            return tenant

        return None
    except (User.DoesNotExist, Tenant.DoesNotExist):
        return None


def resolve_tenant_by_chat_id(chat_id: int) -> Tenant | None:
    """Resolve tenant by Telegram chat_id, including suspended/provisioning users."""
    try:
        user = User.objects.select_related("tenant").get(telegram_chat_id=chat_id)
        tenant = user.tenant

        if tenant.status == Tenant.Status.SUSPENDED:
            return tenant

        if tenant.status in (Tenant.Status.PENDING, Tenant.Status.PROVISIONING):
            return tenant

        if tenant.status == Tenant.Status.ACTIVE and tenant.container_fqdn:
            return tenant

        return None
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


def get_forwarding_timeout(tenant: Tenant) -> tuple[float, bool]:
    """Return ``(timeout_seconds, is_reasoning)`` for a tenant's active model.

    Reasoning models (e.g. Kimi K2.6) get a longer timeout because their
    inference is slower.  The second element signals whether the caller
    should show a "still thinking" notice.
    """
    from apps.billing.constants import (
        DEFAULT_CHAT_TIMEOUT,
        REASONING_MODEL_TIMEOUT,
        REASONING_MODELS,
    )

    model = tenant.preferred_model or ""
    is_reasoning = model in REASONING_MODELS
    return (REASONING_MODEL_TIMEOUT if is_reasoning else DEFAULT_CHAT_TIMEOUT, is_reasoning)


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
                    container_fqdn,
                    attempt,
                    max_retries + 1,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
                continue
            logger.warning(
                "Timeout forwarding to %s after %d attempts",
                container_fqdn,
                attempt,
            )
            return None
        except httpx.HTTPError as e:
            logger.error("Error forwarding to %s: %s", container_fqdn, e)
            return None


def send_telegram_message(chat_id: int, text: str, **kwargs: Any) -> bool:
    """Send a Telegram message directly. Returns True on success.

    Supports extra Telegram API params via kwargs (e.g. reply_markup).
    """
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        logger.warning("Cannot send Telegram message: no bot token configured")
        return False

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, **kwargs},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("sendMessage failed (%s): %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception:
        logger.exception("Failed to send Telegram message to chat_id=%s", chat_id)
        return False


def send_onboarding_link(chat_id: int) -> dict:
    """Build a response telling an unregistered user to sign up."""
    frontend_url = getattr(settings, "FRONTEND_URL", "https://neighborhoodunited.org").rstrip("/")
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": (
            "👋 Welcome! I don't recognize your account yet.\n\n"
            f"Sign up at {frontend_url} to get your own "
            "AI assistant.\n\n"
            "Once subscribed, come back and send me a message!"
        ),
    }


_COLD_START_MESSAGES = [
    "Grabbing my coffee ☕ — try again in about 30 seconds!",
    "Just doing some stretches 🧘 — hit me up again in ~30 seconds!",
    "Warming up the engines 🚀 — try again in about 30 seconds!",
    "BRB, microwaving a burrito 🌯 — send that again in ~30 seconds!",
    "Sharpening my pencils ✏️ — give me about 30 seconds and try again!",
    "Doing a quick power nap 😴 — try again in about 30 seconds!",
    "Watering my plants 🌱 — send that again in ~30 seconds!",
    "Tuning my guitar 🎸 — try again in about 30 seconds!",
    "Making a smoothie 🥤 — hit me again in ~30 seconds!",
    "Tying my shoelaces 👟 — try again in about 30 seconds!",
    "Flipping pancakes 🥞 — send that again in ~30 seconds!",
    "Feeding the cat 🐱 — try again in about 30 seconds!",
    "Organizing my desk 🗂️ — give me ~30 seconds and try again!",
    "Doing jumping jacks 🏃 — try again in about 30 seconds!",
    "Toasting some bread 🍞 — send that again in ~30 seconds!",
    "Brushing up on my jokes 😄 — try again in about 30 seconds!",
    "Refilling my water bottle 💧 — hit me again in ~30 seconds!",
    "Taking a deep breath 🌬️ — try again in about 30 seconds!",
    "Putting on my thinking cap 🎩 — send that again in ~30 seconds!",
    "Rolling out of bed 🛏️ — give me about 30 seconds and try again!",
]


def send_temporary_error(chat_id: int) -> dict:
    """Build a response telling the user their agent is temporarily unreachable."""
    return {
        "method": "sendMessage",
        "chat_id": chat_id,
        "text": random.choice(_COLD_START_MESSAGES),
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
