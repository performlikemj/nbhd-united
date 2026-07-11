"""Email/calendar/Reddit ingestion provenance (continuity-directive P3, Phase 5).

Extends the LIVE document-keeping ledger to information the assistant kept from a
NON-upload source it read. Same validated manifest, same forget fan-out — only the
source identity differs: a namespaced ``source_ref`` (``gmail:<id>`` …) in place of a
filename, and no ephemeral file to expire. These tests pin:

- a valid gmail-sourced manifest records with ``source_kind="email"`` + ``source_ref``,
  no ``file_expires_at``, and validates artifacts EXACTLY like a document's;
- a malformed email source is refused (never recorded) — the ledger can't hold an
  ungroupable row that "forget everything from that email" couldn't act on;
- 0-collateral forget across a MIX of an upload ingestion and an email ingestion;
- the list + forget rendering stays honest (no false "file expired"; source-aware caveat).
"""

from __future__ import annotations

import uuid

from django.test import TestCase

from apps.journal.document_ingestion import forget_ingestion, list_ingestions, record_keep
from apps.journal.models import DocumentIngestion, Task
from apps.tenants.services import create_tenant


def _mk_task(tenant, title="Task"):
    return Task.objects.create(tenant=tenant, title=title)


class EmailKeepValidationTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Email Keep", telegram_chat_id=940001)

    def test_valid_gmail_manifest_records_with_source_identity(self):
        task = _mk_task(self.tenant, title="Pay Rakuten")
        with self.assertLogs("apps.journal.document_ingestion", level="INFO") as cm:
            result = record_keep(
                self.tenant,
                source={
                    "source_kind": "email",
                    "source_ref": "gmail:196f3a2b1c",
                    "original_filename": "Invoice #4471",
                },
                artifacts=[{"object_type": "journal.Task", "object_id": str(task.id), "excerpt": "¥82,300 due Oct 31"}],
            )
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(result["errors"], [])
        ingestion = DocumentIngestion.objects.get(id=result["ingestion_id"])
        self.assertEqual(ingestion.source_kind, "email")
        self.assertEqual(ingestion.source_ref, "gmail:196f3a2b1c")
        self.assertEqual(ingestion.original_filename, "Invoice #4471")
        # No ephemeral file → no expiry (the agent must not claim it clears out).
        self.assertIsNone(ingestion.file_expires_at)
        # The artifact validated + recorded exactly as a document's would.
        art = ingestion.artifacts.get()
        self.assertEqual(art.object_id, str(task.id))
        self.assertEqual(art.removal_strategy, "row_delete")
        self.assertEqual(art.content_excerpt, "¥82,300 due Oct 31")
        self.assertTrue(any("ingest_provenance_stamped" in line and "source_kind=email" in line for line in cm.output))

    def test_valid_reddit_and_calendar_refs(self):
        for kind, ref in (("reddit", "reddit:t3_abc123"), ("calendar", "gcal:evt_9981")):
            task = _mk_task(self.tenant, title=f"from {kind}")
            result = record_keep(
                self.tenant,
                source={"source_kind": kind, "source_ref": ref, "original_filename": f"{kind} label"},
                artifacts=[{"object_type": "journal.Task", "object_id": str(task.id)}],
            )
            self.assertEqual(result["recorded"], 1, kind)
            self.assertEqual(DocumentIngestion.objects.get(id=result["ingestion_id"]).source_kind, kind)

    def test_malformed_email_source_is_refused_never_recorded(self):
        task = _mk_task(self.tenant)  # a real, valid artifact — but the source is bad
        for bad_ref in ("", "gmail:", "   ", "196f3a2b1c", "notgmail:196f3a"):
            with self.assertLogs("apps.journal.document_ingestion", level="INFO") as cm:
                result = record_keep(
                    self.tenant,
                    source={"source_kind": "email", "source_ref": bad_ref, "original_filename": "x"},
                    artifacts=[{"object_type": "journal.Task", "object_id": str(task.id)}],
                )
            self.assertEqual(result["recorded"], 0, bad_ref)
            self.assertIsNone(result["ingestion_id"], bad_ref)
            self.assertEqual(result["errors"][0]["reason"], "invalid_source", bad_ref)
            self.assertTrue(any("ingest_write_blocked" in line for line in cm.output), bad_ref)
        # Nothing recorded across all the bad refs.
        self.assertFalse(DocumentIngestion.objects.filter(tenant=self.tenant).exists())

    def test_unknown_source_kind_is_refused(self):
        result = record_keep(
            self.tenant,
            source={"source_kind": "sms", "source_ref": "sms:123", "original_filename": "x"},
            artifacts=[],
        )
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["errors"][0]["reason"], "invalid_source_kind")

    def test_bad_reddit_fullname_is_refused(self):
        result = record_keep(
            self.tenant,
            source={"source_kind": "reddit", "source_ref": "reddit:abc", "original_filename": "x"},
            artifacts=[],
        )
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["errors"][0]["reason"], "invalid_source")

    def test_email_keep_validates_artifacts_exactly_like_a_document(self):
        # A bad artifact under an email source errors per-artifact, same as an upload.
        result = record_keep(
            self.tenant,
            source={"source_kind": "email", "source_ref": "gmail:abc", "original_filename": "x"},
            artifacts=[{"object_type": "journal.Task", "object_id": str(uuid.uuid4())}],
        )
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["errors"][0]["reason"], "not_found")


class MixedSourceForgetTest(TestCase):
    """0-collateral forget across an upload ingestion and an email ingestion."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Mixed Forget", telegram_chat_id=940002)

    def test_forget_email_leaves_the_uploaded_documents_items_untouched(self):
        upload_tasks = [_mk_task(self.tenant, title="upload") for _ in range(3)]
        upload = record_keep(
            self.tenant,
            source={"original_filename": "invoice-oct.pdf"},  # upload (default source_kind)
            artifacts=[{"object_type": "journal.Task", "object_id": str(t.id)} for t in upload_tasks],
        )
        email_tasks = [_mk_task(self.tenant, title="email") for _ in range(2)]
        email = record_keep(
            self.tenant,
            source={"source_kind": "email", "source_ref": "gmail:zz99", "original_filename": "Invoice #4471"},
            artifacts=[{"object_type": "journal.Task", "object_id": str(t.id)} for t in email_tasks],
        )

        result = forget_ingestion(self.tenant, email["ingestion_id"])

        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["status"], DocumentIngestion.Status.REMOVED)
        # The email's two tasks are gone…
        for t in email_tasks:
            self.assertFalse(Task.objects.filter(id=t.id).exists())
        # …and the uploaded document's three are untouched.
        for t in upload_tasks:
            self.assertTrue(Task.objects.filter(id=t.id).exists())
        self.assertEqual(DocumentIngestion.objects.get(id=upload["ingestion_id"]).status, DocumentIngestion.Status.KEPT)

    def test_email_forget_caveat_names_the_email_and_its_redaction_posture(self):
        task = _mk_task(self.tenant)
        kept = record_keep(
            self.tenant,
            source={"source_kind": "email", "source_ref": "gmail:cav", "original_filename": "Invoice"},
            artifacts=[{"object_type": "journal.Task", "object_id": str(task.id)}],
        )
        result = forget_ingestion(self.tenant, kept["ingestion_id"])
        joined = " ".join(result["caveats"]).lower()
        self.assertIn("email", joined)
        self.assertIn("already reached the ai model", joined)
        self.assertIn("people settings", joined)


class EmailListRenderingTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Email List", telegram_chat_id=940003)

    def test_list_exposes_source_kind_and_never_claims_email_expired(self):
        task = _mk_task(self.tenant)
        record_keep(
            self.tenant,
            source={"source_kind": "email", "source_ref": "gmail:list1", "original_filename": "Weekly digest"},
            artifacts=[{"object_type": "journal.Task", "object_id": str(task.id)}],
        )
        out = list_ingestions(self.tenant)
        row = out["ingestions"][0]
        self.assertEqual(row["source_kind"], "email")
        self.assertEqual(row["source_ref"], "gmail:list1")
        self.assertEqual(row["original_filename"], "Weekly digest")
        self.assertFalse(row["file_expired"])  # no ephemeral file → never "expired"
