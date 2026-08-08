from __future__ import annotations

from datetime import date
from unittest.mock import patch
from uuid import UUID

from django.db import DataError
from django.test import TestCase

from apps.journal.models import JournalEntry, PendingTaskAction, Task
from apps.pii.authoring import AuthoredText
from apps.pii.redactor import RedactionOutcome
from apps.pii.repair_sweep import _check_rate_alert, repair_tenant
from apps.tenants.models import Tenant, User


class RepairSweepTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username="repair", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
        )

    def test_repairs_unconfirmed_row_and_becomes_idempotently_ineligible(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="Alice task",
            pii_receipts={"title": {"state": "unconfirmed", "reason": "redaction-error"}},
        )
        repaired = AuthoredText(
            text="[PERSON_1] task",
            receipt={
                "state": "placeholder",
                "redactions": [{"placeholder": "[PERSON_1]", "value": "Alice"}],
            },
        )
        with patch("apps.pii.repair_sweep.author_text", return_value=repaired) as author:
            first = repair_tenant(self.tenant, alert=False)
            second = repair_tenant(self.tenant, alert=False)

        task.refresh_from_db()
        self.assertEqual(task.title, "[PERSON_1] task")
        self.assertEqual(task.pii_receipts["title"]["state"], "placeholder")
        self.assertEqual(first["fields_repaired"], 1)
        self.assertEqual(second["fields_attempted"], 0)
        author.assert_called_once()
        self.assertEqual(author.call_args.kwargs["model_label"], "journal.Task")

    def test_real_registered_json_store_repairs_wildcard_leaves_once(self):
        entry = JournalEntry.objects.create(
            tenant=self.tenant,
            date=date(2026, 8, 8),
            mood="steady",
            energy=JournalEntry.Energy.MEDIUM,
            wins=["Alice shipped", "No name"],
            challenges=[],
            reflection="",
            raw_text="Wins: Alice shipped, No name",
            pii_receipts={"wins": {"state": "unconfirmed", "reason": "redaction-error"}},
        )

        def authored(_tenant, text, **_kwargs):
            rewritten = text.replace("Alice", "[PERSON_1]")
            redactions = [{"placeholder": "[PERSON_1]"}] if rewritten != text else []
            return AuthoredText(
                text=rewritten,
                receipt={"state": "placeholder", "redactions": redactions, "writer": "background"},
            )

        with patch("apps.pii.authoring.author_text", side_effect=authored) as author:
            first = repair_tenant(self.tenant, alert=False)
            second = repair_tenant(self.tenant, alert=False)

        entry.refresh_from_db()
        self.assertEqual(entry.wins, ["[PERSON_1] shipped", "No name"])
        self.assertEqual(entry.pii_receipts["wins"]["state"], "placeholder")
        self.assertEqual(entry.pii_receipts["wins"]["redactions"], [{"placeholder": "[PERSON_1]"}])
        self.assertEqual(first["fields_attempted"], 1)
        self.assertEqual(first["fields_repaired"], 1)
        self.assertEqual(second["fields_attempted"], 0)
        self.assertEqual(author.call_count, 2)
        self.assertTrue(all(call.kwargs["model_label"] == "journal.JournalEntry" for call in author.call_args_list))

    def test_repairs_unconfirmed_reconciliation_evidence(self):
        action = PendingTaskAction.objects.create(
            tenant=self.tenant,
            kind=PendingTaskAction.Kind.TASK_PROGRESS,
            evidence="Alice confirmed it",
            pii_receipts={"evidence": {"state": "unconfirmed", "reason": "redaction-error"}},
            source_date="2026-08-07",
        )
        repaired = AuthoredText(
            text="[PERSON_1] confirmed it",
            receipt={
                "state": "placeholder",
                "redactions": [{"placeholder": "[PERSON_1]", "value": "Alice"}],
            },
        )

        with patch("apps.pii.repair_sweep.author_text", return_value=repaired):
            result = repair_tenant(self.tenant, alert=False)

        action.refresh_from_db()
        self.assertEqual(action.evidence, "[PERSON_1] confirmed it")
        self.assertEqual(action.pii_receipts["evidence"]["state"], "placeholder")
        self.assertEqual(result["fields_repaired"], 1)

    def test_unconfirmed_rows_are_processed_before_residual_rows(self):
        residual = Task.objects.create(
            id=UUID(int=1),
            tenant=self.tenant,
            title="stable residual",
            pii_receipts={"title": {"state": "residual"}},
        )
        unconfirmed = Task.objects.create(
            id=UUID(int=2),
            tenant=self.tenant,
            title="repair me",
            pii_receipts={"title": {"state": "unconfirmed"}},
        )
        repaired = AuthoredText(text="repaired", receipt={"state": "placeholder", "redactions": []})

        with patch("apps.pii.repair_sweep.author_text", return_value=repaired):
            result = repair_tenant(self.tenant, max_rows=1, alert=False)

        residual.refresh_from_db()
        unconfirmed.refresh_from_db()
        self.assertEqual(result["rows_seen"], 1)
        self.assertEqual(unconfirmed.title, "repaired")
        self.assertEqual(residual.title, "stable residual")

    def test_runtime_residual_row_is_reauthored_as_background_and_stays_residual(self):
        raw = "Dana Whitfield is on the list"
        task = Task.objects.create(
            tenant=self.tenant,
            title=raw,
            pii_receipts={
                "title": {
                    "state": "residual",
                    "writer": "runtime",
                    "residual_spans": {"count": 1, "kinds": {"PERSON": 1}},
                }
            },
        )
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text=raw, confirmed=True, reason="redacted"),
            ),
            patch(
                "apps.pii.authoring._residual_summary",
                return_value={"count": 1, "kinds": {"PERSON": 1}},
            ),
            patch("apps.pii.alerts.record_live_write_outcome"),
        ):
            result = repair_tenant(self.tenant, alert=False)

        task.refresh_from_db()
        self.assertEqual(task.title, raw)
        self.assertEqual(task.pii_receipts["title"]["state"], "residual")
        self.assertEqual(task.pii_receipts["title"]["writer"], "background")
        self.assertEqual(result["fields_attempted"], 1)
        self.assertEqual(result["residual"], 1)
        self.assertEqual(result["fields_repaired"], 0)
        self.assertEqual(result["errors"], 0)

    def test_row_save_error_is_counted_and_sweep_continues(self):
        poison = Task.objects.create(
            id=UUID(int=1),
            tenant=self.tenant,
            title="poison",
            pii_receipts={"title": {"state": "unconfirmed"}},
        )
        good = Task.objects.create(
            id=UUID(int=2),
            tenant=self.tenant,
            title="good",
            pii_receipts={"title": {"state": "unconfirmed"}},
        )
        original_save = Task.save

        def flaky_save(instance, *args, **kwargs):
            if instance.pk == poison.pk:
                raise DataError("title overflow")
            return original_save(instance, *args, **kwargs)

        def repaired(_tenant, text, **_kwargs):
            return AuthoredText(text=f"fixed {text}", receipt={"state": "placeholder", "redactions": []})

        with (
            patch.object(Task, "save", new=flaky_save),
            patch("apps.pii.repair_sweep.author_text", side_effect=repaired),
        ):
            result = repair_tenant(self.tenant, alert=False)

        poison.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["rows_seen"], 2)
        self.assertEqual(result["fields_repaired"], 1)
        self.assertEqual(poison.title, "poison")
        self.assertEqual(good.title, "fixed good")

    def test_reauthor_truncation_prevents_poison_row_from_wedging_sweep(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="repair me",
            pii_receipts={"title": {"state": "unconfirmed"}},
        )
        over_limit = "x" * 250 + "[PERSON_123456789]" + "tail"
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text=over_limit, confirmed=True, reason="redacted"),
            ),
            patch("apps.pii.authoring._residual_summary", return_value={"count": 0, "kinds": {}}),
            patch("apps.pii.alerts.record_live_write_outcome"),
        ):
            first = repair_tenant(self.tenant, alert=False)
            second = repair_tenant(self.tenant, alert=False)

        task.refresh_from_db()
        self.assertEqual(task.title, "x" * 250)
        self.assertLessEqual(len(task.title), Task._meta.get_field("title").max_length)
        self.assertNotIn("[PERSON_", task.title)
        self.assertEqual(task.pii_receipts["title"]["writer"], "background")
        self.assertEqual(first["errors"], 0)
        self.assertEqual(first["fields_repaired"], 1)
        self.assertEqual(second["fields_attempted"], 0)

    def test_sweep_repairs_do_not_pollute_live_write_counters(self):
        Task.objects.create(
            tenant=self.tenant,
            title="Alice task",
            pii_receipts={"title": {"state": "unconfirmed", "reason": "redaction-error"}},
        )
        with (
            patch(
                "apps.pii.authoring.redact_user_message_checked",
                return_value=RedactionOutcome(text="still broken", confirmed=False, reason="redaction-error"),
            ),
            patch("apps.pii.alerts.record_live_write_outcome") as record_live,
        ):
            result = repair_tenant(self.tenant, alert=False)

        self.assertEqual(result["fields_attempted"], 1)
        self.assertEqual(result["unconfirmed"], 1)
        record_live.assert_not_called()

    def test_synthetic_error_rate_above_one_percent_fires_metadata_only_alert(self):
        with patch("apps.transcripts.alerts._send_alert", return_value=True) as send_alert:
            fired = _check_rate_alert(self.tenant, attempts=100, count=2, kind="error")

        self.assertTrue(fired)
        kwargs = send_alert.call_args.kwargs
        self.assertIn("above 1%", kwargs["subject"])
        self.assertIn(f"Tenant ID: {self.tenant.id}", kwargs["body"])
        self.assertIn("Attempts: 100", kwargs["body"])
        self.assertNotIn("Alice", kwargs["body"])

    def test_qstash_task_map_registers_repair_entrypoint(self):
        from apps.cron.views import TASK_MAP

        self.assertEqual(
            TASK_MAP["placeholder_repair_sweep"],
            "apps.pii.repair_sweep.placeholder_repair_sweep_task",
        )

    def test_system_crons_register_hourly_repair_sweep(self):
        from apps.cron.management.commands.register_system_crons import SYSTEM_CRONS

        entry = next(item for item in SYSTEM_CRONS if item[0] == "placeholder-repair-sweep")
        self.assertEqual(entry[1], "0 * * * *")
        self.assertEqual(entry[2], "/api/cron/trigger/placeholder_repair_sweep/")
