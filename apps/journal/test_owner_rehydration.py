"""Owner-facing journal reads must show real values; owner writes must be
re-redacted so real PII never persists into agent-visible storage.

The per-tenant AI assistant runs on redacted input, so everything it authors
into journal storage (daily-note markdown, typed Task/Goal titles, document
titles) is stored in placeholder space — ``Book hotel for [LOCATION_330]``.
The owner must see the real value, so every owner-facing serve boundary
rehydrates. Symmetrically, when the owner edits that rehydrated text and saves
it back, the write endpoints re-redact so the real value does NOT land in
``Document.markdown`` where the agent would read it (via
``RuntimeDailyNotesView``) — that is the round-trip hazard these tests guard.

All PII fixtures use throwaway values ("july", "Sarah") and no real emails.
The known-entity Step-1 regex pass in ``redact_user_message`` reuses the
seeded entity map without the NER model, so the write-path tests stub
``_detect_pii`` to ``[]`` (mirroring ``apps.pii.tests``) to avoid a model load.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.journal.models import Document, Goal, Task
from apps.journal.status_projection import build_journal_status
from apps.tenants.models import Tenant, User

# Placeholder -> registry-dict entry (the fresh-mint shape).
ENTITY_MAP = {
    "[LOCATION_330]": {"name": "july"},
    "[PERSON_1]": {"name": "Sarah"},
}


class _AuthedTenantMixin:
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="owner", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            pii_entity_map=dict(ENTITY_MAP),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)


class DocumentReadRehydrationTests(_AuthedTenantMixin, TestCase):
    def test_get_detail_rehydrates_markdown(self):
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="trip",
            title="Trip",
            markdown="Book hotel for [LOCATION_330]",
        )
        resp = self.client.get("/api/v1/journal/documents/project/trip/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["markdown"], "Book hotel for july")
        self.assertNotIn("[LOCATION_330]", resp.data["markdown"])

    def test_stored_markdown_stays_redacted(self):
        # The serve boundary rehydrates the RESPONSE, never the row.
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="trip",
            title="Trip",
            markdown="Book hotel for [LOCATION_330]",
        )
        self.client.get("/api/v1/journal/documents/project/trip/")
        row = Document.objects.get(tenant=self.tenant, kind="project", slug="trip")
        self.assertEqual(row.markdown, "Book hotel for [LOCATION_330]")

    def test_missing_tenant_context_falls_back_to_raw(self):
        # Defensive contract on the serializer: no tenant in context => today's
        # behaviour (raw markdown), not a crash.
        from apps.journal.document_serializers import DocumentSerializer

        doc = Document(kind="project", slug="x", title="x", markdown="Hi [PERSON_1]")
        self.assertEqual(DocumentSerializer(doc).data["markdown"], "Hi [PERSON_1]")


class ReplyArtifactOwnerRoundTripTests(_AuthedTenantMixin, TestCase):
    def test_artifact_stays_placeholder_space_and_owner_round_trip_reredacts(self):
        from apps.journal.reply_artifacts import upsert_reply_artifact

        doc = upsert_reply_artifact(
            tenant=self.tenant,
            source="ios",
            dedup_key="owner-round-trip",
            title="Table — [PERSON_1]",
            markdown="| Person |\n| --- |\n| [PERSON_1] |",
        )
        stored = Document.objects.get(id=doc.id)
        self.assertIn("[PERSON_1]", stored.title)
        self.assertIn("[PERSON_1]", stored.markdown)
        self.assertNotIn("Sarah", stored.markdown)

        response = self.client.get(f"/api/v1/journal/documents/project/{doc.slug}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Table — Sarah")
        self.assertIn("| Sarah |", response.data["markdown"])

        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            edited = self.client.patch(
                f"/api/v1/journal/documents/project/{doc.slug}/",
                {"title": "Table — Sarah edited", "markdown": "Sarah updated the table"},
                format="json",
            )
        self.assertEqual(edited.status_code, 200)
        stored.refresh_from_db()
        self.assertIn("[PERSON_1]", stored.title)
        self.assertIn("[PERSON_1]", stored.markdown)
        self.assertNotIn("Sarah", stored.markdown)

    def test_artifact_helper_works_with_current_journal_encryption_flag_both_ways(self):
        from apps.journal.reply_artifacts import upsert_reply_artifact

        for enabled in (False, True):
            with self.subTest(encrypt_journal_writes=enabled):
                self.tenant.encrypt_journal_writes = enabled
                self.tenant.save(update_fields=["encrypt_journal_writes"])
                doc = upsert_reply_artifact(
                    tenant=self.tenant,
                    source="ios",
                    dedup_key=f"encryption-{enabled}",
                    title="Encrypted rollout compatibility",
                    markdown="placeholder-space body",
                )
                self.assertEqual(Document.objects.get(id=doc.id).markdown.splitlines()[-1], "placeholder-space body")


@override_settings(NBHD_INTERNAL_API_KEY="artifact-runtime-key")
class ReplyArtifactRuntimeRetrievalTests(TestCase):
    def setUp(self):
        from apps.tenants.test_utils import seed_internal_key

        self.user = User.objects.create_user(username="artifact-runtime", password="x")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        seed_internal_key(self.tenant, key="artifact-runtime-key")
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "artifact-runtime-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_new_artifact_is_immediately_searchable_and_get_returns_full_content(self):
        from apps.journal.reply_artifacts import upsert_reply_artifact

        doc = upsert_reply_artifact(
            tenant=self.tenant,
            source="ios",
            dedup_key="runtime-retrieval",
            title="Table — Nebula inventory",
            markdown="| Item | Status |\n| --- | --- |\n| zephyrite | ready |",
        )
        search = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/journal/search/",
            {"q": "zephyrite"},
            **self.headers,
        )
        self.assertEqual(search.status_code, 200)
        self.assertIn(doc.slug, [result["slug"] for result in search.json()["results"]])

        fetched = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/document/",
            {"kind": "project", "slug": doc.slug},
            **self.headers,
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["id"], str(doc.id))
        self.assertIn("| zephyrite | ready |", fetched.json()["markdown"])


class DocumentWriteRedactionTests(_AuthedTenantMixin, TestCase):
    def test_patch_round_trip_reredacts_then_rehydrates(self):
        # Owner PATCHes back the rehydrated real value ("july"). It must be
        # re-redacted to the existing placeholder before storage, and the next
        # GET must surface the real value again.
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            patch_resp = self.client.patch(
                "/api/v1/journal/documents/daily/2026-07-09/",
                {"markdown": "Dinner with july tonight"},
                format="json",
            )
        self.assertEqual(patch_resp.status_code, 200)

        # Stored row is back in placeholder space — real value never persisted.
        row = Document.objects.get(tenant=self.tenant, kind="daily", slug="2026-07-09")
        self.assertIn("[LOCATION_330]", row.markdown)
        self.assertNotIn("july", row.markdown)

        # PATCH response itself is already rehydrated for the owner.
        self.assertIn("july", patch_resp.data["markdown"])
        self.assertNotIn("[LOCATION_330]", patch_resp.data["markdown"])

        # A fresh GET rehydrates the placeholder back to the real value.
        get_resp = self.client.get("/api/v1/journal/documents/daily/2026-07-09/")
        self.assertEqual(get_resp.status_code, 200)
        self.assertIn("july", get_resp.data["markdown"])
        self.assertNotIn("[LOCATION_330]", get_resp.data["markdown"])

    def test_append_reredacts_content(self):
        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug="2026-07-09",
            title="2026-07-09",
            markdown="# 2026-07-09\n",
        )
        with patch("apps.pii.redactor._detect_pii", return_value=[]):
            resp = self.client.post(
                "/api/v1/journal/documents/daily/2026-07-09/append/",
                {"content": "Coffee with july"},
                format="json",
            )
        self.assertEqual(resp.status_code, 201)
        row = Document.objects.get(tenant=self.tenant, kind="daily", slug="2026-07-09")
        self.assertIn("[LOCATION_330]", row.markdown)
        self.assertNotIn("july", row.markdown)
        # Owner still sees the real value in the response.
        self.assertIn("july", resp.data["markdown"])


class SidebarRehydrationTests(_AuthedTenantMixin, TestCase):
    def test_tree_titles_rehydrated(self):
        Document.objects.create(
            tenant=self.tenant,
            kind="project",
            slug="sarah-project",
            title="[PERSON_1] project",
            markdown="",
        )
        resp = self.client.get("/api/v1/journal/tree/")
        self.assertEqual(resp.status_code, 200)
        projects = next(group["items"] for group in resp.data if group["kind"] == "project")
        titles = [item["title"] for item in projects]
        self.assertIn("Sarah project", titles)
        for t in titles:
            self.assertNotIn("[PERSON_1]", t)


class StatusViewRehydrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="statususer", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            experimental_typed_journal_lifecycle=True,
            finance_enabled=False,
            pii_entity_map=dict(ENTITY_MAP),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_status_view_rehydrates_task_title(self):
        receipt = {
            "state": "placeholder",
            "redactions": [{"placeholder": "[PERSON_1]", "value": "Sarah"}],
        }
        Task.objects.create(
            tenant=self.tenant,
            title="Call [PERSON_1]",
            status=Task.Status.OPEN,
            pii_receipts={"title": receipt},
        )
        resp = self.client.get("/api/v1/journal/status/")
        self.assertEqual(resp.status_code, 200)
        titles = [t["title"] for t in resp.data["open_tasks"]]
        self.assertIn("Call Sarah", titles)
        for t in titles:
            self.assertNotIn("[PERSON_1]", t)
        self.assertEqual(resp.data["open_tasks"][0]["pii_receipts"]["title"], receipt)

    def test_status_view_rehydrates_goal_title(self):
        Goal.objects.create(tenant=self.tenant, title="Visit [LOCATION_330]", status=Goal.Status.ACTIVE)
        resp = self.client.get("/api/v1/journal/status/")
        titles = [g["title"] for g in resp.data["active_goals"]]
        self.assertIn("Visit july", titles)

    def test_build_journal_status_stays_redacted(self):
        # The projection is ALSO consumed by the OpenClaw runtime context; it
        # must stay in placeholder space. Rehydration lives only at the view.
        Task.objects.create(tenant=self.tenant, title="Call [PERSON_1]", status=Task.Status.OPEN)
        payload = build_journal_status(self.tenant, date(2026, 7, 9))
        titles = [t["title"] for t in payload["open_tasks"]]
        self.assertIn("Call [PERSON_1]", titles)
        self.assertNotIn("Call Sarah", titles)


class LifecycleSerializerRehydrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lifeuser", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status="active",
            experimental_typed_journal_lifecycle=True,
            pii_entity_map=dict(ENTITY_MAP),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_task_detail_rehydrates_title_and_description(self):
        receipt = {
            "state": "placeholder",
            "redactions": [{"placeholder": "[PERSON_1]", "value": "Sarah"}],
        }
        task = Task.objects.create(
            tenant=self.tenant,
            title="Call [PERSON_1]",
            description="about [LOCATION_330]",
            status=Task.Status.OPEN,
            pii_receipts={"title": receipt},
        )
        resp = self.client.get(f"/api/v1/journal/tasks/{task.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["title"], "Call Sarah")
        self.assertEqual(resp.data["description"], "about july")
        self.assertEqual(resp.data["pii_receipts"]["title"], receipt)

    def test_goal_detail_rehydrates_title(self):
        goal = Goal.objects.create(tenant=self.tenant, title="Support [PERSON_1]", status=Goal.Status.ACTIVE)
        resp = self.client.get(f"/api/v1/journal/goals/{goal.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["title"], "Support Sarah")

    def test_serializer_without_rehydrate_context_stays_redacted(self):
        # The runtime/agent path instantiates the same serializer WITHOUT the
        # opt-in flag; those fields must stay in placeholder space for the model.
        from apps.journal.lifecycle_serializers import TaskSerializer

        task = Task.objects.create(tenant=self.tenant, title="Call [PERSON_1]", status=Task.Status.OPEN)
        self.assertEqual(TaskSerializer(task).data["title"], "Call [PERSON_1]")
        self.assertNotIn("pii_receipts", TaskSerializer(task).data)
        # Even with tenant present (the agent write path passes tenant), absence
        # of rehydrate keeps it redacted.
        self.assertEqual(TaskSerializer(task, context={"tenant": self.tenant}).data["title"], "Call [PERSON_1]")
        self.assertNotIn("pii_receipts", TaskSerializer(task, context={"tenant": self.tenant}).data)
