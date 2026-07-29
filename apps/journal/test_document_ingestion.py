"""Document information-keeping service tests (Phase 2 — D2/D3/D4/§5).

Covers the critic-hardened invariants: VALIDATED provenance (keep refuses
hallucinated/stale/wrong-tenant/unregistered ids per-artifact), the 0-collateral
forget contract, idempotent re-entrant partial-failure retry, reminder removal that
actually stops the fire under BOTH postgres_cron_canonical states, the completeness
gap signal, and tenant scoping.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.test import TestCase

from apps.cron.gateway_client import GatewayError
from apps.cron.models import CronJob
from apps.journal.document_ingestion import (
    REMOVAL_HANDLERS,
    forget_ingestion,
    list_ingestions,
    record_keep,
)
from apps.journal.models import (
    Document,
    DocumentChunk,
    DocumentIngestion,
    DocumentIngestionArtifact,
    Goal,
    Task,
)
from apps.tenants.services import create_tenant


def _mk_document(tenant, *, kind="project", slug=None, title="Doc"):
    return Document.objects.create(
        tenant=tenant,
        kind=kind,
        slug=slug or f"doc-{uuid.uuid4().hex[:8]}",
        title=title,
        markdown="body",
    )


def _mk_task(tenant, title="Task"):
    return Task.objects.create(tenant=tenant, title=title)


def _mk_goal(tenant, title="Goal"):
    return Goal.objects.create(tenant=tenant, title=title)


def _mk_reminder(tenant, name):
    return CronJob.objects.create(tenant=tenant, name=name, data={"name": name})


class RemovalRegistryTest(TestCase):
    def test_v1_registry_covers_exactly_the_four_core_destinations(self):
        self.assertEqual(
            set(REMOVAL_HANDLERS),
            {"journal.Document", "journal.Task", "journal.Goal", "cron.CronJob"},
        )
        self.assertEqual(REMOVAL_HANDLERS["cron.CronJob"].strategy, "cron_delete")
        self.assertEqual(REMOVAL_HANDLERS["journal.Document"].strategy, "row_delete")


class KeepValidationTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Keep", telegram_chat_id=910001)
        self.other = create_tenant(display_name="Other", telegram_chat_id=910002)

    def test_unregistered_object_type_returns_error_not_recorded(self):
        result = record_keep(
            self.tenant,
            source={"original_filename": "x.pdf"},
            artifacts=[{"kind": "insight", "object_type": "insights.AssistantInsight", "object_id": str(uuid.uuid4())}],
        )
        self.assertEqual(result["recorded"], 0)
        self.assertIsNone(result["ingestion_id"])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["reason"], "unregistered_type")
        self.assertFalse(DocumentIngestion.objects.filter(tenant=self.tenant).exists())

    def test_nonexistent_object_id_returns_not_found(self):
        result = record_keep(
            self.tenant,
            source={"original_filename": "x.pdf"},
            artifacts=[{"object_type": "journal.Task", "object_id": str(uuid.uuid4())}],
        )
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["errors"][0]["reason"], "not_found")

    def test_wrong_tenant_object_id_rejected_as_not_found(self):
        foreign_task = _mk_task(self.other)
        result = record_keep(
            self.tenant,
            source={"original_filename": "x.pdf"},
            artifacts=[{"object_type": "journal.Task", "object_id": str(foreign_task.id)}],
        )
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["errors"][0]["reason"], "not_found")

    def test_daily_document_is_not_recordable(self):
        daily = _mk_document(self.tenant, kind="daily", slug="2026-07-09")
        result = record_keep(
            self.tenant,
            source={"original_filename": "x.pdf"},
            artifacts=[{"object_type": "journal.Document", "object_id": str(daily.id)}],
        )
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["errors"][0]["reason"], "not_found")

    def test_mixed_valid_and_invalid_is_per_artifact_not_all_or_nothing(self):
        good = _mk_task(self.tenant)
        result = record_keep(
            self.tenant,
            source={"original_filename": "x.pdf"},
            artifacts=[
                {"object_type": "journal.Task", "object_id": str(good.id), "excerpt": "buy milk"},
                {"object_type": "journal.Goal", "object_id": str(uuid.uuid4())},
            ],
        )
        self.assertEqual(result["recorded"], 1)
        self.assertEqual(len(result["errors"]), 1)
        ingestion = DocumentIngestion.objects.get(tenant=self.tenant)
        self.assertEqual(ingestion.artifacts.count(), 1)
        art = ingestion.artifacts.get()
        self.assertEqual(art.object_id, str(good.id))
        self.assertEqual(art.removal_strategy, "row_delete")
        self.assertEqual(art.content_excerpt, "buy milk")

    def test_bad_ref_emits_telemetry(self):
        with self.assertLogs("apps.journal.document_ingestion", level="INFO") as cm:
            record_keep(
                self.tenant,
                source={"original_filename": "x.pdf"},
                artifacts=[{"object_type": "journal.Task", "object_id": str(uuid.uuid4())}],
            )
        self.assertTrue(any("doc_ingest_bad_ref" in line for line in cm.output))


class ForgetContractTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Forget", telegram_chat_id=920001)

    def _keep(self, artifacts):
        result = record_keep(self.tenant, source={"original_filename": "src.pdf"}, artifacts=artifacts)
        return result["ingestion_id"]

    def test_forget_removes_only_that_documents_items(self):
        a_docs = [_mk_document(self.tenant) for _ in range(2)]
        a_tasks = [_mk_task(self.tenant) for _ in range(2)]
        a_goals = [_mk_goal(self.tenant) for _ in range(2)]
        ingestion_a = self._keep(
            [{"object_type": "journal.Document", "object_id": str(d.id)} for d in a_docs]
            + [{"object_type": "journal.Task", "object_id": str(t.id)} for t in a_tasks]
            + [{"object_type": "journal.Goal", "object_id": str(g.id)} for g in a_goals]
        )
        b_docs = [_mk_document(self.tenant) for _ in range(2)]
        b_tasks = [_mk_task(self.tenant) for _ in range(2)]
        self._keep(
            [{"object_type": "journal.Document", "object_id": str(d.id)} for d in b_docs]
            + [{"object_type": "journal.Task", "object_id": str(t.id)} for t in b_tasks]
        )

        result = forget_ingestion(self.tenant, ingestion_a)

        self.assertEqual(result["removed"], 6)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["status"], DocumentIngestion.Status.REMOVED)
        # A's destination rows are gone…
        for d in a_docs:
            self.assertFalse(Document.objects.filter(id=d.id).exists())
        for t in a_tasks:
            self.assertFalse(Task.objects.filter(id=t.id).exists())
        for g in a_goals:
            self.assertFalse(Goal.objects.filter(id=g.id).exists())
        # …and B's four are untouched.
        for d in b_docs:
            self.assertTrue(Document.objects.filter(id=d.id).exists())
        for t in b_tasks:
            self.assertTrue(Task.objects.filter(id=t.id).exists())

    def test_forget_cascades_document_chunks(self):
        doc = _mk_document(self.tenant)
        DocumentChunk.objects.create(
            tenant=self.tenant, document=doc, chunk_index=0, text="chunk", embedding=[0.0] * 1536
        )
        ingestion = self._keep([{"object_type": "journal.Document", "object_id": str(doc.id)}])
        forget_ingestion(self.tenant, ingestion)
        self.assertFalse(DocumentChunk.objects.filter(document_id=doc.id).exists())

    def test_forget_is_idempotent_and_reentrant(self):
        task = _mk_task(self.tenant)
        ingestion = self._keep([{"object_type": "journal.Task", "object_id": str(task.id)}])
        first = forget_ingestion(self.tenant, ingestion)
        self.assertEqual(first["removed"], 1)
        # Second run: the row is already removed → no-op, reported as removed.
        second = forget_ingestion(self.tenant, ingestion)
        self.assertEqual(second["removed"], 0)
        self.assertEqual(second["failed"], 0)
        self.assertEqual(second["status"], DocumentIngestion.Status.REMOVED)
        self.assertTrue(second["results"][0]["removed"])

    def test_object_gone_by_other_means_is_success_not_error(self):
        task = _mk_task(self.tenant)
        ingestion = self._keep([{"object_type": "journal.Task", "object_id": str(task.id)}])
        task.delete()  # deleted since keep, by other means
        result = forget_ingestion(self.tenant, ingestion)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["failed"], 0)

    def test_content_excerpt_survives_deletion(self):
        task = _mk_task(self.tenant, title="Pay Rakuten")
        ingestion = self._keep(
            [{"object_type": "journal.Task", "object_id": str(task.id), "excerpt": "Rakuten ¥82,300 due Oct 31"}]
        )
        forget_ingestion(self.tenant, ingestion)
        art = DocumentIngestionArtifact.objects.get(ingestion_id=ingestion)
        self.assertEqual(art.content_excerpt, "Rakuten ¥82,300 due Oct 31")

    def test_forget_reports_honesty_caveats(self):
        rem = _mk_reminder(self.tenant, "remind-1")
        ingestion = self._keep([{"object_type": "cron.CronJob", "object_id": rem.name}])
        with patch("apps.cron.gateway_client.invoke_gateway_tool"), patch("apps.cron.signals._enqueue_regen"):
            result = forget_ingestion(self.tenant, ingestion)
        joined = " ".join(result["caveats"]).lower()
        self.assertIn("already reached the ai model", joined)
        self.assertIn("people settings", joined)
        self.assertIn("history", joined)  # the reminder caveat

    def test_forget_unknown_ingestion_returns_not_found(self):
        result = forget_ingestion(self.tenant, uuid.uuid4())
        self.assertEqual(result.get("error"), "not_found")

    def test_forget_emits_telemetry(self):
        task = _mk_task(self.tenant)
        ingestion = self._keep([{"object_type": "journal.Task", "object_id": str(task.id)}])
        with self.assertLogs("apps.journal.document_ingestion", level="INFO") as cm:
            forget_ingestion(self.tenant, ingestion)
        self.assertTrue(any("doc_ingest_forget" in line for line in cm.output))


class ReminderRemovalBothFlagStatesTest(TestCase):
    """Reminder forget must stop the fire under BOTH postgres_cron_canonical states
    (directive finding-4): delete the Postgres desired-state row AND remove the job
    directly from the gateway. Assert the gateway cron.remove path is hit, not just
    the Postgres row delete.
    """

    gateway_job_id = "journal-reminder-id"

    def setUp(self):
        self.tenant = create_tenant(display_name="Reminder", telegram_chat_id=930001)

    def _forget_one_reminder(self):
        rem = _mk_reminder(self.tenant, "trash-tue")
        result = record_keep(
            self.tenant,
            source={"original_filename": "cal.pdf"},
            artifacts=[{"object_type": "cron.CronJob", "object_id": rem.name, "excerpt": "trash Tue 8am"}],
        )
        return rem.name, result["ingestion_id"]

    def _assert_gateway_remove_called(self, mock_invoke):
        remove_calls = [c for c in mock_invoke.call_args_list if len(c.args) >= 2 and c.args[1] == "cron.remove"]
        self.assertTrue(remove_calls, "gateway cron.remove was never invoked")
        self.assertTrue(any(c.args[2].get("jobId") == self.gateway_job_id for c in remove_calls))

    def test_canonical_flag_true_deletes_row_and_hits_gateway(self):
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        name, ingestion = self._forget_one_reminder()
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.signals._enqueue_regen"),
        ):
            mock_invoke.side_effect = [
                {"details": {"jobs": [{"id": self.gateway_job_id, "name": name}]}},
                {},
            ]
            result = forget_ingestion(self.tenant, ingestion)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name=name).exists())
        self._assert_gateway_remove_called(mock_invoke)

    def test_canonical_flag_false_deletes_row_and_hits_gateway(self):
        self.tenant.postgres_cron_canonical = False
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        name, ingestion = self._forget_one_reminder()
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool") as mock_invoke,
            patch("apps.cron.signals._enqueue_regen"),
        ):
            mock_invoke.side_effect = [
                {"details": {"jobs": [{"id": self.gateway_job_id, "name": name}]}},
                {},
            ]
            result = forget_ingestion(self.tenant, ingestion)
        self.assertEqual(result["removed"], 1)
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name=name).exists())
        self._assert_gateway_remove_called(mock_invoke)

    def test_gateway_unavailable_on_canonical_tenant_is_treated_as_removed(self):
        self.tenant.postgres_cron_canonical = True
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        name, ingestion = self._forget_one_reminder()
        exc = GatewayError("container hibernated")
        exc.unavailable = True
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", side_effect=exc),
            patch("apps.cron.signals._enqueue_regen"),
        ):
            result = forget_ingestion(self.tenant, ingestion)
        # Desired-state row deleted + wake reconcile authoritative → removed, not failed.
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name=name).exists())

    def test_gateway_hard_error_marks_artifact_failed_and_retryable(self):
        self.tenant.postgres_cron_canonical = False
        self.tenant.save(update_fields=["postgres_cron_canonical"])
        name, ingestion = self._forget_one_reminder()
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", side_effect=GatewayError("boom")),
            patch("apps.cron.signals._enqueue_regen"),
        ):
            result = forget_ingestion(self.tenant, ingestion)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["status"], DocumentIngestion.Status.PARTIALLY_REMOVED)
        art = DocumentIngestionArtifact.objects.get(ingestion_id=ingestion)
        self.assertIsNone(art.removed_at)
        self.assertTrue(art.last_error)
        # Retry after the gateway recovers succeeds (re-entrant).
        with patch("apps.cron.gateway_client.invoke_gateway_tool"), patch("apps.cron.signals._enqueue_regen"):
            retry = forget_ingestion(self.tenant, ingestion)
        self.assertEqual(retry["removed"], 1)
        self.assertEqual(retry["status"], DocumentIngestion.Status.REMOVED)


# NOTE: the completeness gap signal + thread/uploaded_at binding depend on the
# document-upload AppChatMessage carrying `attachment_path` (a stored doc_<hash>
# file) — a state ONLY the real chat ingress produces. Those tests therefore drive
# the real POST /api/v1/chat/messages/ ingress in
# apps.integrations.test_document_write_backstop, not a fabricated row here.


class TenantScopingTest(TestCase):
    def setUp(self):
        self.tenant_a = create_tenant(display_name="A", telegram_chat_id=950001)
        self.tenant_b = create_tenant(display_name="B", telegram_chat_id=950002)

    def test_second_tenant_cannot_forget_or_list_first_tenants_ingestion(self):
        task = _mk_task(self.tenant_a)
        ingestion = record_keep(
            self.tenant_a,
            source={"original_filename": "a.pdf"},
            artifacts=[{"object_type": "journal.Task", "object_id": str(task.id)}],
        )["ingestion_id"]

        # B cannot forget A's ingestion.
        self.assertEqual(forget_ingestion(self.tenant_b, ingestion).get("error"), "not_found")
        self.assertTrue(Task.objects.filter(id=task.id).exists())  # untouched

        # B's list does not include A's ingestion; A's does.
        self.assertEqual(list_ingestions(self.tenant_b)["count"], 0)
        a_list = list_ingestions(self.tenant_a)
        self.assertEqual(a_list["count"], 1)
        self.assertEqual(a_list["ingestions"][0]["id"], ingestion)


class ListIngestionsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="List", telegram_chat_id=960001)

    def test_list_reports_artifacts_and_expiry_flag(self):
        task = _mk_task(self.tenant)
        record_keep(
            self.tenant,
            source={"original_filename": "receipt.pdf"},
            artifacts=[
                {
                    "object_type": "journal.Task",
                    "object_id": str(task.id),
                    "destination": "A task",
                    "excerpt": "buy milk",
                }
            ],
        )
        # Force the file to have expired.
        ingestion = DocumentIngestion.objects.get(tenant=self.tenant)
        ingestion.file_expires_at = ingestion.uploaded_at
        ingestion.save(update_fields=["file_expires_at"])

        payload = list_ingestions(self.tenant)
        self.assertEqual(payload["count"], 1)
        row = payload["ingestions"][0]
        self.assertEqual(row["original_filename"], "receipt.pdf")
        self.assertTrue(row["file_expired"])
        self.assertEqual(row["artifacts"][0]["destination"], "A task")
        self.assertEqual(row["artifacts"][0]["excerpt"], "buy milk")
        self.assertFalse(row["artifacts"][0]["removed"])
