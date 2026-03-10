"""Tests for nightly extraction engine and callback handler."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.journal.models import Document, DailyNote, PendingExtraction
from apps.journal.extraction import (
    run_extraction_for_tenant,
    _get_daily_note_content,
    _is_duplicate,
)
from apps.lessons.models import Lesson
from apps.tenants.models import Tenant, User


def make_tenant(tz="UTC", chat_id=12345678) -> Tenant:
    user = User.objects.create_user(username=f"u{chat_id}", password="pass")
    user.timezone = tz
    user.telegram_chat_id = chat_id
    user.save(update_fields=["timezone", "telegram_chat_id"])
    tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE)
    return tenant


RICH_NOTE = """
# Daily Note 2026-03-02

## Morning

Worked on the NBHD United platform today. Discovered that QStash retries
indefinitely on 5xx responses, so we need to always return 200 for background
tasks that might fail due to missing resources. This was causing a retry storm
on the expire-trials endpoint.

## Afternoon

Goal for this month: get the first paying subscriber on NBHD United by March 31.

## Tasks

- Create GitHub issue for the IFSI compliance requirement
- Update the resync endpoint to cover all tenant statuses
"""


class TestDailyNoteResolution(TestCase):
    def setUp(self):
        self.tenant = make_tenant()

    def test_returns_none_for_empty_note(self):
        result = _get_daily_note_content(self.tenant, date.today())
        self.assertIsNone(result)

    def test_returns_none_for_short_note(self):
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.DAILY, slug=str(date.today()),
            title="Today", markdown="Short."
        )
        result = _get_daily_note_content(self.tenant, date.today())
        self.assertIsNone(result)

    def test_returns_v2_document(self):
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.DAILY, slug=str(date.today()),
            title="Today", markdown=RICH_NOTE
        )
        result = _get_daily_note_content(self.tenant, date.today())
        self.assertIsNotNone(result)
        self.assertIn("QStash", result)

    def test_falls_back_to_legacy_daily_note(self):
        DailyNote.objects.create(tenant=self.tenant, date=date.today(), markdown=RICH_NOTE)
        result = _get_daily_note_content(self.tenant, date.today())
        self.assertIsNotNone(result)


class TestDeduplication(TestCase):
    def setUp(self):
        self.tenant = make_tenant()

    def test_no_duplicate_when_empty(self):
        self.assertFalse(_is_duplicate(self.tenant, PendingExtraction.Kind.GOAL, "Ship NBHD v2"))

    def test_detects_substring_duplicate(self):
        PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.GOAL,
            text="Get first paying subscriber on NBHD United by March 31",
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertTrue(_is_duplicate(self.tenant, PendingExtraction.Kind.GOAL, "first paying subscriber"))

    def test_dismissed_still_blocks(self):
        PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.TASK,
            text="Create GitHub issue for IFSI compliance",
            status=PendingExtraction.Status.DISMISSED,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.assertTrue(_is_duplicate(self.tenant, PendingExtraction.Kind.TASK, "GitHub issue for IFSI"))


MOCK_EXTRACTION_RESPONSE = {
    "lessons": [{"text": "QStash retries indefinitely on 5xx — always return 200 for background tasks.", "confidence": "high", "tags": ["infra"]}],
    "goals": [{"text": "Get the first paying NBHD United subscriber by March 31.", "confidence": "high"}],
    "tasks": [{"text": "Create GitHub issue for the IFSI compliance requirement.", "confidence": "high"}],
}


class TestRunExtractionForTenant(TestCase):
    def setUp(self):
        self.tenant = make_tenant(chat_id=99999)
        Document.objects.create(
            tenant=self.tenant, kind=Document.Kind.DAILY, slug=str(date.today()),
            title="Today", markdown=RICH_NOTE
        )

    @patch("apps.journal.extraction._call_extraction_llm", return_value=(MOCK_EXTRACTION_RESPONSE, {"prompt_tokens": 100, "completion_tokens": 50}))
    @patch("apps.journal.extraction._deliver_summary_telegram")
    @patch("django.conf.settings.TELEGRAM_BOT_TOKEN", "test-token", create=True)
    def test_auto_adds_items(self, mock_summary, mock_llm):
        result = run_extraction_for_tenant(self.tenant)
        self.assertEqual(result["lessons"], 1)
        self.assertEqual(result["goals"], 1)
        self.assertEqual(result["tasks"], 1)
        self.assertIsNone(result["skipped"])
        # All items should be auto-approved
        pendings = PendingExtraction.objects.filter(tenant=self.tenant)
        self.assertEqual(pendings.count(), 3)
        for p in pendings:
            self.assertEqual(p.status, PendingExtraction.Status.APPROVED)
        # Lesson should be created immediately
        self.assertTrue(Lesson.objects.filter(tenant=self.tenant).exists())
        # Goals doc should exist with content
        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals")
        self.assertIn("first paying", doc.markdown)
        # Tasks doc should exist with content
        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.TASKS, slug="tasks")
        self.assertIn("IFSI compliance", doc.markdown)

    @patch("apps.journal.extraction._call_extraction_llm", return_value=(MOCK_EXTRACTION_RESPONSE, {"prompt_tokens": 100, "completion_tokens": 50}))
    @patch("apps.journal.extraction._deliver_summary_telegram")
    @patch("django.conf.settings.TELEGRAM_BOT_TOKEN", "test-token", create=True)
    def test_sends_one_summary_message(self, mock_summary, mock_llm):
        run_extraction_for_tenant(self.tenant)
        # Should be called exactly once with all items
        mock_summary.assert_called_once()
        items = mock_summary.call_args[0][2]
        self.assertEqual(len(items), 3)

    @patch("apps.journal.extraction._call_extraction_llm", return_value=(MOCK_EXTRACTION_RESPONSE, {"prompt_tokens": 100, "completion_tokens": 50}))
    @patch("apps.journal.extraction._deliver_summary_telegram")
    @patch("django.conf.settings.TELEGRAM_BOT_TOKEN", "test-token", create=True)
    def test_deduplicates_on_second_run(self, mock_summary, mock_llm):
        run_extraction_for_tenant(self.tenant)
        result = run_extraction_for_tenant(self.tenant)
        # All 3 items should be deduped on second run
        self.assertEqual(result["lessons"], 0)
        self.assertEqual(result["goals"], 0)
        self.assertEqual(result["tasks"], 0)

    def test_skips_when_no_content(self):
        Document.objects.filter(tenant=self.tenant).delete()
        result = run_extraction_for_tenant(self.tenant)
        self.assertEqual(result["skipped"], "no_content")

    @patch("django.conf.settings.TELEGRAM_BOT_TOKEN", "test-token", create=True)
    def test_skips_when_no_channel(self):
        self.tenant.user.telegram_chat_id = None
        self.tenant.user.line_user_id = None
        self.tenant.user.save(update_fields=["telegram_chat_id", "line_user_id"])
        result = run_extraction_for_tenant(self.tenant)
        self.assertEqual(result["skipped"], "no_channel")


class TestExtractionCallbacks(TestCase):
    def setUp(self):
        self.tenant = make_tenant(chat_id=77777)
        self.pending_goal = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.GOAL,
            text="Get first paying subscriber",
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.pending_task = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.TASK,
            text="Create GitHub issue for IFSI",
            expires_at=timezone.now() + timedelta(days=7),
        )
        self.pending_lesson = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.LESSON,
            text="QStash retries on 5xx — always soft-fail background tasks.",
            expires_at=timezone.now() + timedelta(days=7),
        )

    def _make_update(self, callback_data: str, message_id: int = 42) -> dict:
        return {
            "callback_query": {
                "id": "cb123",
                "data": callback_data,
                "message": {"chat": {"id": 77777}, "message_id": message_id},
            }
        }

    @patch("apps.router.extraction_callbacks._edit_message")
    def test_approve_goal_creates_document(self, mock_edit):
        from apps.router.extraction_callbacks import handle_extraction_callback
        update = self._make_update(f"extract:approve_goal:{self.pending_goal.id}")
        handle_extraction_callback(update, self.tenant)
        self.pending_goal.refresh_from_db()
        self.assertEqual(self.pending_goal.status, PendingExtraction.Status.APPROVED)
        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals")
        self.assertIn("Get first paying subscriber", doc.markdown)

    @patch("apps.router.extraction_callbacks._edit_message")
    def test_approve_task_creates_document(self, mock_edit):
        from apps.router.extraction_callbacks import handle_extraction_callback
        update = self._make_update(f"extract:approve_task:{self.pending_task.id}")
        handle_extraction_callback(update, self.tenant)
        self.pending_task.refresh_from_db()
        self.assertEqual(self.pending_task.status, PendingExtraction.Status.APPROVED)
        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.TASKS, slug="tasks")
        self.assertIn("GitHub issue for IFSI", doc.markdown)

    @patch("apps.router.extraction_callbacks._edit_message")
    def test_approve_lesson_creates_lesson(self, mock_edit):
        from apps.router.extraction_callbacks import handle_extraction_callback
        update = self._make_update(f"extract:approve_lesson:{self.pending_lesson.id}")
        handle_extraction_callback(update, self.tenant)
        self.pending_lesson.refresh_from_db()
        self.assertEqual(self.pending_lesson.status, PendingExtraction.Status.APPROVED)
        self.assertTrue(Lesson.objects.filter(tenant=self.tenant).exists())
        # lesson_id should be stored
        self.assertIsNotNone(self.pending_lesson.lesson_id)

    @patch("apps.router.extraction_callbacks._edit_message")
    def test_dismiss(self, mock_edit):
        from apps.router.extraction_callbacks import handle_extraction_callback
        update = self._make_update(f"extract:dismiss:{self.pending_goal.id}")
        handle_extraction_callback(update, self.tenant)
        self.pending_goal.refresh_from_db()
        self.assertEqual(self.pending_goal.status, PendingExtraction.Status.DISMISSED)


class TestExtractionUndo(TestCase):
    """Test undo flow: items are auto-added (APPROVED), user taps Remove to undo."""

    def setUp(self):
        self.tenant = make_tenant(chat_id=88888)

    def _make_update(self, callback_data: str, message_id: int = 42) -> dict:
        return {
            "callback_query": {
                "id": "cb456",
                "data": callback_data,
                "message": {"chat": {"id": 88888}, "message_id": message_id},
            }
        }

    @patch("apps.router.extraction_callbacks._edit_message")
    def test_undo_lesson_deletes_lesson(self, mock_edit):
        from apps.router.extraction_callbacks import handle_extraction_callback, _approve_lesson

        pending = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.LESSON,
            text="QStash retries on 5xx — always soft-fail background tasks.",
            status=PendingExtraction.Status.APPROVED,
            resolved_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        # Simulate auto-add: create the lesson and store ID
        _, lesson_id = _approve_lesson(pending)
        pending.lesson_id = lesson_id
        pending.save(update_fields=["lesson_id"])
        self.assertTrue(Lesson.objects.filter(id=lesson_id).exists())

        # Undo
        update = self._make_update(f"extract:undo_lesson:{pending.id}")
        handle_extraction_callback(update, self.tenant)
        pending.refresh_from_db()
        self.assertEqual(pending.status, PendingExtraction.Status.UNDONE)
        self.assertFalse(Lesson.objects.filter(id=lesson_id).exists())

    @patch("apps.router.extraction_callbacks._edit_message")
    def test_undo_goal_removes_from_document(self, mock_edit):
        from apps.router.extraction_callbacks import handle_extraction_callback, _approve_goal

        pending = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.GOAL,
            text="Get first paying subscriber",
            status=PendingExtraction.Status.APPROVED,
            resolved_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        _approve_goal(pending)
        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.GOAL, slug="goals")
        self.assertIn("Get first paying subscriber", doc.markdown)

        update = self._make_update(f"extract:undo_goal:{pending.id}")
        handle_extraction_callback(update, self.tenant)
        pending.refresh_from_db()
        self.assertEqual(pending.status, PendingExtraction.Status.UNDONE)
        doc.refresh_from_db()
        self.assertNotIn("Get first paying subscriber", doc.markdown)

    @patch("apps.router.extraction_callbacks._edit_message")
    def test_undo_task_removes_from_document(self, mock_edit):
        from apps.router.extraction_callbacks import handle_extraction_callback, _approve_task

        pending = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.TASK,
            text="Create GitHub issue for IFSI",
            status=PendingExtraction.Status.APPROVED,
            resolved_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        _approve_task(pending)
        doc = Document.objects.get(tenant=self.tenant, kind=Document.Kind.TASKS, slug="tasks")
        self.assertIn("GitHub issue for IFSI", doc.markdown)

        update = self._make_update(f"extract:undo_task:{pending.id}")
        handle_extraction_callback(update, self.tenant)
        pending.refresh_from_db()
        self.assertEqual(pending.status, PendingExtraction.Status.UNDONE)
        doc.refresh_from_db()
        self.assertNotIn("GitHub issue for IFSI", doc.markdown)

    @patch("apps.router.extraction_callbacks._edit_message")
    def test_undo_is_idempotent(self, mock_edit):
        from apps.router.extraction_callbacks import handle_extraction_callback

        pending = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.LESSON,
            text="Already undone item test.",
            status=PendingExtraction.Status.UNDONE,
            resolved_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=7),
        )
        update = self._make_update(f"extract:undo_lesson:{pending.id}")
        resp = handle_extraction_callback(update, self.tenant)
        # Should return "Already removed" without error
        self.assertEqual(resp.status_code, 200)
