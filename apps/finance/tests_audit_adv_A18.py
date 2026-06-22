"""Audit tests for FA-0536 — FinanceSnapshot monthly cron pipeline.

Verifies that create_monthly_snapshots_task is wired into TASK_MAP and that
a matching SYSTEM_CRONS entry exists in register_system_crons, so FinanceSnapshot
rows are actually written in production.
"""

from __future__ import annotations

from django.test import SimpleTestCase


class FinanceSnapshotCronWiringTests(SimpleTestCase):
    """FA-0536 — the monthly finance snapshot must be reachable via the cron pipeline."""

    def test_task_wrapper_exists_and_is_callable(self):
        """create_monthly_snapshots_task must be importable from apps.finance.tasks."""
        from apps.finance.tasks import create_monthly_snapshots_task

        self.assertTrue(callable(create_monthly_snapshots_task))

    def test_task_map_contains_snapshot_finance_monthly(self):
        """TASK_MAP must include 'snapshot_finance_monthly' pointing at the new task."""
        from apps.cron.views import TASK_MAP

        self.assertIn(
            "snapshot_finance_monthly",
            TASK_MAP,
            "TASK_MAP is missing 'snapshot_finance_monthly' — the cron trigger "
            "endpoint will return 404 and the monthly FinanceSnapshot will never be written.",
        )
        self.assertEqual(
            TASK_MAP["snapshot_finance_monthly"],
            "apps.finance.tasks.create_monthly_snapshots_task",
        )

    def test_system_cron_entry_present(self):
        """SYSTEM_CRONS must include a monthly entry for snapshot-finance-monthly."""
        from apps.cron.management.commands.register_system_crons import SYSTEM_CRONS

        names = [name for name, _cron, _path in SYSTEM_CRONS]
        self.assertIn(
            "snapshot-finance-monthly",
            names,
            "SYSTEM_CRONS is missing 'snapshot-finance-monthly' — the QStash schedule "
            "will never be registered and FinanceSnapshot rows will never be created.",
        )

    def test_system_cron_fires_monthly_on_first(self):
        """The cron expression must target the 1st of each month."""
        from apps.cron.management.commands.register_system_crons import SYSTEM_CRONS

        entry = next(
            ((name, cron, path) for name, cron, path in SYSTEM_CRONS if name == "snapshot-finance-monthly"),
            None,
        )
        self.assertIsNotNone(entry, "snapshot-finance-monthly not found in SYSTEM_CRONS")
        _name, cron_expr, path = entry
        # Day-of-month field (3rd field, 0-indexed) must be '1'
        fields = cron_expr.split()
        self.assertEqual(len(fields), 5, f"Unexpected cron expression: {cron_expr!r}")
        self.assertEqual(fields[2], "1", f"Day-of-month field should be '1', got {fields[2]!r}")
        self.assertIn("/snapshot_finance_monthly/", path)

    def test_task_wrapper_delegates_to_snapshot_module(self):
        """create_monthly_snapshots_task must delegate to create_monthly_snapshots."""
        import inspect

        from apps.finance.tasks import create_monthly_snapshots_task

        source = inspect.getsource(create_monthly_snapshots_task)
        self.assertIn(
            "create_monthly_snapshots",
            source,
            "Task wrapper must call create_monthly_snapshots from the snapshot module.",
        )
