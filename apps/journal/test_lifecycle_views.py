"""Tests for session-auth typed write endpoints + the markdown-PATCH guard.

The write-path fix: GET re-synthesizes tasks/goal docs from typed rows, so a
markdown PATCH would be silently discarded. These verify (a) the typed write
endpoints update the rows correctly, and (b) DocumentDetailView.patch now
rejects (409) markdown writes to flag-on tasks/goal docs instead of losing them.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from apps.journal.models import Document, Goal, Task
from apps.tenants.models import Tenant, User


class TypedWriteEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="writeuser", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active", experimental_typed_journal_lifecycle=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_complete_sets_status_and_timestamp(self):
        task = Task.objects.create(tenant=self.tenant, title="Pay loan", status=Task.Status.OPEN)
        resp = self.client.post(f"/api/v1/journal/tasks/{task.id}/complete/")
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.DONE)
        self.assertIsNotNone(task.completed_at)

    def test_reopen_clears_timestamp(self):
        task = Task.objects.create(tenant=self.tenant, title="Pay loan", status=Task.Status.OPEN)
        task.complete()
        resp = self.client.post(f"/api/v1/journal/tasks/{task.id}/reopen/")
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.OPEN)
        self.assertIsNone(task.completed_at)

    def test_patch_task_title(self):
        task = Task.objects.create(tenant=self.tenant, title="old", status=Task.Status.OPEN)
        resp = self.client.patch(f"/api/v1/journal/tasks/{task.id}/", {"title": "new"}, format="json")
        self.assertEqual(resp.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.title, "new")

    def test_goal_achieve_and_abandon(self):
        goal = Goal.objects.create(tenant=self.tenant, title="Debt-free", status=Goal.Status.ACTIVE)
        resp = self.client.post(f"/api/v1/journal/goals/{goal.id}/achieve/")
        self.assertEqual(resp.status_code, 200)
        goal.refresh_from_db()
        self.assertEqual(goal.status, Goal.Status.ACHIEVED)
        self.assertIsNotNone(goal.achieved_at)

        resp = self.client.post(f"/api/v1/journal/goals/{goal.id}/abandon/")
        self.assertEqual(resp.status_code, 200)
        goal.refresh_from_db()
        self.assertEqual(goal.status, Goal.Status.ABANDONED)

    def test_requires_auth(self):
        task = Task.objects.create(tenant=self.tenant, title="t", status=Task.Status.OPEN)
        anon = APIClient()
        resp = anon.post(f"/api/v1/journal/tasks/{task.id}/complete/")
        self.assertIn(resp.status_code, (401, 403))

    def test_tenant_isolation(self):
        other_user = User.objects.create_user(username="other_w", password="x")
        other_tenant = Tenant.objects.create(user=other_user, status="active")
        task = Task.objects.create(tenant=other_tenant, title="theirs", status=Task.Status.OPEN)
        resp = self.client.post(f"/api/v1/journal/tasks/{task.id}/complete/")
        self.assertEqual(resp.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.OPEN)  # untouched


class MarkdownPatchGuardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="guarduser", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active", experimental_typed_journal_lifecycle=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_markdown_patch_to_tasks_doc_rejected_when_flag_on(self):
        resp = self.client.patch(
            "/api/v1/journal/documents/tasks/tasks/",
            {"markdown": "# hand edit\n- [ ] sneaky"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["error"], "typed_lifecycle_readonly")

    def test_markdown_patch_to_goal_doc_rejected_when_flag_on(self):
        Document.objects.create(tenant=self.tenant, kind="goal", slug="g", title="G", markdown="# G")
        resp = self.client.patch(
            "/api/v1/journal/documents/goal/g/",
            {"markdown": "# hand edit"},
            format="json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_title_only_patch_to_tasks_doc_allowed(self):
        resp = self.client.patch(
            "/api/v1/journal/documents/tasks/tasks/",
            {"title": "My Tasks"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_markdown_patch_to_daily_doc_allowed(self):
        Document.objects.create(tenant=self.tenant, kind="daily", slug="2026-06-04", title="D", markdown="# D")
        resp = self.client.patch(
            "/api/v1/journal/documents/daily/2026-06-04/",
            {"markdown": "# edited daily log"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("edited daily log", resp.data["markdown"])

    def test_markdown_patch_to_tasks_doc_allowed_when_flag_off(self):
        self.tenant.experimental_typed_journal_lifecycle = False
        self.tenant.save(update_fields=["experimental_typed_journal_lifecycle"])
        resp = self.client.patch(
            "/api/v1/journal/documents/tasks/tasks/",
            {"markdown": "# legacy editable tasks"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("legacy editable tasks", resp.data["markdown"])
