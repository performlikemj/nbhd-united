"""Tests for session-auth typed write endpoints + the markdown-PATCH guard.

The write-path fix: GET re-synthesizes tasks/goal docs from typed rows, so a
markdown PATCH would be silently discarded. These verify (a) the typed write
endpoints update the rows correctly, and (b) DocumentDetailView.patch now
rejects (409) markdown writes to flag-on tasks/goal docs instead of losing them.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.journal.lifecycle_views import _search_variants
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


class TaskGoalListCreateTests(TestCase):
    """GET (list) + POST (create) for /tasks/ and /goals/ — the collection
    endpoints the connected iOS client uses to enumerate and add typed rows."""

    def setUp(self):
        self.user = User.objects.create_user(username="lcuser", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.other_user = User.objects.create_user(username="lcother", password="x")
        self.other_tenant = Tenant.objects.create(user=self.other_user, status="active")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # ── Tasks ────────────────────────────────────────────────────────

    def test_create_task(self):
        resp = self.client.post(
            "/api/v1/journal/tasks/",
            {"title": "Pay loan", "pillar": "gravity", "due_date": "2026-06-20"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["title"], "Pay loan")
        self.assertEqual(resp.data["status"], Task.Status.OPEN)
        task = Task.objects.get(id=resp.data["id"])
        self.assertEqual(task.tenant_id, self.tenant.id)
        self.assertEqual(task.title, "Pay loan")
        self.assertEqual(
            task.pii_receipts["title"],
            {"state": "bypass", "mode": "legacy-redact"},
        )

    def test_flag_on_create_stores_placeholder_and_owner_receipt(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.post(
                "/api/v1/journal/tasks/",
                {"title": "Call Alice"},
                format="json",
            )

        self.assertEqual(resp.status_code, 201)
        task = Task.objects.get(id=resp.data["id"])
        self.assertEqual(task.title, "Call [PERSON_1]")
        self.assertEqual(resp.data["title"], "Call Alice")
        self.assertEqual(task.pii_receipts["title"]["state"], "placeholder")
        self.assertEqual(
            resp.data["pii_receipts"]["title"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

    def test_create_task_requires_title(self):
        resp = self.client.post("/api/v1/journal/tasks/", {"pillar": "gravity"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_list_tasks_scoped_to_tenant(self):
        Task.objects.create(tenant=self.tenant, title="mine")
        Task.objects.create(tenant=self.other_tenant, title="theirs")
        resp = self.client.get("/api/v1/journal/tasks/")
        self.assertEqual(resp.status_code, 200)
        titles = [t["title"] for t in resp.data]
        self.assertEqual(titles, ["mine"])

    def test_list_tasks_filter_by_status(self):
        Task.objects.create(tenant=self.tenant, title="open one", status=Task.Status.OPEN)
        Task.objects.create(tenant=self.tenant, title="done one", status=Task.Status.DONE)
        resp = self.client.get("/api/v1/journal/tasks/?status=done")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t["title"] for t in resp.data], ["done one"])

    def test_list_tasks_filter_due_before(self):
        Task.objects.create(tenant=self.tenant, title="soon", due_date="2026-06-10")
        Task.objects.create(tenant=self.tenant, title="later", due_date="2026-07-10")
        resp = self.client.get("/api/v1/journal/tasks/?due_before=2026-06-30")
        self.assertEqual([t["title"] for t in resp.data], ["soon"])

    def test_list_tasks_invalid_due_before_returns_400(self):
        resp = self.client.get("/api/v1/journal/tasks/?due_before=nope")
        self.assertEqual(resp.status_code, 400)

    def test_list_tasks_q_name_search_case_insensitive(self):
        Task.objects.create(tenant=self.tenant, title="Call the Dentist")
        Task.objects.create(tenant=self.tenant, title="Buy groceries")
        resp = self.client.get("/api/v1/journal/tasks/?q=dentist")
        self.assertEqual(resp.status_code, 200)
        titles = [t["title"] for t in resp.data]
        self.assertEqual(titles, ["Call the Dentist"])
        # Every result carries the stable UUID id the EntityQuery keys on.
        self.assertIn("id", resp.data[0])

    def test_task_search_real_name_finds_placeholder_stored_row(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        Task.objects.create(tenant=self.tenant, title="Call [PERSON_1]")

        resp = self.client.get("/api/v1/journal/tasks/?q=ALICE")

        self.assertEqual([task["title"] for task in resp.data], ["Call Alice"])

    def test_task_search_cross_product_finds_fully_redacted_multi_name_title(self):
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Alex"},
            "[PERSON_2]": {"name": "Bob"},
        }
        self.tenant.save(update_fields=["pii_entity_map"])
        Task.objects.create(tenant=self.tenant, title="Call [PERSON_1] about [PERSON_2]")

        resp = self.client.get("/api/v1/journal/tasks/?q=call%20Alex%20about%20Bob")

        self.assertEqual([task["title"] for task in resp.data], ["Call Alex about Bob"])

    def test_search_variants_skip_short_values_and_cap_with_log(self):
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Li"},
            **{f"[PERSON_{index + 2}]": {"name": f"Name{index:02d}"} for index in range(25)},
        }
        self.assertEqual(_search_variants(self.tenant, "Call Li"), ["Call Li"])

        query = " ".join(f"Name{index:02d}" for index in range(25))
        with self.assertLogs("apps.journal.lifecycle_views", level="WARNING") as logs:
            variants = _search_variants(self.tenant, query)

        self.assertEqual(len(variants), 20)
        self.assertEqual(variants[0], query)
        self.assertIn("pii_search_variants_capped", logs.output[0])

    def test_list_tasks_q_no_match_returns_empty(self):
        Task.objects.create(tenant=self.tenant, title="Call the Dentist")
        resp = self.client.get("/api/v1/journal/tasks/?q=zzz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, [])

    def test_list_tasks_q_scoped_to_tenant(self):
        Task.objects.create(tenant=self.tenant, title="Renew passport")
        Task.objects.create(tenant=self.other_tenant, title="Renew passport")
        resp = self.client.get("/api/v1/journal/tasks/?q=passport")
        self.assertEqual([t["title"] for t in resp.data], ["Renew passport"])

    def test_list_tasks_q_capped_at_20(self):
        for i in range(25):
            Task.objects.create(tenant=self.tenant, title=f"Review doc {i}")
        resp = self.client.get("/api/v1/journal/tasks/?q=review")
        self.assertEqual(len(resp.data), 20)

    def test_get_task_detail(self):
        task = Task.objects.create(tenant=self.tenant, title="read me")
        resp = self.client.get(f"/api/v1/journal/tasks/{task.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["title"], "read me")

    def test_get_task_detail_other_tenant_404(self):
        task = Task.objects.create(tenant=self.other_tenant, title="theirs")
        resp = self.client.get(f"/api/v1/journal/tasks/{task.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_create_task_cross_tenant_parent_goal_rejected(self):
        their_goal = Goal.objects.create(tenant=self.other_tenant, title="their goal")
        resp = self.client.post(
            "/api/v1/journal/tasks/",
            {"title": "sneaky", "parent_goal_id": str(their_goal.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_requires_auth(self):
        anon = APIClient()
        resp = anon.post("/api/v1/journal/tasks/", {"title": "x"}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    # ── Goals ────────────────────────────────────────────────────────

    def test_create_goal(self):
        resp = self.client.post(
            "/api/v1/journal/goals/",
            {"title": "Debt-free", "pillar": "gravity", "target_date": "2027-01-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], Goal.Status.ACTIVE)
        self.assertEqual(Goal.objects.get(id=resp.data["id"]).tenant_id, self.tenant.id)

    def test_list_goals_scoped_to_tenant(self):
        Goal.objects.create(tenant=self.tenant, title="mine")
        Goal.objects.create(tenant=self.other_tenant, title="theirs")
        resp = self.client.get("/api/v1/journal/goals/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([g["title"] for g in resp.data], ["mine"])

    def test_list_goals_filter_by_status(self):
        Goal.objects.create(tenant=self.tenant, title="active one", status=Goal.Status.ACTIVE)
        Goal.objects.create(tenant=self.tenant, title="achieved one", status=Goal.Status.ACHIEVED)
        resp = self.client.get("/api/v1/journal/goals/?status=achieved")
        self.assertEqual([g["title"] for g in resp.data], ["achieved one"])

    def test_list_goals_q_name_search_case_insensitive(self):
        Goal.objects.create(tenant=self.tenant, title="Run a Marathon")
        Goal.objects.create(tenant=self.tenant, title="Learn piano")
        resp = self.client.get("/api/v1/journal/goals/?q=marathon")
        self.assertEqual([g["title"] for g in resp.data], ["Run a Marathon"])
        self.assertIn("id", resp.data[0])

    def test_goal_search_real_name_finds_placeholder_stored_row(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        Goal.objects.create(tenant=self.tenant, title="Support [PERSON_1]")

        resp = self.client.get("/api/v1/journal/goals/?q=alice")

        self.assertEqual([goal["title"] for goal in resp.data], ["Support Alice"])

    def test_list_goals_q_scoped_to_tenant(self):
        Goal.objects.create(tenant=self.tenant, title="Save for a house")
        Goal.objects.create(tenant=self.other_tenant, title="Save for a house")
        resp = self.client.get("/api/v1/journal/goals/?q=house")
        self.assertEqual([g["title"] for g in resp.data], ["Save for a house"])

    def test_list_goals_q_capped_at_20(self):
        for i in range(25):
            Goal.objects.create(tenant=self.tenant, title=f"Milestone {i}")
        resp = self.client.get("/api/v1/journal/goals/?q=milestone")
        self.assertEqual(len(resp.data), 20)
