from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest.mock import ANY, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.journal.models import NoteTemplate, Task
from apps.lessons.models import Lesson, StarJournalEntry
from apps.pii.receipt_demotion import (
    ReceiptDemotionBatchResult,
    parse_deploy_cutoff,
    process_receipt_demotion_batch,
    w4_receipt_demotion_task,
)
from apps.pii.store_registry import registered_store
from apps.router.models import DeliveryAttempt
from apps.tenants.models import Tenant, User


class ReceiptDemotionTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username=f"w4-demotion-{uuid.uuid4()}", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
        )
        self.cutoff = timezone.now() - timedelta(days=10)

    def _task(self, number: int, *, updated_at, writer: str = "runtime") -> Task:
        task = Task.objects.create(
            id=uuid.UUID(int=number),
            tenant=self.tenant,
            title="[PERSON_1] task",
            description="",
            pii_receipts={
                "title": {
                    "state": "placeholder",
                    "writer": writer,
                    "redactions": [{"placeholder": "[PERSON_1]", "value": "W1c retained value"}],
                },
                "description": {"state": "placeholder", "writer": "owner", "redactions": []},
            },
        )
        Task.objects.filter(pk=task.pk).update(updated_at=updated_at)
        task.refresh_from_db()
        return task

    def test_runtime_and_background_pre_cutoff_dry_run_then_commit_demotes_only_state(self):
        old = self._task(1, updated_at=self.cutoff - timedelta(seconds=1))
        new = self._task(2, updated_at=self.cutoff + timedelta(seconds=1))
        owner = self._task(3, updated_at=self.cutoff - timedelta(seconds=1), writer="owner")
        background = self._task(4, updated_at=self.cutoff - timedelta(seconds=1), writer="background")
        old_timestamp = old.updated_at
        original_redactions = old.pii_receipts["title"]["redactions"]

        dry = process_receipt_demotion_batch(self.tenant, "journal.Task", self.cutoff, batch_size=10)
        old.refresh_from_db()
        self.assertEqual(dry.counts["matched"], 2)
        self.assertEqual(old.pii_receipts["title"]["state"], "placeholder")

        committed = process_receipt_demotion_batch(
            self.tenant,
            "journal.Task",
            self.cutoff,
            commit=True,
            batch_size=10,
        )
        old.refresh_from_db()
        new.refresh_from_db()
        owner.refresh_from_db()
        background.refresh_from_db()
        self.assertEqual(committed.counts["demoted"], 2)
        self.assertEqual(old.pii_receipts["title"]["state"], "unconfirmed")
        self.assertEqual(old.pii_receipts["title"]["redactions"], original_redactions)
        self.assertEqual(old.updated_at, old_timestamp)
        self.assertEqual(new.pii_receipts["title"]["state"], "placeholder")
        self.assertEqual(owner.pii_receipts["title"]["state"], "placeholder")
        self.assertEqual(background.pii_receipts["title"]["state"], "unconfirmed")

    def test_created_at_fallback_demotes_pre_cutoff_receipt(self):
        lesson = Lesson.objects.create(
            tenant=self.tenant,
            text="Stable placeholder-space lesson",
            source_type="experience",
        )
        entry = StarJournalEntry.objects.create(
            tenant=self.tenant,
            star=lesson,
            text="[PERSON_1] reflection",
            pii_receipts={"text": {"state": "placeholder", "writer": "background"}},
        )
        StarJournalEntry.objects.filter(pk=entry.pk).update(created_at=self.cutoff - timedelta(seconds=1))

        result = process_receipt_demotion_batch(
            self.tenant,
            "lessons.StarJournalEntry",
            self.cutoff,
            commit=True,
        )

        entry.refresh_from_db()
        self.assertEqual(result.counts["runtime_pre_cutoff"], 1)
        self.assertEqual(entry.pii_receipts["text"]["state"], "unconfirmed")

    def test_store_without_time_discriminator_is_named_and_skipped(self):
        attempt = DeliveryAttempt.objects.create(
            tenant=self.tenant,
            occurrence_key="w4-no-time-discriminator",
            channel="telegram",
            response_excerpt="[PERSON_1] accepted",
            pii_receipts={"response_excerpt": {"state": "placeholder", "writer": "background"}},
        )

        with self.assertLogs("apps.pii.receipt_demotion", level="INFO") as logs:
            result = process_receipt_demotion_batch(
                self.tenant,
                "router.DeliveryAttempt",
                self.cutoff,
                commit=True,
            )

        attempt.refresh_from_db()
        self.assertTrue(result.skipped)
        self.assertEqual(result.counts["time_discriminator_missing_skipped"], 0)
        self.assertEqual(attempt.pii_receipts["response_excerpt"]["state"], "placeholder")
        self.assertTrue(any("time_discriminator_missing_skipped=0" in line for line in logs.output))

    def test_non_recursive_json_zero_leaf_shape_demotes_placeholder_receipt(self):
        template = NoteTemplate.objects.create(
            tenant=self.tenant,
            slug="pre-w3a-shape",
            name="Template",
            sections="legacy scalar",
            pii_receipts={
                "name": {"state": "placeholder", "writer": "owner"},
                "sections": {"state": "placeholder", "writer": "owner", "redactions": []},
            },
        )

        result = process_receipt_demotion_batch(
            self.tenant,
            "journal.NoteTemplate",
            self.cutoff,
            commit=True,
        )

        template.refresh_from_db()
        self.assertEqual(result.counts["no_leaf_shape"], 1)
        self.assertEqual(template.pii_receipts["sections"]["state"], "unconfirmed")
        self.assertEqual(template.sections, "legacy scalar")

    def test_flag_disabled_tenant_is_refused_even_for_dry_run(self):
        self.tenant.layer1_placeholder_writes = False
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

        with self.assertLogs("apps.pii.receipt_demotion", level="INFO") as logs:
            result = process_receipt_demotion_batch(self.tenant, "journal.Task", self.cutoff)

        self.assertTrue(result.skipped)
        self.assertTrue(any("flag_disabled_skipped=0" in line for line in logs.output))

    def test_cutoff_is_required_and_timezone_aware(self):
        for raw in ("", "2026-08-08T06:46:09"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_deploy_cutoff(raw)

    def test_management_command_is_dry_run_by_default(self):
        with patch("apps.pii.management.commands.w4_receipt_demotion.process_receipt_demotion_batch") as process:
            process.return_value = ReceiptDemotionBatchResult("journal.Task", True, False, 0, "", {})
            call_command(
                "w4_receipt_demotion",
                tenant_id=str(self.tenant.pk),
                deploy_cutoff=self.cutoff.isoformat(),
                store="journal.Task",
            )

        self.assertFalse(process.call_args.kwargs["commit"])


class ReceiptDemotionTaskTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username=f"w4-demotion-task-{uuid.uuid4()}", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
        )
        self.cutoff = timezone.now() - timedelta(days=10)

    @override_settings(W4_MIGRATION_TENANT_IDS="")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.pii.receipt_demotion.process_receipt_demotion_batch")
    def test_stray_commit_publish_is_inert_when_gate_closed(self, process, publish):
        result = w4_receipt_demotion_task(
            str(self.tenant.pk),
            self.cutoff.isoformat(),
            commit=True,
        )

        self.assertEqual(result["status"], "not_gated")
        process.assert_not_called()
        publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.pii.receipt_demotion.process_receipt_demotion_batch")
    def test_bounded_task_chains_with_colon_free_dedup(self, process, publish):
        process.return_value = ReceiptDemotionBatchResult("journal.Task", True, False, 1, "row-1", {"matched": 1})
        task_store = registered_store("journal.Task")
        with patch("apps.pii.receipt_demotion.registered_stores", return_value=(task_store,)):
            result = w4_receipt_demotion_task(str(self.tenant.pk), self.cutoff.isoformat())

        self.assertEqual(result["status"], "chained")
        publish.assert_called_once_with(
            "w4_receipt_demotion",
            str(self.tenant.pk),
            self.cutoff.isoformat(),
            commit=False,
            batch_size=25,
            store_index=1,
            after_pk="",
            idempotency_key=ANY,
        )
        key = publish.call_args.kwargs["idempotency_key"]
        self.assertNotIn(":", key)
        self.assertNotRegex(key, r"\s")

    @patch("apps.pii.receipt_demotion.w4_receipt_demotion_task", autospec=True)
    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_task_map_round_trip(self, _verify, task):
        task.return_value = {"status": "chained"}
        response = APIClient().post(
            "/api/cron/trigger/w4_receipt_demotion/",
            data=json.dumps({"args": [str(self.tenant.pk), self.cutoff.isoformat()], "kwargs": {"commit": False}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        task.assert_called_once_with(str(self.tenant.pk), self.cutoff.isoformat(), commit=False)
