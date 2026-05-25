"""Phase-by-phase integration tests for the journal → task reconciliation feature.

Walks back through phases 5, 7, and 10 with end-to-end assertions that catch
the wiring bugs unit tests miss:

- **Phase 5**: ``_approve_task`` / ``_approve_goal`` write typed rows when
  the tenant flag is on, fall back to legacy markdown otherwise; undo paths
  correctly delete the typed row.
- **Phase 7**: ``handle_task_action_callback`` (Telegram) and
  ``handle_task_action_postback_line`` (LINE) both route ``task_action:undo:<id>``
  correctly, restore the row to ``before_state``, and update
  ``PendingTaskAction.status`` to UNDONE.
- **Phase 10**: ``GET /api/v1/journal/documents/tasks/tasks/`` returns
  synthesized markdown for flag-on tenants; legacy markdown for flag-off
  tenants; underlying ``Document.markdown`` is never mutated by reads.

Phase 6 end-to-end is in ``test_reconciliation.ExtractionRunnerReconciliationTest``.
Phase 9 is a deletion confirmed by grep.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.journal.models import (
    Document,
    Goal,
    PendingExtraction,
    PendingTaskAction,
    Task,
)
from apps.router.extraction_callbacks import _approve_goal, _approve_task, _undo_goal, _undo_task
from apps.router.task_action_callbacks import (
    handle_task_action_callback,
    handle_task_action_postback_line,
)
from apps.tenants.models import Tenant, User


def _make_tenant(slug: str, *, flag_on: bool) -> Tenant:
    user = User.objects.create_user(username=slug, password="x" * 32)
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        experimental_typed_journal_lifecycle=flag_on,
    )


def _make_pending(tenant: Tenant, kind: str, text: str) -> PendingExtraction:
    return PendingExtraction.objects.create(
        tenant=tenant,
        kind=kind,
        text=text,
        expires_at=timezone.now() + timedelta(days=7),
        source_date=timezone.now().date(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 5 — flag-aware approve / undo callbacks
# ─────────────────────────────────────────────────────────────────────────────


class Phase5ApproveTaskFlagOnTest(TestCase):
    def test_approve_task_creates_typed_row_and_stores_fk(self):
        tenant = _make_tenant("p5-task-on", flag_on=True)
        pending = _make_pending(tenant, PendingExtraction.Kind.TASK, "Pay credit card")

        _approve_task(pending)

        pending.refresh_from_db()
        self.assertIsNotNone(pending.task_id, "task_id FK should be set on pending after approve")
        task = Task.objects.get(id=pending.task_id)
        self.assertEqual(task.tenant_id, tenant.id)
        self.assertEqual(task.title, "Pay credit card")
        self.assertEqual(task.status, Task.Status.OPEN)
        # And no legacy markdown document was created
        self.assertFalse(
            Document.objects.filter(tenant=tenant, kind=Document.Kind.TASKS).exists(),
            "flag-on path should NOT touch the legacy tasks document",
        )

    def test_undo_task_deletes_typed_row(self):
        tenant = _make_tenant("p5-undo-on", flag_on=True)
        pending = _make_pending(tenant, PendingExtraction.Kind.TASK, "Buy groceries")
        _approve_task(pending)
        pending.refresh_from_db()
        task_id = pending.task_id
        self.assertTrue(Task.objects.filter(id=task_id).exists())

        _undo_task(pending)

        self.assertFalse(Task.objects.filter(id=task_id).exists(), "undo should delete the typed Task row")


class Phase5ApproveGoalFlagOnTest(TestCase):
    def test_approve_goal_creates_typed_row_and_stores_fk(self):
        tenant = _make_tenant("p5-goal-on", flag_on=True)
        pending = _make_pending(tenant, PendingExtraction.Kind.GOAL, "Ship the redesign")

        _approve_goal(pending)

        pending.refresh_from_db()
        self.assertIsNotNone(pending.goal_id)
        goal = Goal.objects.get(id=pending.goal_id)
        self.assertEqual(goal.tenant_id, tenant.id)
        self.assertEqual(goal.title, "Ship the redesign")
        self.assertEqual(goal.status, Goal.Status.ACTIVE)
        self.assertFalse(Document.objects.filter(tenant=tenant, kind=Document.Kind.GOAL).exists())

    def test_undo_goal_deletes_typed_row(self):
        tenant = _make_tenant("p5-undo-goal-on", flag_on=True)
        pending = _make_pending(tenant, PendingExtraction.Kind.GOAL, "Hit 200 reps")
        _approve_goal(pending)
        pending.refresh_from_db()
        goal_id = pending.goal_id

        _undo_goal(pending)

        self.assertFalse(Goal.objects.filter(id=goal_id).exists())


class Phase5LegacyFallbackTest(TestCase):
    def test_approve_task_appends_to_markdown_when_flag_off(self):
        tenant = _make_tenant("p5-task-off", flag_on=False)
        pending = _make_pending(tenant, PendingExtraction.Kind.TASK, "Old-style task")

        _approve_task(pending)

        pending.refresh_from_db()
        self.assertIsNone(pending.task_id, "flag-off path should NOT create a typed Task")
        self.assertEqual(Task.objects.filter(tenant=tenant).count(), 0)
        doc = Document.objects.get(tenant=tenant, kind=Document.Kind.TASKS, slug="tasks")
        self.assertIn("Old-style task", doc.markdown)
        self.assertIn("- [ ]", doc.markdown)

    def test_approve_goal_appends_to_markdown_when_flag_off(self):
        tenant = _make_tenant("p5-goal-off", flag_on=False)
        pending = _make_pending(tenant, PendingExtraction.Kind.GOAL, "Old-style goal")

        _approve_goal(pending)

        pending.refresh_from_db()
        self.assertIsNone(pending.goal_id)
        self.assertEqual(Goal.objects.filter(tenant=tenant).count(), 0)
        doc = Document.objects.get(tenant=tenant, kind=Document.Kind.GOAL, slug="goals")
        self.assertIn("Old-style goal", doc.markdown)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 7 — Telegram + LINE undo routers
# ─────────────────────────────────────────────────────────────────────────────


def _seed_applied_action(tenant: Tenant, *, before_status: str = Task.Status.OPEN) -> tuple[Task, PendingTaskAction]:
    """Make a Task that's been marked DONE with a recorded PendingTaskAction.

    Mirrors the state the runner would leave behind after applying a
    ``task_complete`` delta.
    """
    task = Task.objects.create(
        tenant=tenant, title="Reconciled task", status=Task.Status.DONE, completed_at=timezone.now()
    )
    pta = PendingTaskAction.objects.create(
        tenant=tenant,
        kind=PendingTaskAction.Kind.TASK_COMPLETE,
        task=task,
        evidence="did it today",
        source_date=timezone.now().date(),
        before_state={"status": before_status, "completed_at": None},
    )
    return task, pta


class Phase7TelegramUndoRouterTest(TestCase):
    def test_undo_callback_restores_task_and_marks_pta_undone(self):
        tenant = _make_tenant("p7-tg", flag_on=True)
        task, pta = _seed_applied_action(tenant)

        update = {
            "callback_query": {
                "id": "cb-1",
                "data": f"task_action:undo:{pta.id}",
                "message": {"chat": {"id": 12345}, "message_id": 67890},
            }
        }

        response = handle_task_action_callback(update, tenant)

        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        pta.refresh_from_db()
        self.assertEqual(task.status, Task.Status.OPEN, "before_state status should be restored")
        self.assertIsNone(task.completed_at, "before_state completed_at (None) should be restored")
        self.assertEqual(pta.status, PendingTaskAction.Status.UNDONE)
        self.assertIsNotNone(pta.resolved_at)

    def test_unknown_action_id_returns_not_found(self):
        tenant = _make_tenant("p7-tg-miss", flag_on=True)
        update = {
            "callback_query": {
                "id": "cb-2",
                "data": f"task_action:undo:{uuid4()}",
                "message": {"chat": {"id": 1}, "message_id": 1},
            }
        }
        # No PTA exists for that UUID — handler should respond gracefully
        response = handle_task_action_callback(update, tenant)
        self.assertEqual(response.status_code, 200)

    def test_already_undone_action_is_idempotent(self):
        tenant = _make_tenant("p7-tg-idem", flag_on=True)
        task, pta = _seed_applied_action(tenant)
        pta.status = PendingTaskAction.Status.UNDONE
        pta.resolved_at = timezone.now()
        pta.save(update_fields=["status", "resolved_at"])

        update = {
            "callback_query": {
                "id": "cb-3",
                "data": f"task_action:undo:{pta.id}",
                "message": {"chat": {"id": 1}, "message_id": 1},
            }
        }
        response = handle_task_action_callback(update, tenant)

        self.assertEqual(response.status_code, 200)
        # Task should not have been mutated again (still DONE from setUp)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)


class Phase7LineUndoRouterTest(TestCase):
    def test_undo_postback_restores_task_and_marks_pta_undone(self):
        tenant = _make_tenant("p7-line", flag_on=True)
        task, pta = _seed_applied_action(tenant)

        ok, message = handle_task_action_postback_line(tenant, f"task_action:undo:{pta.id}")

        self.assertTrue(ok)
        self.assertIn("Undone", message)
        task.refresh_from_db()
        pta.refresh_from_db()
        self.assertEqual(task.status, Task.Status.OPEN)
        self.assertEqual(pta.status, PendingTaskAction.Status.UNDONE)

    def test_subtask_create_undo_deletes_the_subtask(self):
        tenant = _make_tenant("p7-line-sub", flag_on=True)
        parent = Task.objects.create(tenant=tenant, title="Parent")
        sub = Task.objects.create(tenant=tenant, title="Sub", parent_task=parent)
        pta = PendingTaskAction.objects.create(
            tenant=tenant,
            kind=PendingTaskAction.Kind.SUBTASK_CREATE,
            task=sub,
            source_date=timezone.now().date(),
            before_state={"created": True},
        )

        ok, _ = handle_task_action_postback_line(tenant, f"task_action:undo:{pta.id}")

        self.assertTrue(ok)
        self.assertFalse(Task.objects.filter(id=sub.id).exists())
        # Parent must NOT be deleted
        self.assertTrue(Task.objects.filter(id=parent.id).exists())

    def test_malformed_data_returns_false(self):
        tenant = _make_tenant("p7-line-bad", flag_on=True)
        ok, _ = handle_task_action_postback_line(tenant, "task_action:undo")  # missing id
        self.assertFalse(ok)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 10 — DocumentDetailView synthesis from typed rows
# ─────────────────────────────────────────────────────────────────────────────


class Phase10DocumentSynthesisTest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_flag_on_tasks_doc_returns_synthesized_markdown(self):
        tenant = _make_tenant("p10-tasks-on", flag_on=True)
        self.client.force_authenticate(user=tenant.user)
        Task.objects.create(tenant=tenant, title="Open one")
        Task.objects.create(tenant=tenant, title="Done one", status=Task.Status.DONE, completed_at=timezone.now())
        Task.objects.create(tenant=tenant, title="Deferred one", status=Task.Status.DEFERRED)
        # Pre-existing legacy markdown that we should NOT see in the response
        Document.objects.create(
            tenant=tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="LEGACY-ARCHIVE-SHOULD-NOT-APPEAR",
        )

        resp = self.client.get("/api/v1/journal/documents/tasks/tasks/")

        self.assertEqual(resp.status_code, 200)
        md = resp.data["markdown"]
        self.assertIn("## Open", md)
        self.assertIn("Open one", md)
        self.assertIn("## Done", md)
        self.assertIn("Done one", md)
        self.assertIn("## Deferred", md)
        self.assertIn("Deferred one", md)
        self.assertNotIn("LEGACY-ARCHIVE-SHOULD-NOT-APPEAR", md)

        # Underlying Document.markdown is preserved (synthesis is response-only)
        doc = Document.objects.get(tenant=tenant, kind=Document.Kind.TASKS, slug="tasks")
        self.assertEqual(doc.markdown, "LEGACY-ARCHIVE-SHOULD-NOT-APPEAR")

    def test_flag_off_tasks_doc_returns_legacy_markdown(self):
        tenant = _make_tenant("p10-tasks-off", flag_on=False)
        self.client.force_authenticate(user=tenant.user)
        Task.objects.create(tenant=tenant, title="Typed task should not appear")
        Document.objects.create(
            tenant=tenant,
            kind=Document.Kind.TASKS,
            slug="tasks",
            title="Tasks",
            markdown="# Tasks\n\n- [ ] LEGACY VISIBLE\n",
        )

        resp = self.client.get("/api/v1/journal/documents/tasks/tasks/")

        self.assertEqual(resp.status_code, 200)
        md = resp.data["markdown"]
        self.assertIn("LEGACY VISIBLE", md)
        self.assertNotIn("Typed task should not appear", md)

    def test_flag_on_goals_doc_returns_synthesized_markdown(self):
        tenant = _make_tenant("p10-goals-on", flag_on=True)
        self.client.force_authenticate(user=tenant.user)
        Goal.objects.create(tenant=tenant, title="Active one")
        Goal.objects.create(
            tenant=tenant, title="Achieved one", status=Goal.Status.ACHIEVED, achieved_at=timezone.now()
        )
        Document.objects.create(
            tenant=tenant,
            kind=Document.Kind.GOAL,
            slug="goals",
            title="Goals",
            markdown="LEGACY-GOALS-ARCHIVE",
        )

        resp = self.client.get("/api/v1/journal/documents/goal/goals/")

        self.assertEqual(resp.status_code, 200)
        md = resp.data["markdown"]
        self.assertIn("## Active", md)
        self.assertIn("Active one", md)
        self.assertIn("## Achieved", md)
        self.assertIn("Achieved one", md)
        self.assertNotIn("LEGACY-GOALS-ARCHIVE", md)
