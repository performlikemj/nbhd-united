"""Telegram callback handlers for lesson approval actions."""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

from apps.lessons.models import Lesson
from apps.lessons.services import process_approved_lesson
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)


TELEGRAM_API_BASE = "https://api.telegram.org/bot"
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 5


def _answer_callback(callback_id: str, text: str) -> JsonResponse:
    """Build Telegram callback query answer payload."""
    return JsonResponse(
        {
            "method": "answerCallbackQuery",
            "callback_query_id": callback_id,
            "text": text,
        },
    )


def _edit_message_text(chat_id: int, message_id: int, text: str) -> None:
    """Call Telegram editMessageText API to update a callback source message."""
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN is not configured; cannot edit message")
        return

    url = f"{TELEGRAM_API_BASE}{token}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "reply_markup": {"inline_keyboard": []},
    }

    try:
        resp = requests.post(url, json=payload, timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except Exception:
        logger.exception("Failed to edit Telegram message chat_id=%s message_id=%s", chat_id, message_id)


def _edit_and_answer(callback_id: str, chat_id: int, message_id: int, new_text: str, answer_text: str) -> JsonResponse:
    """Edit message then return a callback acknowledgement payload."""
    _edit_message_text(chat_id, message_id, new_text)
    return _answer_callback(callback_id, answer_text)


def handle_lesson_callback(update: dict, tenant: Tenant) -> JsonResponse:
    """Handle inline button presses for lesson approval."""
    callback_query = update["callback_query"]
    callback_data = callback_query["data"]
    callback_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]

    parts = callback_data.split(":")
    if len(parts) != 3:
        return _answer_callback(callback_id, "Invalid action")

    action = parts[1]

    try:
        lesson_id = int(parts[2])
    except (TypeError, ValueError):
        return _answer_callback(callback_id, "Invalid lesson id")

    lesson = Lesson.objects.filter(id=lesson_id, tenant=tenant, status="pending").first()
    if not lesson:
        return _answer_callback(callback_id, "Lesson not found or already processed")

    if action == "approve":
        lesson.status = "approved"
        lesson.approved_at = timezone.now()
        lesson.save(update_fields=["status", "approved_at"])

        # Process embedding (don't block the response)
        try:
            process_approved_lesson(lesson)
        except Exception:
            logger.exception("Failed to process approved lesson %s", lesson_id)

        return _edit_and_answer(
            callback_id,
            chat_id,
            message_id,
            f"✅ Approved: {lesson.text[:100]}",
            "Added to your learning graph!",
        )

    if action == "dismiss":
        lesson.status = "dismissed"
        lesson.save(update_fields=["status"])
        return _edit_and_answer(
            callback_id,
            chat_id,
            message_id,
            f"❌ Dismissed: {lesson.text[:100]}",
            "Dismissed",
        )

    return _answer_callback(callback_id, "Unknown action")
