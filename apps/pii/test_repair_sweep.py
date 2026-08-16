from __future__ import annotations

from datetime import date
from unittest.mock import patch
from uuid import UUID

from django.db import DataError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.fuel.models import Workout, WorkoutCategory
from apps.journal.models import Goal, JournalEntry, PendingTaskAction, Purpose, Task
from apps.pii.authoring import AuthoredText
from apps.pii.redactor import RedactionOutcome
from apps.pii.repair_sweep import (
    DEFAULT_TEXT_BUDGET,
    MAX_DETECTOR_CALLS_PER_SWEEP,
    MAX_DETECTOR_CALLS_PER_TEXT,
    MAX_REPAIR_ATTEMPTS,
    _check_rate_alert,
    repair_tenant,
    sweep_placeholder_repairs,
)
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

        with patch("apps.pii.repair_sweep.author_text", side_effect=authored) as author:
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

    def test_w3b_recursive_json_store_repairs_every_string_descendant_once(self):
        workout = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 8, 8),
            category=WorkoutCategory.OTHER,
            activity="Circuit",
            detail_json={
                "coach": "Alice",
                "sets": [{"cue": "Ask Alice", "reps": 5}, [True, "Alice note"]],
                "empty": None,
            },
            pii_receipts={"detail_json": {"state": "unconfirmed", "reason": "redaction-error"}},
        )

        def authored(_tenant, text, **_kwargs):
            rewritten = text.replace("Alice", "[PERSON_1]")
            redactions = [{"placeholder": "[PERSON_1]"}] if rewritten != text else []
            return AuthoredText(
                text=rewritten,
                receipt={"state": "placeholder", "redactions": redactions, "writer": "background"},
            )

        with patch("apps.pii.repair_sweep.author_text", side_effect=authored) as author:
            first = repair_tenant(self.tenant, alert=False)
            second = repair_tenant(self.tenant, alert=False)

        workout.refresh_from_db()
        self.assertEqual(
            workout.detail_json,
            {
                "coach": "[PERSON_1]",
                "sets": [{"cue": "Ask [PERSON_1]", "reps": 5}, [True, "[PERSON_1] note"]],
                "empty": None,
            },
        )
        self.assertEqual(workout.pii_receipts["detail_json"]["state"], "placeholder")
        self.assertEqual(
            workout.pii_receipts["detail_json"]["redactions"],
            [{"placeholder": "[PERSON_1]"}],
        )
        self.assertEqual(first["fields_attempted"], 1)
        self.assertEqual(first["fields_repaired"], 1)
        self.assertEqual(second["fields_attempted"], 0)
        self.assertEqual(author.call_count, 3)
        self.assertTrue(all(call.kwargs["model_label"] == "fuel.Workout" for call in author.call_args_list))

    def test_recursive_json_converges_across_hard_bounded_chunks_without_data_loss(self):
        workout = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 8, 9),
            category=WorkoutCategory.OTHER,
            activity="Circuit",
            detail_json={"one": "Alice 1", "two": "Alice 2", "three": ["Alice 3", "Alice 4", "Alice 5"]},
            pii_receipts={"detail_json": {"state": "unconfirmed", "reason": "detector-deferred"}},
        )

        def authored(_tenant, text, **_kwargs):
            return AuthoredText(
                text=text.replace("Alice", "[PERSON_1]"),
                receipt={
                    "state": "placeholder",
                    "redactions": [{"placeholder": "[PERSON_1]"}],
                    "writer": "background",
                },
            )

        with patch("apps.pii.repair_sweep.author_text", side_effect=authored) as author:
            first = repair_tenant(self.tenant, max_texts=2, alert=False)
            workout.refresh_from_db()
            first_value = workout.detail_json
            first_receipt = workout.pii_receipts["detail_json"]

            second = repair_tenant(self.tenant, max_texts=2, alert=False)
            workout.refresh_from_db()
            second_value = workout.detail_json
            second_receipt = workout.pii_receipts["detail_json"]

            third = repair_tenant(self.tenant, max_texts=2, alert=False)

        workout.refresh_from_db()
        self.assertEqual(first["texts_authored"], 2)
        self.assertEqual(second["texts_authored"], 2)
        self.assertEqual(third["texts_authored"], 1)
        self.assertEqual(first_receipt["reason"], "repair-batch-partial")
        self.assertEqual(first_receipt["repair_progress"]["cursor"], 2)
        self.assertNotIn("repair_attempts", first_receipt)
        self.assertEqual(second_receipt["reason"], "repair-batch-partial")
        self.assertEqual(second_receipt["repair_progress"]["cursor"], 4)
        self.assertNotIn("repair_attempts", second_receipt)
        self.assertEqual(first_value["one"], "[PERSON_1] 1")
        self.assertEqual(first_value["two"], "[PERSON_1] 2")
        self.assertEqual(first_value["three"], ["Alice 3", "Alice 4", "Alice 5"])
        self.assertEqual(second_value["three"], ["[PERSON_1] 3", "[PERSON_1] 4", "Alice 5"])
        self.assertEqual(
            workout.detail_json,
            {
                "one": "[PERSON_1] 1",
                "two": "[PERSON_1] 2",
                "three": ["[PERSON_1] 3", "[PERSON_1] 4", "[PERSON_1] 5"],
            },
        )
        self.assertEqual(workout.pii_receipts["detail_json"]["state"], "placeholder")
        self.assertNotIn("repair_progress", workout.pii_receipts["detail_json"])
        self.assertNotIn("repair_attempts", workout.pii_receipts["detail_json"])
        self.assertEqual(author.call_count, 5)

    def test_text_budget_spans_multiple_flat_fields_on_one_row(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="raw title",
            description="raw description",
            pii_receipts={
                "title": {"state": "unconfirmed"},
                "description": {"state": "unconfirmed"},
            },
        )

        def repaired(_tenant, text, **_kwargs):
            return AuthoredText(text=f"fixed {text}", receipt={"state": "placeholder", "redactions": []})

        with patch("apps.pii.repair_sweep.author_text", side_effect=repaired) as author:
            first = repair_tenant(self.tenant, max_texts=1, alert=False)
            second = repair_tenant(self.tenant, max_texts=1, alert=False)

        task.refresh_from_db()
        self.assertEqual(first["texts_authored"], 1)
        self.assertEqual(second["texts_authored"], 1)
        self.assertEqual(task.title, "fixed raw title")
        self.assertEqual(task.description, "fixed raw description")
        self.assertEqual(author.call_count, 2)

    def test_shape_mismatch_terminalizes_after_bounded_persisted_attempts(self):
        purpose = Purpose.objects.create(
            tenant=self.tenant,
            statement="A stable direction",
            evidence=[{"unexpected": "Alice"}],
            pii_receipts={"evidence": {"state": "unconfirmed", "reason": "shape-mismatch"}},
        )

        for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
            result = repair_tenant(self.tenant, alert=False)
            purpose.refresh_from_db()
            receipt = purpose.pii_receipts["evidence"]
            self.assertEqual(receipt["repair_attempts"], attempt)
            self.assertEqual(result["fields_attempted"], 1)
            self.assertEqual(result["unconfirmed"], 1)
            if attempt < MAX_REPAIR_ATTEMPTS:
                self.assertEqual(receipt["state"], "unconfirmed")
                self.assertEqual(result["terminal"], 0)
            else:
                self.assertEqual(receipt["state"], "terminal")
                self.assertEqual(receipt["terminal_from"], "unconfirmed")
                self.assertEqual(receipt["terminal_reason"], "repair-attempts-exhausted")
                self.assertEqual(result["terminal"], 1)

        exhausted = repair_tenant(self.tenant, alert=False)
        purpose.refresh_from_db()
        self.assertEqual(exhausted["rows_seen"], 0)
        self.assertEqual(exhausted["fields_attempted"], 0)
        self.assertEqual(purpose.pii_receipts["evidence"]["repair_attempts"], MAX_REPAIR_ATTEMPTS)

    def test_repairs_are_tenant_scoped(self):
        other_user = User.objects.create_user(username="repair-other", password="x")
        other_tenant = Tenant.objects.create(
            user=other_user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
        )
        own = Task.objects.create(
            tenant=self.tenant,
            title="own raw",
            pii_receipts={"title": {"state": "unconfirmed"}},
        )
        other = Task.objects.create(
            tenant=other_tenant,
            title="other raw",
            pii_receipts={"title": {"state": "unconfirmed"}},
        )

        def repaired(_tenant, text, **_kwargs):
            return AuthoredText(text=f"fixed {text}", receipt={"state": "placeholder", "redactions": []})

        with patch("apps.pii.repair_sweep.author_text", side_effect=repaired):
            result = repair_tenant(self.tenant, alert=False)

        own.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(result["rows_seen"], 1)
        self.assertEqual(own.title, "fixed own raw")
        self.assertEqual(other.title, "other raw")
        self.assertEqual(other.pii_receipts["title"]["state"], "unconfirmed")

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
            results = [repair_tenant(self.tenant, alert=False) for _attempt in range(MAX_REPAIR_ATTEMPTS + 1)]

        poison.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(results[0]["errors"], 1)
        self.assertEqual(results[0]["rows_seen"], 2)
        self.assertEqual(results[0]["fields_repaired"], 1)
        self.assertEqual(results[1]["errors"], 1)
        self.assertEqual(results[2]["errors"], 1)
        self.assertEqual(results[2]["terminal"], 1)
        self.assertEqual(results[3]["rows_seen"], 0)
        self.assertEqual(results[3]["fields_attempted"], 0)
        self.assertEqual(poison.title, "poison")
        self.assertEqual(poison.pii_receipts["title"]["state"], "terminal")
        self.assertEqual(poison.pii_receipts["title"]["terminal_from"], "unconfirmed")
        self.assertEqual(poison.pii_receipts["title"]["repair_attempts"], MAX_REPAIR_ATTEMPTS)
        self.assertEqual(good.title, "fixed good")

    def test_concurrent_request_write_wins_after_detector_work(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="stale raw",
            pii_receipts={"title": {"state": "unconfirmed", "reason": "detector-deferred"}},
        )

        def concurrent_write(_tenant, _text, **_kwargs):
            Task.objects.filter(pk=task.pk).update(
                title="request won",
                pii_receipts={"title": {"state": "unconfirmed", "reason": "new-request"}},
            )
            return AuthoredText(text="sweep result", receipt={"state": "placeholder", "redactions": []})

        with patch("apps.pii.repair_sweep.author_text", side_effect=concurrent_write):
            result = repair_tenant(self.tenant, alert=False)

        task.refresh_from_db()
        self.assertEqual(task.title, "request won")
        self.assertEqual(task.pii_receipts["title"]["reason"], "new-request")
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["fields_repaired"], 0)
        self.assertEqual(result["texts_authored"], 1)

    def test_save_failure_fallback_also_respects_concurrent_request_write(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="stale raw",
            pii_receipts={"title": {"state": "unconfirmed", "reason": "detector-deferred"}},
        )
        repaired = AuthoredText(text="sweep result", receipt={"state": "placeholder", "redactions": []})

        def fail_after_request_write(*_args, **_kwargs):
            Task.objects.filter(pk=task.pk).update(
                title="request won",
                pii_receipts={"title": {"state": "placeholder", "reason": "new-request"}},
            )
            raise DataError("simulated stale save")

        with (
            patch("apps.pii.repair_sweep.author_text", return_value=repaired),
            patch("apps.pii.repair_sweep._save_if_unchanged", side_effect=fail_after_request_write),
        ):
            result = repair_tenant(self.tenant, alert=False)

        task.refresh_from_db()
        self.assertEqual(task.title, "request won")
        self.assertEqual(task.pii_receipts["title"]["reason"], "new-request")
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["fields_repaired"], 0)

    def test_store_rotation_reaches_later_registry_stores_under_one_text_budget(self):
        task = Task.objects.create(
            tenant=self.tenant,
            title="task raw",
            pii_receipts={"title": {"state": "unconfirmed"}},
        )
        goal = Goal.objects.create(
            tenant=self.tenant,
            title="goal raw",
            pii_receipts={"title": {"state": "unconfirmed"}},
        )
        repaired = AuthoredText(text="fixed", receipt={"state": "placeholder", "redactions": []})

        with patch("apps.pii.repair_sweep.author_text", return_value=repaired):
            result = repair_tenant(self.tenant, max_texts=1, store_offset=1, alert=False)

        task.refresh_from_db()
        goal.refresh_from_db()
        self.assertEqual(result["texts_authored"], 1)
        self.assertEqual(goal.title, "fixed")
        self.assertEqual(task.title, "task raw")

    def test_capacity_stepped_tenant_rotation_reaches_high_ids_across_ticks(self):
        self.tenant.layer1_placeholder_writes = False
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        tenant_ids = []
        for index in range(6):
            user = User.objects.create_user(username=f"repair-fair-{index}", password="x")
            tenant = Tenant.objects.create(
                id=UUID(int=100 + index),
                user=user,
                status=Tenant.Status.ACTIVE,
                layer1_placeholder_writes=True,
            )
            tenant_ids.append(tenant.id)
            Task.objects.create(
                tenant=tenant,
                title=f"raw {index}",
                pii_receipts={"title": {"state": "unconfirmed"}},
            )

        def repaired(_tenant, text, **_kwargs):
            return AuthoredText(text=f"fixed {text}", receipt={"state": "placeholder", "redactions": []})

        with (
            patch("apps.pii.repair_sweep.author_text", side_effect=repaired) as author,
            patch("apps.pii.repair_sweep._check_rate_alert", return_value=False),
        ):
            for tick in range(3):
                result = sweep_placeholder_repairs(
                    batch_size=2,
                    text_budget=2,
                    tenant_batch_size=1,
                    tenant_text_budget=1,
                    fairness_tick=tick,
                )
                self.assertEqual(result["texts_authored"], 2)

        repaired_tenants = {
            row.tenant_id
            for row in Task.objects.filter(tenant_id__in=tenant_ids).only("tenant_id", "title")
            if row.title.startswith("fixed")
        }
        self.assertEqual(repaired_tenants, set(tenant_ids))
        self.assertEqual(author.call_count, 6)
        self.assertEqual(MAX_DETECTOR_CALLS_PER_TEXT, 2)
        self.assertEqual(MAX_DETECTOR_CALLS_PER_SWEEP, DEFAULT_TEXT_BUDGET * MAX_DETECTOR_CALLS_PER_TEXT)

    def test_fleet_rotation_loads_only_ids_before_fetching_one_tenant_payload(self):
        result = {
            "rows_seen": 1,
            "fields_attempted": 1,
            "texts_authored": 1,
            "fields_repaired": 1,
            "unconfirmed": 0,
            "residual": 0,
            "terminal": 0,
            "conflicts": 0,
            "errors": 0,
        }
        with (
            patch("apps.pii.repair_sweep.repair_tenant", return_value=result) as repair,
            CaptureQueriesContext(connection) as queries,
        ):
            sweep_placeholder_repairs(
                batch_size=1,
                text_budget=1,
                tenant_batch_size=1,
                tenant_text_budget=1,
                fairness_tick=0,
            )

        self.assertEqual(len(queries), 3)
        self.assertNotIn("pii_entity_map", queries[0]["sql"].lower())
        self.assertNotIn("pii_entity_map", queries[1]["sql"].lower())
        self.assertIn("pii_entity_map", queries[2]["sql"].lower())
        self.assertEqual(repair.call_args.args[0].pk, self.tenant.pk)

    def test_nonpositive_per_tenant_budget_returns_without_querying(self):
        with self.assertNumQueries(0):
            result = sweep_placeholder_repairs(tenant_text_budget=0)

        self.assertEqual(result["rows_seen"], 0)
        self.assertEqual(result["texts_authored"], 0)

    def test_oversized_sweep_arguments_cannot_raise_hard_caps(self):
        for index in range(4):
            user = User.objects.create_user(username=f"repair-cap-{index}", password="x")
            Tenant.objects.create(
                user=user,
                status=Tenant.Status.ACTIVE,
                layer1_placeholder_writes=True,
            )

        def consume_budget(_tenant, *, max_rows, max_texts, **_kwargs):
            self.assertLessEqual(max_rows, 4)
            self.assertLessEqual(max_texts, 4)
            return {
                "rows_seen": max_rows,
                "fields_attempted": max_texts,
                "texts_authored": max_texts,
                "fields_repaired": max_texts,
                "unconfirmed": 0,
                "residual": 0,
                "terminal": 0,
                "conflicts": 0,
                "errors": 0,
            }

        with patch("apps.pii.repair_sweep.repair_tenant", side_effect=consume_budget) as repair:
            result = sweep_placeholder_repairs(
                batch_size=10_000,
                text_budget=10_000,
                tenant_batch_size=10_000,
                tenant_text_budget=10_000,
                fairness_tick=0,
            )

        self.assertEqual(result["rows_seen"], 16)
        self.assertEqual(result["texts_authored"], 16)
        self.assertEqual(repair.call_count, 4)

    def test_empty_fleet_scan_is_tenant_bounded_and_rotates_next_hour(self):
        self.tenant.layer1_placeholder_writes = False
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        tenant_ids = []
        for index in range(7):
            user = User.objects.create_user(username=f"repair-empty-{index}", password="x")
            tenant = Tenant.objects.create(
                id=UUID(int=200 + index),
                user=user,
                status=Tenant.Status.ACTIVE,
                layer1_placeholder_writes=True,
            )
            tenant_ids.append(tenant.id)
        no_work = {
            "rows_seen": 0,
            "fields_attempted": 0,
            "texts_authored": 0,
            "fields_repaired": 0,
            "unconfirmed": 0,
            "residual": 0,
            "terminal": 0,
            "conflicts": 0,
            "errors": 0,
        }

        with patch("apps.pii.repair_sweep.repair_tenant", return_value=no_work) as repair:
            sweep_placeholder_repairs(fairness_tick=0)
            first_ids = [call.args[0].pk for call in repair.call_args_list]
            repair.reset_mock()
            sweep_placeholder_repairs(fairness_tick=1)
            second_ids = [call.args[0].pk for call in repair.call_args_list]

        self.assertEqual(first_ids, tenant_ids[:4])
        self.assertEqual(second_ids, [*tenant_ids[4:], tenant_ids[0]])

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

    def test_terminal_rate_has_its_own_alarm_and_counter_line(self):
        with patch("apps.transcripts.alerts._send_alert", return_value=True) as send_alert:
            fired = _check_rate_alert(self.tenant, attempts=100, count=2, kind="terminal")

        self.assertTrue(fired)
        kwargs = send_alert.call_args.kwargs
        self.assertIn("terminal rate above 1%", kwargs["subject"])
        self.assertIn("Terminal outcomes: 2", kwargs["body"])

    def test_repair_feeds_terminal_outcomes_to_terminal_alarm(self):
        Task.objects.create(
            tenant=self.tenant,
            title="still raw",
            pii_receipts={
                "title": {
                    "state": "unconfirmed",
                    "reason": "redaction-error",
                    "repair_attempts": MAX_REPAIR_ATTEMPTS - 1,
                }
            },
        )
        failed = AuthoredText(
            text="still raw",
            receipt={"state": "unconfirmed", "reason": "redaction-error", "redactions": []},
        )

        with (
            patch("apps.pii.repair_sweep.author_text", return_value=failed),
            patch("apps.pii.repair_sweep._check_rate_alert", return_value=False) as check_alert,
        ):
            result = repair_tenant(self.tenant)

        self.assertEqual(result["terminal"], 1)
        terminal_call = next(call for call in check_alert.call_args_list if call.kwargs["kind"] == "terminal")
        self.assertEqual(terminal_call.kwargs["count"], 1)
        self.assertEqual(terminal_call.kwargs["attempts"], 1)

    def test_qstash_task_map_registers_repair_entrypoint(self):
        from apps.cron.views import TASK_MAP

        self.assertEqual(
            TASK_MAP["placeholder_repair_sweep"],
            "apps.pii.repair_sweep.placeholder_repair_sweep_task",
        )

    def test_system_crons_register_hourly_repair_sweep(self):
        from apps.cron.management.commands.register_system_crons import SYSTEM_CRONS

        entry = next(item for item in SYSTEM_CRONS if item[0] == "placeholder-repair-sweep")
        self.assertEqual(entry[1], "13 * * * *")
        self.assertEqual(entry[2], "/api/cron/trigger/placeholder_repair_sweep/")
