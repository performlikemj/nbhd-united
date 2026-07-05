"""Platform-agnostic wave (friend-request) notifications.

Mirrors :mod:`apps.lessons.notifications`: when a wave arrives, send the
ADDRESSEE inline accept/decline buttons on their preferred channel (Telegram or
LINE), reusing the exact ``friend:<action>:<id>`` callback-data contract the
router callbacks parse. Fully defensive — a notification failure must never
break the wave that was already persisted. iOS-first users with no linked
channel simply see the wave in the console.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.tenants.models import Tenant

from .models import Friendship, NeighborProfile

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _waver_name(friendship: Friendship) -> str:
    profile = NeighborProfile.objects.filter(tenant=friendship.requester_id).first()
    if profile is not None:
        return profile.display_name
    return getattr(friendship.requester.user, "display_name", None) or "A neighbor"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def _send_telegram_wave(tenant: Tenant, friendship: Friendship) -> bool:
    import httpx

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        return False
    chat_id = tenant.user.telegram_chat_id
    if not chat_id:
        return False

    name = _waver_name(friendship)
    note = (friendship.invite_note or "").strip()
    text = f"\U0001f44b <b>{name}</b> waved — want to be neighbors?"
    if note:
        text += f"\n\n“{note}”"
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "\U0001f44b Wave back", "callback_data": f"friend:accept:{friendship.id}"},
                {"text": "✕ Not now", "callback_data": f"friend:decline:{friendship.id}"},
            ]
        ]
    }
    try:
        resp = httpx.post(
            f"{TELEGRAM_API_BASE}{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": keyboard},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        resp2 = httpx.post(
            f"{TELEGRAM_API_BASE}{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{name} waved — want to be neighbors?", "reply_markup": keyboard},
            timeout=10,
        )
        return resp2.status_code == 200
    except Exception:
        logger.exception("Failed to send wave notification via Telegram for tenant %s", tenant.id)
        return False


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------


def _send_line_wave(tenant: Tenant, friendship: Friendship) -> bool:
    import httpx

    channel_token = getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    if not channel_token:
        return False
    line_user_id = tenant.user.line_user_id
    if not line_user_id:
        return False

    name = _waver_name(friendship)
    note = (friendship.invite_note or "").strip()
    contents = [{"type": "text", "text": f"\U0001f44b {name} waved", "weight": "bold", "size": "md"}]
    if note:
        contents.append({"type": "text", "text": note, "wrap": True, "margin": "md"})
    contents.append({"type": "text", "text": "Want to be neighbors?", "wrap": True, "margin": "md"})
    flex_content = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "contents": contents},
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#0D9488",
                    "action": {
                        "type": "postback",
                        "label": "\U0001f44b Wave back",
                        "data": f"friend:accept:{friendship.id}",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": "✕ Not now",
                        "data": f"friend:decline:{friendship.id}",
                    },
                },
            ],
        },
    }
    try:
        resp = httpx.post(
            LINE_PUSH_URL,
            json={
                "to": line_user_id,
                "messages": [
                    {"type": "flex", "altText": f"{name} waved — want to be neighbors?", "contents": flex_content}
                ],
            },
            headers={"Authorization": f"Bearer {channel_token}", "Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("LINE wave push failed (%s): %s", resp.status_code, resp.text[:300])
        return resp.status_code == 200
    except Exception:
        logger.exception("Failed to send wave notification via LINE for tenant %s", tenant.id)
        return False


# ---------------------------------------------------------------------------
# Platform dispatcher
# ---------------------------------------------------------------------------

_SENDERS = {"telegram": _send_telegram_wave, "line": _send_line_wave}


def notify_wave_received(friendship: Friendship) -> bool:
    """Notify the addressee of a pending wave on their preferred channel, with
    fallback. Returns True if any channel accepted it. Never raises."""
    try:
        addressee = friendship.addressee
        if friendship.status != Friendship.Status.PENDING:
            return False
        preferred = getattr(addressee.user, "preferred_channel", "") or "telegram"
        fallback = "line" if preferred == "telegram" else "telegram"
        for channel in (preferred, fallback):
            sender = _SENDERS.get(channel)
            if sender and sender(addressee, friendship):
                return True
        return False
    except Exception:
        logger.exception("notify_wave_received failed for friendship %s", getattr(friendship, "id", "?"))
        return False
