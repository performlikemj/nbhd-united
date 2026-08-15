from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest.mock import ANY, patch

from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.journal.models import JournalEntry, NoteTemplate, Task
from apps.pii.historical_migration import (
    MAX_CHANGED_RECHAINS,
    BatchResult,
    _claim_cursor,
    historical_placeholder_migration_batch_task,
    historical_placeholder_migration_driver_task,
    migrate_tenant_registered_stores,
    process_store_batch,
    w4_migration_tenant_allowed,
)
from apps.pii.redactor import DetectedEntity
from apps.pii.store_registry import registered_store
from apps.router.models import DeliveryAttempt
from apps.tenants.models import PlaceholderMigrationCursor, Tenant, User


def _detect_names(text, _entities, _threshold):
    results = []
    for match in re.finditer(r"Dana Whitfield", text, re.IGNORECASE):
        results.append(DetectedEntity("PERSON", match.start(), match.end(), 0.99))
    return results


class HistoricalMigrationTests(TestCase):
    def setUp(self):
        authoring_detector = patch("apps.pii.authoring._detect_pii", side_effect=_detect_names)
        authoring_detector.start()
        self.addCleanup(authoring_detector.stop)
        user = User.objects.create_user(username=f"w4-{uuid.uuid4()}", password="x", display_name="Owner")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
        )

    def _task(self, number: int, *, title: str = "Dana Whitfield task", title_receipt=None) -> Task:
        return Task.objects.create(
            id=uuid.UUID(int=number),
            tenant=self.tenant,
            title=title,
            description="",
            pii_receipts={
                "title": title_receipt,
                "description": {"state": "placeholder", "writer": "runtime", "redactions": []},
            }
            if title_receipt is not None
            else {"description": {"state": "placeholder", "writer": "runtime", "redactions": []}},
        )

    @patch("apps.pii.historical_migration.set_rls_context")
    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_batch_reasserts_scan_rls_context_before_empty_completion(self, _repair, set_context):
        result = process_store_batch(self.tenant, "journal.Task")

        self.assertTrue(result.done)
        set_context.assert_called_once_with(tenant_id=self.tenant.pk, service_role=True)

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_per_field_fence_reports_every_state_including_terminal(self, _repair):
        states = ["placeholder", "bypass", "unconfirmed", "residual", "terminal"]
        for number, state in enumerate(states, 1):
            self._task(number, title_receipt={"state": state})
        self._task(10, title_receipt=None)

        with patch("apps.pii.redactor._detect_pii", side_effect=_detect_names):
            result = process_store_batch(self.tenant, "journal.Task", batch_size=10)

        for state in states:
            self.assertEqual(result.counts[("title", state)], 1)
        self.assertEqual(result.counts[("title", "absent")], 1)

    def test_repair_precondition_skips_only_that_store_and_excludes_terminal(self):
        self._task(1, title_receipt={"state": "terminal"})
        residual = self._task(2, title_receipt={"state": "residual"})

        with self.assertLogs("apps.pii.historical_migration", level="INFO") as logs:
            result = process_store_batch(self.tenant, "journal.Task", commit=True)

        self.assertTrue(result.skipped)
        self.assertEqual(result.counts[("-", "repair_pending_skipped")], 1)
        self.assertTrue(any("state=repair_pending_skipped rows=1" in line for line in logs.output))
        residual.refresh_from_db()
        self.assertEqual(residual.title, "Dana Whitfield task")

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_same_new_name_mints_once_across_two_rows(self, _repair):
        first = self._task(1)
        second = self._task(2, title="Call dana whitfield tomorrow")

        with patch("apps.pii.redactor._detect_pii", side_effect=_detect_names):
            process_store_batch(self.tenant, "journal.Task", commit=True, batch_size=2)

        self.tenant.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(len(self.tenant.pii_entity_map), 1)
        self.assertEqual(first.title, "[PERSON_1] task")
        self.assertEqual(second.title, "Call [PERSON_1] tomorrow")
        self.assertTrue(first.pii_receipts["title"]["migrated"])
        self.assertEqual(first.pii_receipts["title"]["writer"], "owner")
        self.assertEqual(first.pii_receipts["title"]["state"], "placeholder")

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_commit_preserves_updated_at_database_bytes(self, _repair):
        task = self._task(1)
        historical = timezone.now() - timedelta(days=400)
        Task.objects.filter(pk=task.pk).update(updated_at=historical)

        def timestamp_bytes():
            with connection.cursor() as cursor:
                if connection.vendor == "postgresql":
                    cursor.execute("SELECT timestamptz_send(updated_at) FROM journal_tasks WHERE id = %s", [task.pk])
                else:
                    cursor.execute("SELECT updated_at FROM journal_tasks WHERE id = %s", [task.pk.hex])
                value = cursor.fetchone()[0]
            return bytes(value) if isinstance(value, memoryview) else str(value).encode()

        before = timestamp_bytes()
        with patch("apps.pii.redactor._detect_pii", side_effect=_detect_names):
            process_store_batch(self.tenant, "journal.Task", commit=True)

        task.refresh_from_db()
        self.assertEqual(task.title, "[PERSON_1] task")
        self.assertEqual(timestamp_bytes(), before)

    def test_commit_refuses_flag_disabled_tenant_with_report(self):
        self.tenant.layer1_placeholder_writes = False
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        task = self._task(1)

        with self.assertLogs("apps.pii.historical_migration", level="INFO") as logs:
            result = process_store_batch(self.tenant, "journal.Task", commit=True)

        task.refresh_from_db()
        self.assertTrue(result.skipped)
        self.assertEqual(task.title, "Dana Whitfield task")
        self.assertTrue(any("state=flag_disabled_skipped" in line for line in logs.output))

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_rewrite_occurs_after_tenant_lock_and_never_row_locks_store(self, _repair):
        self._task(1)

        with (
            patch("apps.pii.redactor._detect_pii", side_effect=_detect_names),
            CaptureQueriesContext(connection) as captured,
        ):
            process_store_batch(self.tenant, "journal.Task", commit=True)

        statements = [query["sql"].upper() for query in captured.captured_queries]
        tenant_locks = [
            index for index, sql in enumerate(statements) if 'FROM "TENANTS"' in sql and "FOR UPDATE" in sql
        ]
        tenant_mint = next(
            index
            for index, sql in enumerate(statements)
            if sql.startswith('UPDATE "TENANTS"') and "PII_ENTITY_MAP" in sql
        )
        row_update = next(index for index, sql in enumerate(statements) if sql.startswith('UPDATE "JOURNAL_TASKS"'))
        self.assertEqual(len(tenant_locks), 1)
        self.assertLess(tenant_locks[0], tenant_mint)
        self.assertLess(tenant_mint, row_update)
        self.assertFalse(any("JOURNAL_TASKS" in sql and "FOR UPDATE" in sql for sql in statements))
        self.assertTrue(any("SET_CONFIG('APP.TENANT_ID'" in sql and ", TRUE)" in sql for sql in statements))

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_changed_row_is_reported_and_not_rewritten(self, _repair):
        task = self._task(1)

        def concurrent_change(*_args, **_kwargs):
            Task.objects.filter(pk=task.pk).update(title="concurrent edit", updated_at=timezone.now())
            return 0

        with patch("apps.pii.historical_migration.mint_owner_batch_entities", side_effect=concurrent_change):
            result = process_store_batch(self.tenant, "journal.Task", commit=True)

        task.refresh_from_db()
        self.assertEqual(task.title, "concurrent edit")
        self.assertEqual(result.counts[("title", "changed_skipped")], 1)
        self.assertEqual(result.last_pk, "")

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_interrupted_batch_resumes_without_double_mint_or_rewrite(self, _repair):
        first = self._task(1)
        second = self._task(2)
        from apps.pii import historical_migration as migration

        real_update = migration._conditional_update
        calls = 0

        def kill_after_first(row, store, version, updates):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("simulated worker kill")
            return real_update(row, store, version, updates)

        with (
            patch("apps.pii.redactor._detect_pii", side_effect=_detect_names),
            patch("apps.pii.historical_migration._conditional_update", side_effect=kill_after_first),
            self.assertRaises(KeyboardInterrupt),
        ):
            process_store_batch(self.tenant, "journal.Task", commit=True, batch_size=2)

        first.refresh_from_db()
        first_rewritten_at = first.updated_at
        cursor = PlaceholderMigrationCursor.objects.get(
            tenant=self.tenant,
            store_label="journal.Task",
            mode=PlaceholderMigrationCursor.Mode.COMMIT,
        )
        cursor.lease_expires_at = timezone.now() - timedelta(seconds=1)
        cursor.save(update_fields=["lease_expires_at", "updated_at"])
        with patch("apps.pii.redactor._detect_pii", side_effect=_detect_names):
            process_store_batch(self.tenant, "journal.Task", commit=True, batch_size=2)

        self.tenant.refresh_from_db()
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(len(self.tenant.pii_entity_map), 1)
        self.assertEqual(first.updated_at, first_rewritten_at)
        self.assertEqual(first.title, "[PERSON_1] task")
        self.assertEqual(second.title, "[PERSON_1] task")

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_dry_run_leaves_rows_and_entity_map_byte_identical(self, _repair):
        self._task(1)
        before_rows = json.dumps(
            list(Task.objects.filter(tenant=self.tenant).values("title", "description", "pii_receipts")),
            sort_keys=True,
        )
        before_map = json.dumps(self.tenant.pii_entity_map, sort_keys=True)

        with patch("apps.pii.redactor._detect_pii", side_effect=_detect_names):
            process_store_batch(self.tenant, "journal.Task")

        self.tenant.refresh_from_db()
        after_rows = json.dumps(
            list(Task.objects.filter(tenant=self.tenant).values("title", "description", "pii_receipts")),
            sort_keys=True,
        )
        self.assertEqual(after_rows, before_rows)
        self.assertEqual(json.dumps(self.tenant.pii_entity_map, sort_keys=True), before_map)

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_real_registered_json_path_store_rewrites_end_to_end(self, _repair):
        entry = JournalEntry.objects.create(
            tenant=self.tenant,
            date=date(2026, 8, 9),
            mood="steady",
            energy=JournalEntry.Energy.MEDIUM,
            wins=["Dana Whitfield shipped", {"structured": "untouched"}],
            challenges=[],
            reflection="",
            raw_text="",
            pii_receipts={
                field: {"state": "placeholder", "writer": "runtime", "redactions": []}
                for field in ("mood", "challenges", "reflection", "raw_text")
            },
        )

        with patch("apps.pii.redactor._detect_pii", side_effect=_detect_names):
            process_store_batch(self.tenant, "journal.JournalEntry", commit=True)

        entry.refresh_from_db()
        self.assertEqual(entry.wins, ["[PERSON_1] shipped", {"structured": "untouched"}])
        self.assertEqual(entry.pii_receipts["wins"]["state"], "placeholder")
        self.assertTrue(entry.pii_receipts["wins"]["migrated"])

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_registered_store_without_updated_at_uses_row_version_cas(self, _repair):
        attempt = DeliveryAttempt.objects.create(
            id=uuid.UUID(int=30),
            tenant=self.tenant,
            occurrence_key="w4-xmin",
            channel="telegram",
            response_excerpt="Dana Whitfield accepted",
        )

        with patch("apps.pii.redactor._detect_pii", side_effect=_detect_names):
            process_store_batch(self.tenant, "router.DeliveryAttempt", commit=True)

        attempt.refresh_from_db()
        self.assertEqual(attempt.response_excerpt, "[PERSON_1] accepted")
        self.assertEqual(attempt.pii_receipts["response_excerpt"]["state"], "placeholder")
        self.assertTrue(attempt.pii_receipts["response_excerpt"]["migrated"])

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_json_shape_mismatch_is_reported_without_claiming_migration(self, _repair):
        template = NoteTemplate.objects.create(
            id=uuid.UUID(int=1),
            tenant=self.tenant,
            slug="bad-shape",
            name="Template",
            sections="unexpected legacy scalar",
            pii_receipts={"name": {"state": "placeholder", "writer": "runtime", "redactions": []}},
        )
        next_template = NoteTemplate.objects.create(
            id=uuid.UUID(int=2),
            tenant=self.tenant,
            slug="good-shape",
            name="Template two",
            sections=[{"title": "Dana Whitfield", "content": ""}],
            pii_receipts={"name": {"state": "placeholder", "writer": "runtime", "redactions": []}},
        )

        with (
            patch("apps.pii.redactor._detect_pii", side_effect=_detect_names),
            self.assertLogs("apps.pii.historical_migration", level="INFO") as logs,
        ):
            totals = migrate_tenant_registered_stores(
                self.tenant,
                commit=True,
                store_label="journal.NoteTemplate",
            )

        template.refresh_from_db()
        next_template.refresh_from_db()
        self.assertEqual(template.sections, "unexpected legacy scalar")
        self.assertNotIn("sections", template.pii_receipts)
        self.assertEqual(next_template.sections[0]["title"], "[PERSON_1]")
        self.assertEqual(totals["stores_complete"], 1)
        self.assertTrue(any("field=sections state=authoring_unconfirmed rows=1" in line for line in logs.output))

    @patch("apps.pii.historical_migration.repair_pending_count", return_value=0)
    def test_dry_run_shape_precheck_counts_would_quarantine_without_writing(self, _repair):
        template = NoteTemplate.objects.create(
            tenant=self.tenant,
            slug="dry-bad-shape",
            name="Template",
            sections="unexpected legacy scalar",
            pii_receipts={"name": {"state": "placeholder", "writer": "runtime"}},
        )
        before = json.dumps(template.pii_receipts, sort_keys=True)

        result = process_store_batch(self.tenant, "journal.NoteTemplate")

        template.refresh_from_db()
        self.assertEqual(result.counts[("sections", "authoring_unconfirmed")], 1)
        self.assertEqual(template.sections, "unexpected legacy scalar")
        self.assertEqual(json.dumps(template.pii_receipts, sort_keys=True), before)


class HistoricalMigrationTaskTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(username=f"w4-task-{uuid.uuid4()}", password="x")
        self.tenant = Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, layer1_placeholder_writes=True)

    def assert_colon_free_chain(self, publish):
        key = publish.call_args.kwargs["idempotency_key"]
        self.assertNotIn(":", key)
        self.assertNotRegex(key, r"\s")

    @patch("apps.cron.publish.publish_task")
    def test_driver_chains_registered_stores_sequentially(self, publish):
        task_store = registered_store("journal.Task")
        goal_store = registered_store("journal.Goal")
        with patch("apps.pii.historical_migration.registered_stores", return_value=(task_store, goal_store)):
            first = historical_placeholder_migration_driver_task(str(self.tenant.pk))
            publish.assert_called_once_with(
                "historical_placeholder_migration_batch",
                str(self.tenant.pk),
                "journal.Task",
                commit=False,
                batch_size=25,
                changed_attempts=0,
                idempotency_key=ANY,
            )
            self.assert_colon_free_chain(publish)
            publish.reset_mock()
            PlaceholderMigrationCursor.objects.filter(
                tenant=self.tenant,
                store_label="journal.Task",
            ).update(status=PlaceholderMigrationCursor.Status.COMPLETE)
            second = historical_placeholder_migration_driver_task(str(self.tenant.pk))

        self.assertEqual(first["store"], "journal.Task")
        self.assertEqual(second["store"], "journal.Goal")
        publish.assert_called_once_with(
            "historical_placeholder_migration_batch",
            str(self.tenant.pk),
            "journal.Goal",
            commit=False,
            batch_size=25,
            changed_attempts=0,
            idempotency_key=ANY,
        )
        self.assert_colon_free_chain(publish)

    @patch("apps.cron.publish.publish_task")
    def test_internal_driver_chain_skips_repair_blocked_store(self, publish):
        task_store = registered_store("journal.Task")
        goal_store = registered_store("journal.Goal")
        PlaceholderMigrationCursor.objects.create(
            tenant=self.tenant,
            store_label="journal.Task",
            mode=PlaceholderMigrationCursor.Mode.DRY_RUN,
            status=PlaceholderMigrationCursor.Status.SKIPPED,
        )

        with patch("apps.pii.historical_migration.registered_stores", return_value=(task_store, goal_store)):
            result = historical_placeholder_migration_driver_task(
                str(self.tenant.pk),
                retry_skipped=False,
            )

        self.assertEqual(result["store"], "journal.Goal")
        publish.assert_called_once_with(
            "historical_placeholder_migration_batch",
            str(self.tenant.pk),
            "journal.Goal",
            commit=False,
            batch_size=25,
            changed_attempts=0,
            idempotency_key=ANY,
        )
        self.assert_colon_free_chain(publish)

    @patch("apps.cron.publish.publish_task")
    @patch("apps.pii.historical_migration.process_store_batch")
    def test_completed_batch_chains_back_to_driver(self, process, publish):
        process.return_value = BatchResult(
            "journal.Task",
            "dry-run",
            True,
            False,
            False,
            0,
            "",
            {},
        )

        historical_placeholder_migration_batch_task(str(self.tenant.pk), "journal.Task")

        publish.assert_called_once_with(
            "historical_placeholder_migration_driver",
            str(self.tenant.pk),
            commit=False,
            batch_size=25,
            retry_skipped=False,
            idempotency_key=ANY,
        )
        self.assert_colon_free_chain(publish)

    @override_settings(W4_MIGRATION_TENANT_IDS="")
    @patch("apps.cron.publish.publish_task")
    @patch("apps.pii.historical_migration.process_store_batch")
    def test_stray_commit_batch_is_inert_when_allowlist_closed(self, process, publish):
        result = historical_placeholder_migration_batch_task(
            str(self.tenant.pk),
            "journal.Task",
            commit=True,
        )

        self.assertEqual(result["status"], "not_gated")
        process.assert_not_called()
        publish.assert_not_called()

    @override_settings(W4_MIGRATION_TENANT_IDS="")
    @patch("apps.cron.publish.publish_task")
    def test_stray_commit_driver_is_inert_when_allowlist_closed(self, publish):
        result = historical_placeholder_migration_driver_task(str(self.tenant.pk), commit=True)

        self.assertEqual(result["status"], "not_gated")
        publish.assert_not_called()

    @patch("apps.cron.publish.publish_task")
    @patch("apps.pii.historical_migration.process_store_batch")
    def test_flag_revoked_commit_batch_is_inert_at_fire_time(self, process, publish):
        self.tenant.layer1_placeholder_writes = False
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        with override_settings(W4_MIGRATION_TENANT_IDS=str(self.tenant.pk)):
            result = historical_placeholder_migration_batch_task(
                str(self.tenant.pk),
                "journal.Task",
                commit=True,
            )

        self.assertEqual(result["status"], "flag_disabled")
        process.assert_not_called()
        publish.assert_not_called()

    def test_allowlist_is_whitespace_and_case_safe_and_logs_populated_miss(self):
        raw = f" 00000000-0000-0000-0000-000000000000, {str(self.tenant.id).upper()} , "
        with override_settings(W4_MIGRATION_TENANT_IDS=raw):
            self.assertTrue(w4_migration_tenant_allowed(self.tenant))

        with (
            override_settings(W4_MIGRATION_TENANT_IDS="00000000-0000-0000-0000-000000000000"),
            self.assertLogs("apps.pii.historical_migration", level="INFO") as logs,
        ):
            self.assertFalse(w4_migration_tenant_allowed(self.tenant))
        self.assertTrue(any("is not in W4_MIGRATION_TENANT_IDS" in line for line in logs.output))

    @patch("apps.cron.publish.publish_task")
    @patch("apps.pii.historical_migration.process_store_batch")
    def test_changed_skip_chain_caps_then_advances(self, process, publish):
        process.return_value = BatchResult(
            "journal.Task",
            "dry-run",
            False,
            False,
            False,
            1,
            "",
            {("title", "changed_skipped"): 1},
        )
        historical_placeholder_migration_batch_task(
            str(self.tenant.pk),
            "journal.Task",
            changed_attempts=MAX_CHANGED_RECHAINS - 1,
        )
        self.assertEqual(publish.call_args.kwargs["changed_attempts"], MAX_CHANGED_RECHAINS)
        self.assertEqual(publish.call_args.kwargs["delay_seconds"], 30)
        self.assert_colon_free_chain(publish)

        publish.reset_mock()
        process.return_value = BatchResult(
            "journal.Task",
            "dry-run",
            False,
            False,
            False,
            1,
            "row-1",
            {("title", "changed_skipped"): 1, ("title", "changed_skipped_advanced"): 1},
        )
        historical_placeholder_migration_batch_task(
            str(self.tenant.pk),
            "journal.Task",
            changed_attempts=MAX_CHANGED_RECHAINS,
        )
        self.assertEqual(publish.call_args.kwargs["changed_attempts"], 0)
        self.assertIsNone(publish.call_args.kwargs["delay_seconds"])
        self.assertTrue(process.call_args.kwargs["advance_changed"])

    @patch("apps.pii.historical_migration.process_store_batch")
    def test_management_engine_uses_same_three_rechain_cap(self, process):
        changed = BatchResult(
            "journal.Task",
            "dry-run",
            False,
            False,
            False,
            1,
            "",
            {("title", "changed_skipped"): 1},
        )
        advanced = BatchResult(
            "journal.Task",
            "dry-run",
            False,
            False,
            False,
            1,
            "row-1",
            {("title", "changed_skipped"): 1, ("title", "changed_skipped_advanced"): 1},
        )
        done = BatchResult("journal.Task", "dry-run", True, False, False, 0, "row-1", {})
        process.side_effect = [changed, changed, changed, advanced, done]

        totals = migrate_tenant_registered_stores(self.tenant, store_label="journal.Task")

        self.assertEqual(totals["stores_complete"], 1)
        self.assertEqual(
            [call.kwargs["advance_changed"] for call in process.call_args_list],
            [False, False, False, True, False],
        )

    @patch("apps.pii.historical_migration.historical_placeholder_migration_driver_task", autospec=True)
    @patch("apps.cron.views.verify_qstash_signature", return_value=True)
    def test_task_map_round_trip_through_real_api_cron_url(self, _verify, driver):
        driver.return_value = {"status": "chained"}
        response = APIClient().post(
            "/api/cron/trigger/historical_placeholder_migration_driver/",
            data=json.dumps({"args": [str(self.tenant.pk)], "kwargs": {"commit": False, "batch_size": 3}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        driver.assert_called_once_with(str(self.tenant.pk), commit=False, batch_size=3)

    @patch("apps.pii.management.commands.migrate_placeholder_history.migrate_tenant_registered_stores")
    def test_management_command_is_dry_run_by_default(self, migrate):
        migrate.return_value = {"stores_complete": 1, "stores_skipped": 0, "batches": 1}

        call_command("migrate_placeholder_history", tenant_id=str(self.tenant.pk), store="journal.Task")

        migrate.assert_called_once_with(
            self.tenant,
            commit=False,
            batch_size=25,
            store_label="journal.Task",
        )


class HistoricalMigrationLeaseConcurrencyTests(TransactionTestCase):
    def setUp(self):
        user = User.objects.create_user(username=f"w4-lease-{uuid.uuid4()}", password="x")
        self.tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=True,
        )

    def test_second_real_worker_is_denied_while_first_lease_is_live(self):
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            try:
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                barrier.wait(timeout=5)
                return _claim_cursor(tenant, registered_store("journal.Task"), "commit") is not None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: worker(), range(2)))

        self.assertCountEqual(results, [True, False])
        cursor = PlaceholderMigrationCursor.objects.get(
            tenant=self.tenant,
            store_label="journal.Task",
            mode="commit",
        )
        self.assertGreater(cursor.lease_expires_at, timezone.now())
