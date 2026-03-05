"""Telegram callback handlers for PendingExtraction approval actions.

Callback data format:  extract:<action>:<pending_id>

Actions:
  approve_lesson  — create Lesson record, mark approved
  approve_goal    — append to goals Document, mark approved
  approve_task    — append to tasks Document, mark approved
  dismiss         — mark dismissed (suppresses re-extraction for 30 days)
"""

from __future__ import annotations

import logging
from datetime import date

import requests
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone

from apps.journal.models import Document, PendingExtraction
from apps.lessons.models import Lesson
from apps.lessons.services import process_approved_lesson
from apps.tenants.models import Tenant

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot"
TELEGRAM_TIMEOUT = 5


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _answer_callback(callback_id: str, text: str) -> JsonResponse:
    return JsonResponse({"method": "answerCallbackQuery", "callback_query_id": callback_id, "text": text})


def _edit_message(chat_id: int, message_id: int, text: str) -> None:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return
    try:
        requests.post(
            f"{TELEGRAM_API_BASE}{token}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": {"inline_keyboard": []}},
            timeout=TELEGRAM_TIMEOUT,
        )
    except Exception:
        logger.exception("extraction_callbacks: failed to edit message")


# ── Approval actions ──────────────────────────────────────────────────────────

def _approve_lesson(pending: PendingExtraction) -> str:
    """Create a Lesson from the pending extraction and process its embedding."""
    lesson = Lesson.objects.create(
        tenant=pending.tenant,
        text=pending.text,
        tags=pending.tags,
        source_type="journal",
        source_ref=str(pending.source_date or date.today()),
        status="approved",
        approved_at=timezone.now(),
    )

    # Generate embedding + connections (matches lesson_callbacks.py pattern)
    try:
        process_approved_lesson(lesson)
    except Exception:
        logger.exception("extraction_callbacks: embedding failed for lesson %s", lesson.id)

    # Re-cluster if threshold reached (matches views.py pattern)
    try:
        from apps.lessons.clustering import refresh_constellation

        if Lesson.objects.filter(tenant=pending.tenant, status="approved").count() >= 5:
            refresh_constellation(pending.tenant)
    except Exception:
        logger.exception("extraction_callbacks: clustering failed for tenant %s", str(pending.tenant.id)[:8])

    return "Added to your learning constellation! ✨"


def _approve_goal(pending: PendingExtraction) -> str:
    """Append goal to the tenant's goals Document (create if missing)."""
    doc, _ = Document.objects.get_or_create(
        tenant=pending.tenant,
        kind=Document.Kind.GOAL,
        slug="goals",
        defaults={"title": "Goals", "markdown": "# Goals\n\n## Active\n\n## Completed\n"},
    )
    today = date.today().isoformat()
    new_entry = f"\n### {pending.text}\n- Added: {today}\n- Status: active\n"

    # Insert under ## Active
    if "## Active" in doc.markdown:
        doc.markdown = doc.markdown.replace("## Active", f"## Active\n{new_entry}", 1)
    else:
        doc.markdown += f"\n## Active\n{new_entry}"

    doc.save(update_fields=["markdown", "updated_at"])
    return "Added to your goals! 🎯"


def _approve_task(pending: PendingExtraction) -> str:
    """Append task to the tenant's tasks Document (create if missing)."""
    doc, _ = Document.objects.get_or_create(
        tenant=pending.tenant,
        kind=Document.Kind.TASKS,
        slug="tasks",
        defaults={"title": "Tasks", "markdown": "# Tasks\n\n"},
    )
    today = date.today().isoformat()
    doc.markdown += f"- [ ] {pending.text}  _(added {today})_\n"
    doc.save(update_fields=["markdown", "updated_at"])
    return "Added to your tasks! ✅"


# ── Main handler ──────────────────────────────────────────────────────────────

def handle_extraction_callback(update: dict, tenant: Tenant) -> JsonResponse:
    """Handle inline button presses for PendingExtraction approval/dismissal."""
    callback_query = update["callback_query"]
    callback_data = callback_query["data"]
    callback_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]

    # Format: extract:<action>:<uuid>
    parts = callback_data.split(":")
    if len(parts) != 3:
        return _answer_callback(callback_id, "Invalid action")

    _, action, pending_id = parts

    pending = PendingExtraction.objects.filter(
        id=pending_id,
        tenant=tenant,
        status=PendingExtraction.Status.PENDING,
    ).first()

    if not pending:
        _edit_message(chat_id, message_id, "Already processed.")
        return _answer_callback(callback_id, "Already processed")

    if action == "dismiss":
        pending.status = PendingExtraction.Status.DISMISSED
        pending.resolved_at = timezone.now()
        pending.save(update_fields=["status", "resolved_at"])
        _edit_message(chat_id, message_id, f"❌ Skipped: {pending.text[:80]}")
        return _answer_callback(callback_id, "Skipped")

    try:
        if action == "approve_lesson":
            answer = _approve_lesson(pending)
        elif action == "approve_goal":
            answer = _approve_goal(pending)
        elif action == "approve_task":
            answer = _approve_task(pending)
        else:
            return _answer_callback(callback_id, "Unknown action")
    except Exception:
        logger.exception("extraction_callbacks: approval failed for %s", pending_id)
        return _answer_callback(callback_id, "Something went wrong — try again")

    pending.status = PendingExtraction.Status.APPROVED
    pending.resolved_at = timezone.now()
    pending.save(update_fields=["status", "resolved_at"])

    kind_emoji = {"lesson": "💡", "goal": "🎯", "task": "✅"}.get(pending.kind, "✅")
    _edit_message(chat_id, message_id, f"{kind_emoji} {pending.text[:80]}")
    return _answer_callback(callback_id, answer)
