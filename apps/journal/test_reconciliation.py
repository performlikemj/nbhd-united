"""Tests for the journal → Task/Goal reconciliation module."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from apps.journal.models import Goal, PendingTaskAction, Task
from apps.journal.reconciliation import (
    apply_goal_action,
    apply_reconciliation_deltas,
    apply_subtask_create,
    apply_task_action,
    gather_reconciliation_context,
    undo_task_action,
)
from apps.tenants.models import Tenant, User


def _make_tenant(slug: str) -> Tenant:
    user = User.objects.create_user(username=slug, password="x" * 32)
    return Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        experimental_typed_journal_lifecycle=True,
    )


class GatherReconciliationContextTest(TestCase):
    def test_returns_open_tasks_and_active_goals_only(self):
        tenant = _make_tenant("ctx@x.test")
        # Two open + one done task — only open should appear
        open_task = Task.objects.create(tenant=tenant, title="Pay bill", status=Task.Status.OPEN)
        Task.objects.create(tenant=tenant, title="Gym", status=Task.Status.IN_PROGRESS)
        Task.objects.create(tenant=tenant, title="Old task", status=Task.Status.DONE)
        # One active + one achieved goal
        Goal.objects.create(tenant=tenant, title="Lose 5kg", status=Goal.Status.ACTIVE)
        Goal.objects.create(tenant=tenant, title="Done goal", status=Goal.Status.ACHIEVED)

        ctx = gather_reconciliation_context(tenant)

        self.assertEqual(len(ctx["open_tasks"]), 2)
        self.assertEqual(len(ctx["active_goals"]), 1)
        # IDs are stringified for JSON safety
        self.assertTrue(any(t["id"] == str(open_task.id) for t in ctx["open_tasks"]))
        self.assertEqual(ctx["active_goals"][0]["title"], "Lose 5kg")

    def test_excludes_other_tenants(self):
        a = _make_tenant("a@x.test")
        b = _make_tenant("b@x.test")
        Task.objects.create(tenant=a, title="A's task", status=Task.Status.OPEN)
        Task.objects.create(tenant=b, title="B's task", status=Task.Status.OPEN)

        ctx = gather_reconciliation_context(a)

        self.assertEqual(len(ctx["open_tasks"]), 1)
        self.assertEqual(ctx["open_tasks"][0]["title"], "A's task")


class ApplyTaskActionTest(TestCase):
    def setUp(self):
        self.tenant = _make_tenant("task@x.test")
        self.task = Task.objects.create(tenant=self.tenant, title="Gym session")

    def test_complete_marks_done_and_records_pta(self):
        pta = apply_task_action(
            tenant=self.tenant,
            task_id=str(self.task.id),
            action="complete",
            evidence="did the gym today",
            source_date=date(2026, 5, 24),
        )

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)
        self.assertIsNotNone(self.task.completed_at)
        self.assertIsNotNone(pta)
        self.assertEqual(pta.kind, PendingTaskAction.Kind.TASK_COMPLETE)
        self.assertEqual(pta.task_id, self.task.id)
        self.assertEqual(pta.before_state["status"], Task.Status.OPEN)
        self.assertEqual(pta.evidence, "did the gym today")

    def test_complete_on_already_done_is_noop(self):
        self.task.complete()
        pta = apply_task_action(
            tenant=self.tenant,
            task_id=str(self.task.id),
            action="complete",
            evidence="redundant",
            source_date=date(2026, 5, 24),
        )
        self.assertIsNone(pta)
        self.assertEqual(PendingTaskAction.objects.count(), 0)

    def test_cross_tenant_task_id_silently_skipped(self):
        other = _make_tenant("other@x.test")
        other_task = Task.objects.create(tenant=other, title="Other's task")

        pta = apply_task_action(
            tenant=self.tenant,
            task_id=str(other_task.id),
            action="complete",
            evidence="hack",
            source_date=date(2026, 5, 24),
        )
        self.assertIsNone(pta)
        other_task.refresh_from_db()
        self.assertEqual(other_task.status, Task.Status.OPEN)

    def test_malformed_uuid_silently_skipped(self):
        pta = apply_task_action(
            tenant=self.tenant,
            task_id="not-a-uuid",
            action="complete",
            evidence="",
            source_date=date(2026, 5, 24),
        )
        self.assertIsNone(pta)

    def test_unknown_action_silently_skipped(self):
        pta = apply_task_action(
            tenant=self.tenant,
            task_id=str(self.task.id),
            action="explode",
            evidence="",
            source_date=date(2026, 5, 24),
        )
        self.assertIsNone(pta)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.OPEN)


class ApplySubtaskCreateTest(TestCase):
    def test_creates_child_with_parent_fk_and_inherits_pillar(self):
        tenant = _make_tenant("sub@x.test")
        parent = Task.objects.create(tenant=tenant, title="Gym session", pillar="fuel")

        pta = apply_subtask_create(
            tenant=tenant,
            parent_task_id=str(parent.id),
            title="Cardio set",
            source_date=date(2026, 5, 24),
        )

        self.assertIsNotNone(pta)
        self.assertEqual(pta.kind, PendingTaskAction.Kind.SUBTASK_CREATE)
        subtask = Task.objects.get(id=pta.task_id)
        self.assertEqual(subtask.parent_task_id, parent.id)
        self.assertEqual(subtask.pillar, "fuel")
        self.assertEqual(subtask.title, "Cardio set")

    def test_unknown_parent_silently_skipped(self):
        tenant = _make_tenant("sub2@x.test")
        pta = apply_subtask_create(
            tenant=tenant,
            parent_task_id=str(uuid4()),
            title="Orphan",
            source_date=date(2026, 5, 24),
        )
        self.assertIsNone(pta)


class ApplyGoalActionTest(TestCase):
    def test_achieve_marks_goal_and_records_pta(self):
        tenant = _make_tenant("goal@x.test")
        goal = Goal.objects.create(tenant=tenant, title="Lose 5kg")

        pta = apply_goal_action(
            tenant=tenant,
            goal_id=str(goal.id),
            action="achieve",
            evidence="hit my target weight today",
            source_date=date(2026, 5, 24),
        )

        goal.refresh_from_db()
        self.assertEqual(goal.status, Goal.Status.ACHIEVED)
        self.assertIsNotNone(goal.achieved_at)
        self.assertIsNotNone(pta)
        self.assertEqual(pta.kind, PendingTaskAction.Kind.GOAL_ACHIEVE)
        self.assertEqual(pta.before_state["status"], Goal.Status.ACTIVE)


class ApplyReconciliationDeltasTest(TestCase):
    def test_applies_each_delta_type(self):
        tenant = _make_tenant("deltas@x.test")
        task_to_complete = Task.objects.create(tenant=tenant, title="Pay bill")
        task_to_subtask = Task.objects.create(tenant=tenant, title="Gym")
        goal_to_achieve = Goal.objects.create(tenant=tenant, title="5kg")

        deltas = {
            "task_updates": [{"task_id": str(task_to_complete.id), "action": "complete", "evidence": "paid it"}],
            "subtasks_added": [{"parent_task_id": str(task_to_subtask.id), "title": "Cardio"}],
            "goal_updates": [{"goal_id": str(goal_to_achieve.id), "action": "achieve", "evidence": "hit target"}],
        }

        actions = apply_reconciliation_deltas(tenant=tenant, deltas=deltas, source_date=date(2026, 5, 24))

        self.assertEqual(len(actions), 3)
        task_to_complete.refresh_from_db()
        self.assertEqual(task_to_complete.status, Task.Status.DONE)
        goal_to_achieve.refresh_from_db()
        self.assertEqual(goal_to_achieve.status, Goal.Status.ACHIEVED)
        self.assertEqual(Task.objects.filter(parent_task=task_to_subtask).count(), 1)

    def test_malformed_entries_silently_skipped(self):
        tenant = _make_tenant("malformed@x.test")
        task = Task.objects.create(tenant=tenant, title="Real task")

        deltas = {
            "task_updates": [
                {"task_id": str(task.id), "action": "complete", "evidence": "done"},
                "not a dict",  # skipped
                {"task_id": "bogus-uuid", "action": "complete", "evidence": ""},  # skipped
            ],
        }

        actions = apply_reconciliation_deltas(tenant=tenant, deltas=deltas, source_date=date(2026, 5, 24))

        self.assertEqual(len(actions), 1)


class UndoTaskActionTest(TestCase):
    def test_undo_complete_restores_open(self):
        tenant = _make_tenant("undo@x.test")
        task = Task.objects.create(tenant=tenant, title="X")

        pta = apply_task_action(
            tenant=tenant,
            task_id=str(task.id),
            action="complete",
            evidence="",
            source_date=date(2026, 5, 24),
        )
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)

        ok = undo_task_action(pta)

        task.refresh_from_db()
        self.assertTrue(ok)
        self.assertEqual(task.status, Task.Status.OPEN)
        self.assertIsNone(task.completed_at)

    def test_undo_subtask_create_deletes_child(self):
        tenant = _make_tenant("undosub@x.test")
        parent = Task.objects.create(tenant=tenant, title="Parent")
        pta = apply_subtask_create(
            tenant=tenant,
            parent_task_id=str(parent.id),
            title="Child",
            source_date=date(2026, 5, 24),
        )
        child_id = pta.task_id
        self.assertTrue(Task.objects.filter(id=child_id).exists())

        ok = undo_task_action(pta)

        self.assertTrue(ok)
        self.assertFalse(Task.objects.filter(id=child_id).exists())

    def test_undo_goal_achieve_restores_active(self):
        tenant = _make_tenant("undogoal@x.test")
        goal = Goal.objects.create(tenant=tenant, title="G")
        pta = apply_goal_action(
            tenant=tenant,
            goal_id=str(goal.id),
            action="achieve",
            evidence="",
            source_date=date(2026, 5, 24),
        )
        goal.refresh_from_db()
        self.assertEqual(goal.status, Goal.Status.ACHIEVED)

        ok = undo_task_action(pta)

        goal.refresh_from_db()
        self.assertTrue(ok)
        self.assertEqual(goal.status, Goal.Status.ACTIVE)
        self.assertIsNone(goal.achieved_at)


class ExtractionRunnerReconciliationTest(TestCase):
    """End-to-end: extension wires reconciliation into nightly_extraction."""

    def test_run_extraction_applies_deltas_when_flag_on(self):
        from apps.journal.extraction import run_extraction_for_tenant
        from apps.journal.models import Document

        tenant = _make_tenant("e2e@x.test")
        tenant.user.telegram_chat_id = 99999
        tenant.user.save()
        task = Task.objects.create(tenant=tenant, title="Pay credit card")

        # Seed a daily note long enough to clear MIN_NOTE_LENGTH
        Document.objects.create(
            tenant=tenant,
            kind=Document.Kind.DAILY,
            slug=str(date.today()),
            title="Today",
            markdown="Paid the credit card today, finally. " * 10,
        )

        fake_llm_response = (
            {
                "lessons": [],
                "goals": [],
                "tasks": [],
                "task_updates": [
                    {"task_id": str(task.id), "action": "complete", "evidence": "Paid the credit card today"}
                ],
                "subtasks_added": [],
                "goal_updates": [],
            },
            {"prompt_tokens": 100, "completion_tokens": 20},
        )

        with (
            patch("apps.journal.extraction._call_extraction_llm", return_value=fake_llm_response),
            patch("apps.journal.extraction._send_telegram_with_buttons", return_value=1234),
            patch(
                "apps.journal.extraction._resolve_delivery_channel",
                return_value=("telegram", 99999, "fake-token"),
            ),
            patch("apps.journal.extraction.record_usage"),
            patch("apps.journal.extraction.embed_daily_note", create=True, return_value=0),
        ):
            result = run_extraction_for_tenant(tenant)

        self.assertEqual(result["task_actions"], 1)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertEqual(PendingTaskAction.objects.filter(tenant=tenant).count(), 1)
