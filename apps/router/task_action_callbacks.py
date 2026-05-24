"""Callback handlers for reconciliation ``PendingTaskAction`` undo.

Counterpart to ``apps.router.extraction_callbacks``: when the nightly
extraction reconciles the day's journal against open Tasks / active
Goals, each applied state change lands as a ``PendingTaskAction`` row
and gets a Remove/Undo button in the morning Telegram / LINE summary.
This module routes that button press back to
``apps.journal.reconciliation.undo_task_action`` and edits the channel
message accordingly.

Callback data format:  ``task_action:undo:<action_id>``
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

from apps.journal.models import PendingTaskAction
from apps.journal.reconciliation import undo_task_action
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
TELEGRAM_TIMEOUT = 5


def _answer_callback(callback_id: str, text: str) -> JsonResponse:
    return JsonResponse({"method": "answerCallbackQuery", "callback_query_id": callback_id, "text": text})


def _edit_message(chat_id: int, message_id: int, text: str) -> None:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    try:
        requests.post(
            f"{TELEGRAM_API_BASE}{token}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": {"inline_keyboard": []},
            },
            timeout=TELEGRAM_TIMEOUT,
        )
    except Exception:
        logger.exception("task_action_callbacks: failed to edit message")


def _resolve_action(tenant: Tenant, action_id: str) -> PendingTaskAction | None:
    return PendingTaskAction.objects.filter(id=action_id, tenant=tenant).first()


def _label_for(action: PendingTaskAction) -> str:
    """Short text describing the action for the channel reply."""
    if action.task_id and action.task:
        return action.task.title[:80]
    if action.goal_id and action.goal:
        return action.goal.title[:80]
    return str(action.id)[:8]


def handle_task_action_callback(update: dict, tenant: Tenant) -> JsonResponse:
    """Telegram callback-query handler for ``task_action:undo:<id>``."""
    callback_query = update["callback_query"]
    callback_data = callback_query["data"]
    callback_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]

    parts = callback_data.split(":")
    if len(parts) != 3 or parts[1] != "undo":
        return _answer_callback(callback_id, "Invalid action")

    _, _action, action_id = parts
    action = _resolve_action(tenant, action_id)
    if action is None:
        return _answer_callback(callback_id, "Not found")

    if action.status == PendingTaskAction.Status.UNDONE:
        return _answer_callback(callback_id, "Already undone")

    if action.status != PendingTaskAction.Status.APPLIED:
        return _answer_callback(callback_id, "Cannot undo")

    label = _label_for(action)

    try:
        ok = undo_task_action(action)
    except Exception:
        logger.exception("task_action_callbacks: undo failed for %s", action_id)
        return _answer_callback(callback_id, "Something went wrong — try again")

    if not ok:
        action.status = PendingTaskAction.Status.FAILED
        action.resolved_at = timezone.now()
        action.save(update_fields=["status", "resolved_at"])
        return _answer_callback(callback_id, "Couldn't undo — the row is gone")

    action.status = PendingTaskAction.Status.UNDONE
    action.resolved_at = timezone.now()
    action.save(update_fields=["status", "resolved_at"])

    _edit_message(chat_id, message_id, f"~{label}~ — undone")
    return _answer_callback(callback_id, "Undone!")


def handle_task_action_postback_line(tenant: Tenant, data: str) -> tuple[bool, str]:
    """LINE-postback adapter. Returns (ok, user_facing_message).

    LINE postbacks don't have the same edit-original-message affordance
    Telegram does, so the caller posts ``user_facing_message`` as a
    follow-up status bubble.
    """
    parts = data.split(":")
    if len(parts) != 3 or parts[1] != "undo":
        return False, "Invalid action"

    _, _action, action_id = parts
    action = _resolve_action(tenant, action_id)
    if action is None:
        return False, "Not found"

    if action.status == PendingTaskAction.Status.UNDONE:
        return True, "👍 Already undone!"

    if action.status != PendingTaskAction.Status.APPLIED:
        return False, "Cannot undo — not currently applied."

    label = _label_for(action)

    try:
        ok = undo_task_action(action)
    except Exception:
        logger.exception("task_action_callbacks: LINE undo failed for %s", action_id)
        return False, "Something went wrong — try again"

    if not ok:
        action.status = PendingTaskAction.Status.FAILED
        action.resolved_at = timezone.now()
        action.save(update_fields=["status", "resolved_at"])
        return False, "Couldn't undo — the row is gone"

    action.status = PendingTaskAction.Status.UNDONE
    action.resolved_at = timezone.now()
    action.save(update_fields=["status", "resolved_at"])

    return True, f"Undone: {label}"
