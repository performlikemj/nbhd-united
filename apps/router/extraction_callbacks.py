"""Telegram callback handlers for PendingExtraction approval actions.

Callback data format:  extract:<action>:<pending_id>

Actions:
  approve_lesson  — create Lesson record, mark approved
  approve_goal    — append to goals Document, mark approved
  approve_task    — append to tasks Document, mark approved
  undo_lesson     — delete Lesson, mark undone
  undo_goal       — remove goal from Document, mark undone
  undo_task       — remove task from Document, mark undone
  dismiss         — mark dismissed (suppresses re-extraction for 30 days)
"""

from __future__ import annotations

import logging
import re
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

def _approve_lesson(pending: PendingExtraction) -> tuple[str, str | None]:
    """Create a Lesson from the pending extraction and process its embedding.

    Returns (user_message, lesson_id_str).
    """
    context = f"Extracted from daily note — {pending.source_date.isoformat() if pending.source_date else 'recent entries'}"
    lesson = Lesson.objects.create(
        tenant=pending.tenant,
        text=pending.text,
        context=context,
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

    return "Added to your learning constellation! ✨", lesson.id


def _approve_goal(pending: PendingExtraction) -> tuple[str, None]:
    """Append goal to the tenant's goals Document (create if missing).

    Returns (user_message, None).
    """
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
    return "Added to your goals! 🎯", None


def _approve_task(pending: PendingExtraction) -> tuple[str, None]:
    """Append task to the tenant's tasks Document (create if missing).

    Returns (user_message, None).
    """
    doc, _ = Document.objects.get_or_create(
        tenant=pending.tenant,
        kind=Document.Kind.TASKS,
        slug="tasks",
        defaults={"title": "Tasks", "markdown": "# Tasks\n\n"},
    )
    today = date.today().isoformat()
    doc.markdown += f"- [ ] {pending.text}  _(added {today})_\n"
    doc.save(update_fields=["markdown", "updated_at"])
    return "Added to your tasks! ✅", None


# ── Undo actions ─────────────────────────────────────────────────────────────

def _undo_lesson(pending: PendingExtraction) -> None:
    """Delete the Lesson created by this extraction."""
    if pending.lesson_id:
        Lesson.objects.filter(id=pending.lesson_id, tenant=pending.tenant).delete()


def _undo_goal(pending: PendingExtraction) -> None:
    """Remove the goal block from the tenant's goals Document."""
    doc = Document.objects.filter(
        tenant=pending.tenant, kind=Document.Kind.GOAL, slug="goals"
    ).first()
    if doc:
        pattern = r"\n" + re.escape(f"### {pending.text}") + r"\n- Added: \d{4}-\d{2}-\d{2}\n- Status: active\n"
        doc.markdown = re.sub(pattern, "", doc.markdown)
        doc.save(update_fields=["markdown", "updated_at"])


def _undo_task(pending: PendingExtraction) -> None:
    """Remove the task line from the tenant's tasks Document."""
    doc = Document.objects.filter(
        tenant=pending.tenant, kind=Document.Kind.TASKS, slug="tasks"
    ).first()
    if doc:
        pattern = re.escape(f"- [ ] {pending.text}") + r"  _\(added \d{4}-\d{2}-\d{2}\)_\n"
        doc.markdown = re.sub(pattern, "", doc.markdown)
        doc.save(update_fields=["markdown", "updated_at"])


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

    # Undo actions operate on APPROVED items
    is_undo = action in ("undo_lesson", "undo_goal", "undo_task")

    if is_undo:
        pending = PendingExtraction.objects.filter(
            id=pending_id,
            tenant=tenant,
            status=PendingExtraction.Status.APPROVED,
        ).first()

        if not pending:
            # May already be undone
            already = PendingExtraction.objects.filter(
                id=pending_id, tenant=tenant, status=PendingExtraction.Status.UNDONE,
            ).exists()
            if already:
                return _answer_callback(callback_id, "Already removed")
            return _answer_callback(callback_id, "Not found")

        try:
            if action == "undo_lesson":
                _undo_lesson(pending)
            elif action == "undo_goal":
                _undo_goal(pending)
            elif action == "undo_task":
                _undo_task(pending)
        except Exception:
            logger.exception("extraction_callbacks: undo failed for %s", pending_id)
            return _answer_callback(callback_id, "Something went wrong — try again")

        pending.status = PendingExtraction.Status.UNDONE
        pending.resolved_at = timezone.now()
        pending.save(update_fields=["status", "resolved_at"])

        _edit_message(chat_id, message_id, f"~{pending.text[:80]}~ — removed")
        return _answer_callback(callback_id, "Removed!")

    # Approve/dismiss actions operate on PENDING items
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
            answer, lesson_id = _approve_lesson(pending)
        elif action == "approve_goal":
            answer, _ = _approve_goal(pending)
        elif action == "approve_task":
            answer, _ = _approve_task(pending)
        else:
            return _answer_callback(callback_id, "Unknown action")
    except Exception:
        logger.exception("extraction_callbacks: approval failed for %s", pending_id)
        return _answer_callback(callback_id, "Something went wrong — try again")

    pending.status = PendingExtraction.Status.APPROVED
    pending.resolved_at = timezone.now()
    if action == "approve_lesson" and lesson_id:
        pending.lesson_id = lesson_id
        pending.save(update_fields=["status", "resolved_at", "lesson_id"])
    else:
        pending.save(update_fields=["status", "resolved_at"])

    kind_emoji = {"lesson": "💡", "goal": "🎯", "task": "✅"}.get(pending.kind, "✅")
    _edit_message(chat_id, message_id, f"{kind_emoji} {pending.text[:80]}")
    return _answer_callback(callback_id, answer)
