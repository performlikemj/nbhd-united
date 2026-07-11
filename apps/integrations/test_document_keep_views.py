"""Runtime endpoint tests for document keep / list / forget.

Auth pattern matches the other runtime endpoints: HTTP_X_NBHD_INTERNAL_KEY +
HTTP_X_NBHD_TENANT_ID headers; tenant scope must match the URL tenant_id. The
business logic is covered by apps.journal.test_document_ingestion; these pin the
HMAC-authed wrappers, the request/response shape, and status codes.
"""

from __future__ import annotations

import uuid

from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.models import Document, DocumentIngestion, Task
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class DocumentKeepViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="KeepView", telegram_chat_id=970001)
        seed_internal_key(self.tenant)
        self.other = create_tenant(display_name="OtherView", telegram_chat_id=970002)

    def _headers(self, tenant_id=None, key="shared-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _keep_url(self):
        return f"/api/v1/integrations/runtime/{self.tenant.id}/documents/keep/"

    def test_keep_requires_internal_auth(self):
        resp = self.client.post(self._keep_url(), data={"source": {}, "artifacts": []}, content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    def test_keep_rejects_tenant_scope_mismatch(self):
        resp = self.client.post(
            self._keep_url(),
            data={"source": {"original_filename": "x.pdf"}, "artifacts": []},
            content_type="application/json",
            **self._headers(tenant_id=str(self.other.id)),
        )
        self.assertEqual(resp.status_code, 401)

    def test_keep_records_valid_artifacts(self):
        task = Task.objects.create(tenant=self.tenant, title="Pay rent")
        resp = self.client.post(
            self._keep_url(),
            data={
                "source": {"original_filename": "invoice.pdf", "content_hash": "ab12cd34"},
                "artifacts": [
                    {"kind": "task", "object_type": "journal.Task", "object_id": str(task.id), "excerpt": "pay rent"}
                ],
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["recorded"], 1)
        self.assertEqual(body["errors"], [])
        self.assertTrue(DocumentIngestion.objects.filter(tenant=self.tenant, id=body["ingestion_id"]).exists())

    def test_keep_returns_errors_for_bad_ref_without_recording(self):
        resp = self.client.post(
            self._keep_url(),
            data={
                "source": {"original_filename": "invoice.pdf"},
                "artifacts": [{"object_type": "journal.Task", "object_id": str(uuid.uuid4())}],
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["recorded"], 0)
        self.assertIsNone(body["ingestion_id"])
        self.assertEqual(body["errors"][0]["reason"], "not_found")

    def test_keep_records_a_gmail_sourced_manifest(self):
        # Phase 5: the endpoint accepts the email source shape and validates
        # artifacts exactly like a document's (same pass-through to record_keep).
        task = Task.objects.create(tenant=self.tenant, title="Reply to Kenji")
        resp = self.client.post(
            self._keep_url(),
            data={
                "source": {
                    "source_kind": "email",
                    "source_ref": "gmail:196f3a2b1c",
                    "original_filename": "Invoice #4471",
                },
                "artifacts": [{"object_type": "journal.Task", "object_id": str(task.id), "excerpt": "due Oct 31"}],
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["recorded"], 1)
        ingestion = DocumentIngestion.objects.get(tenant=self.tenant, id=body["ingestion_id"])
        self.assertEqual(ingestion.source_kind, "email")
        self.assertEqual(ingestion.source_ref, "gmail:196f3a2b1c")
        self.assertIsNone(ingestion.file_expires_at)

    def test_keep_refuses_a_malformed_email_source(self):
        task = Task.objects.create(tenant=self.tenant, title="valid artifact, bad source")
        resp = self.client.post(
            self._keep_url(),
            data={
                "source": {"source_kind": "email", "source_ref": "not-a-gmail-ref", "original_filename": "x"},
                "artifacts": [{"object_type": "journal.Task", "object_id": str(task.id)}],
            },
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["recorded"], 0)
        self.assertIsNone(body["ingestion_id"])
        self.assertEqual(body["errors"][0]["reason"], "invalid_source")
        self.assertFalse(DocumentIngestion.objects.filter(tenant=self.tenant).exists())


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class DocumentListForgetViewTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="LFView", telegram_chat_id=980001)
        seed_internal_key(self.tenant)

    def _headers(self):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": "shared-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _seed_ingestion(self):
        from apps.journal.document_ingestion import record_keep

        task = Task.objects.create(tenant=self.tenant, title="A task")
        doc = Document.objects.create(tenant=self.tenant, kind="project", slug="p1", title="P", markdown="x")
        return record_keep(
            self.tenant,
            source={"original_filename": "plan.pdf"},
            artifacts=[
                {"object_type": "journal.Task", "object_id": str(task.id), "destination": "Task"},
                {"object_type": "journal.Document", "object_id": str(doc.id), "destination": "Note"},
            ],
        )["ingestion_id"]

    def test_list_returns_ingestions(self):
        ingestion = self._seed_ingestion()
        resp = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/documents/ingestions/",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["ingestions"][0]["id"], ingestion)
        self.assertEqual(len(body["ingestions"][0]["artifacts"]), 2)

    def test_forget_removes_and_reports(self):
        ingestion = self._seed_ingestion()
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/documents/{ingestion}/forget/",
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["removed"], 2)
        self.assertEqual(body["status"], "removed")
        self.assertTrue(body["caveats"])
        self.assertEqual(len(body["results"]), 2)
        self.assertFalse(Task.objects.filter(tenant=self.tenant).exists())

    def test_forget_unknown_ingestion_returns_404(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/documents/{uuid.uuid4()}/forget/",
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(resp.status_code, 404)
