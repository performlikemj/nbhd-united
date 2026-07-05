"""Telegram callback handlers for wave (friend-request) accept/decline buttons.

Mirrors :mod:`apps.router.lesson_callbacks`. Callback-data contract:
``friend:accept:<friendship_uuid>`` / ``friend:decline:<friendship_uuid>``. The
button recipient is always the wave's ADDRESSEE, so accept/decline are valid;
the service layer re-checks party + state, making double-taps idempotent.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.http import JsonResponse

from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 5


def _answer_callback(callback_id: str, text: str) -> JsonResponse:
    return JsonResponse({"method": "answerCallbackQuery", "callback_query_id": callback_id, "text": text})


def _edit_message_text(chat_id: int, message_id: int, text: str) -> None:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    try:
        resp = requests.post(
            f"{TELEGRAM_API_BASE}{token}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": {"inline_keyboard": []}},
            timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to edit Telegram message chat_id=%s message_id=%s", chat_id, message_id)


def _edit_and_answer(callback_id, chat_id, message_id, new_text, answer_text) -> JsonResponse:
    _edit_message_text(chat_id, message_id, new_text)
    return _answer_callback(callback_id, answer_text)


def _waver_name(edge) -> str:
    from apps.friends.models import NeighborProfile

    profile = NeighborProfile.objects.filter(tenant=edge.requester_id).first()
    if profile is not None:
        return profile.display_name
    return getattr(edge.requester.user, "display_name", None) or "your neighbor"


def handle_friend_callback(update: dict, tenant: Tenant) -> JsonResponse:
    """Handle a wave accept/decline inline-button press."""
    from apps.friends import services
    from apps.friends.models import Friendship

    callback_query = update["callback_query"]
    callback_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]

    parts = callback_query["data"].split(":")
    if len(parts) != 3:
        return _answer_callback(callback_id, "Invalid action")
    action, friendship_id = parts[1], parts[2]

    try:
        edge = Friendship.objects.filter(id=friendship_id).first()
    except (ValueError, TypeError):
        edge = None
    if edge is None or edge.addressee_id != tenant.id:
        return _answer_callback(callback_id, "This wave is no longer available.")

    name = _waver_name(edge)
    try:
        services.respond_to_wave(tenant, edge.id, action)
    except Exception:  # noqa: BLE001 — service raises DRF exceptions; answer gracefully
        return _answer_callback(callback_id, "That wave was already answered.")

    if action == "accept":
        return _edit_and_answer(
            callback_id, chat_id, message_id, f"\U0001f44b You and {name} are neighbors now.", "You're neighbors!"
        )
    if action == "decline":
        return _edit_and_answer(callback_id, chat_id, message_id, f"✕ Declined {name}'s wave.", "Declined")
    return _answer_callback(callback_id, "Unknown action")


def handle_friend_line_postback(tenant: Tenant, data: str) -> str:
    """LINE has no message-edit API — resolve the wave and return a short
    confirmation string the caller pushes as a follow-up. Returns "" on no-op."""
    from apps.friends import services
    from apps.friends.models import Friendship

    parts = data.split(":")
    if len(parts) != 3:
        return ""
    action, friendship_id = parts[1], parts[2]
    try:
        edge = Friendship.objects.filter(id=friendship_id).first()
    except (ValueError, TypeError):
        edge = None
    if edge is None or edge.addressee_id != tenant.id:
        return "That wave is no longer available."
    name = _waver_name(edge)
    try:
        services.respond_to_wave(tenant, edge.id, action)
    except Exception:  # noqa: BLE001
        return "That wave was already answered."
    if action == "accept":
        return f"\U0001f44b You and {name} are neighbors now."
    if action == "decline":
        return f"Declined {name}'s wave."
    return ""
