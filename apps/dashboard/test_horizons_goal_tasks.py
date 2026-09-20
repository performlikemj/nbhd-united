"""Horizons goal checklist payload, tenant isolation, and cache invalidation."""

from datetime import date, timedelta

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.common.cache import get_tag_version
from apps.journal.models import Document, Goal, Task
from apps.tenants.services import create_tenant


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    NBHD_DISABLE_BACKGROUND_THREADS=True,
)
class HorizonsGoalTasksTests(TestCase):
    url = "/api/v1/dashboard/horizons/"

    def setUp(self):
        self.tenant = create_tenant(display_name="Goal workspace", telegram_chat_id=901810)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        cache.clear()
        self.addCleanup(cache.clear)

    def _goal_row(self, response, goal):
        self.assertEqual(response.status_code, 200)
        return next(row for row in response.json()["goals"] if row["id"] == str(goal.id))

    def test_typed_goal_tasks_shape_order_rehydration_and_single_query(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        goal = Goal.objects.create(tenant=self.tenant, title="Plan a trip", description="Travel slowly.")
        first = Task.objects.create(
            tenant=self.tenant, parent_goal=goal, title="Ask [PERSON_1]", due_date=date(2026, 10, 1)
        )
        second = Task.objects.create(tenant=self.tenant, parent_goal=goal, title="Choose dates")
        second.complete()
        Task.objects.filter(id=first.id).update(created_at=timezone.now() - timedelta(days=2))
        Task.objects.filter(id=second.id).update(created_at=timezone.now() - timedelta(days=1))
        another_goal = Goal.objects.create(tenant=self.tenant, title="Learn to paint")
        other_step = Task.objects.create(tenant=self.tenant, parent_goal=another_goal, title="Find a class")
        Task.objects.create(tenant=self.tenant, title="Unlinked task")
        other_tenant = create_tenant(display_name="Other workspace", telegram_chat_id=901811)
        # Even a mismatched tenant/parent row must not leak into the checklist.
        Task.objects.create(tenant=other_tenant, parent_goal=goal, title="Private step")

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)

        row = self._goal_row(response, goal)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["slug"], f"typed:{goal.id}")
        self.assertEqual(row["markdown"], "Travel slowly.")
        self.assertEqual(
            row["tasks"],
            [
                {"id": str(first.id), "title": "Ask Alice", "status": "open", "due_date": "2026-10-01"},
                {"id": str(second.id), "title": "Choose dates", "status": "done", "due_date": None},
            ],
        )
        self.assertEqual(self._goal_row(response, another_goal)["tasks"][0]["id"], str(other_step.id))
        task_reads = [query["sql"] for query in queries if 'FROM "journal_tasks"' in query["sql"]]
        self.assertEqual(len(task_reads), 1, task_reads)

    def test_legacy_goal_has_no_tasks(self):
        doc = Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="learn-pottery",
            title="Learn pottery",
            markdown="Try a weekend class.",
        )
        row = self._goal_row(self.client.get(self.url), doc)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["tasks"], [])
        self.assertEqual(row["slug"], "learn-pottery")

    def test_task_save_invalidates_cached_horizons(self):
        goal = Goal.objects.create(tenant=self.tenant, title="Practice piano")
        task = Task.objects.create(tenant=self.tenant, parent_goal=goal, title="Practice scales")
        self.assertEqual(self.client.get(self.url)["X-Cache"], "MISS")
        self.assertEqual(self.client.get(self.url)["X-Cache"], "HIT")
        before = get_tag_version(self.tenant.id, "dashboard")

        task.complete()

        self.assertGreater(get_tag_version(self.tenant.id, "dashboard"), before)
        response = self.client.get(self.url)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(self._goal_row(response, goal)["tasks"][0]["status"], "done")

    def test_task_delete_invalidates_cached_horizons(self):
        goal = Goal.objects.create(tenant=self.tenant, title="Practice piano")
        task = Task.objects.create(tenant=self.tenant, parent_goal=goal, title="Practice scales")
        self.client.get(self.url)
        self.assertEqual(self.client.get(self.url)["X-Cache"], "HIT")
        before = get_tag_version(self.tenant.id, "dashboard")

        task.delete()

        self.assertGreater(get_tag_version(self.tenant.id, "dashboard"), before)
        response = self.client.get(self.url)
        self.assertEqual(response["X-Cache"], "MISS")
        self.assertEqual(self._goal_row(response, goal)["tasks"], [])

    def test_achieved_goal_does_not_resurface_its_legacy_document(self):
        doc = Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.GOAL,
            slug="piano",
            title="Practice piano",
            markdown="Practice every day.",
        )
        goal = Goal.objects.create(tenant=self.tenant, title="Practice piano", migrated_from_document=doc)
        self._goal_row(self.client.get(self.url), goal)
        goal.mark_achieved()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        ids = {row["id"] for row in response.json()["goals"]}
        self.assertNotIn(str(goal.id), ids)
        self.assertNotIn(str(doc.id), ids)
