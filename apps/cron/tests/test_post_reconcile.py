from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import call, patch
from uuid import UUID

from django.test import SimpleTestCase, TestCase, override_settings

from apps.cron.gateway_client import GatewayError, invoke_gateway_tool
from apps.cron.models import CronJob
from apps.cron.post_reconcile import (
    _dated_sync_disposition,
    _sweep_ghost_jobs,
    run_post_reconcile_maintenance,
)
from apps.tenants.models import Tenant, User


def _tenant(tenant_id: str = "11111111-1111-1111-1111-111111111111"):
    return SimpleNamespace(
        id=UUID(tenant_id),
        user=SimpleNamespace(timezone="Asia/Tokyo"),
    )


def _job(
    job_id: str,
    expr: str,
    *,
    name: str | None = None,
    kind: str = "cron",
) -> dict:
    return {
        "id": job_id,
        "name": name or f"_sync:{job_id}",
        "schedule": {"kind": kind, "expr": expr},
    }


class DatedSyncDispositionTest(SimpleTestCase):
    def test_uses_tenant_local_today_at_utc_midnight_boundary(self):
        tenant = _tenant()
        jobs = [
            _job("past", "0 9 26 7 *"),
            _job("margin", "0 9 27 7 *"),
            _job("future", "0 9 30 7 *"),
        ]

        # 2026-07-28 15:30 UTC is 2026-07-29 00:30 in Tokyo. The July 26
        # job is therefore three local dates old and must be swept; using
        # UTC's July 28 would incorrectly retain it at the two-day margin.
        with (
            patch(
                "django.utils.timezone.now",
                return_value=datetime(2026, 7, 28, 15, 30, tzinfo=UTC),
            ),
            patch("apps.cron.post_reconcile.cron_remove") as mock_remove,
        ):
            summary = _sweep_ghost_jobs(tenant, jobs)

        self.assertEqual(summary["swept"], 1)
        self.assertEqual(summary["skipped_future"], 1)
        mock_remove.assert_called_once_with(
            tenant,
            job_id="past",
            error_log_level=logging.WARNING,
        )

    def test_two_day_margin_is_not_more_than_two_days_past(self):
        today = date(2026, 7, 29)
        self.assertEqual(
            _dated_sync_disposition(_job("three-days", "0 8 26 7 *"), today=today),
            "remove",
        )
        self.assertEqual(
            _dated_sync_disposition(_job("two-days", "0 8 27 7 *"), today=today),
            "keep",
        )
        self.assertEqual(
            _dated_sync_disposition(_job("tomorrow", "0 8 30 7 *"), today=today),
            "future",
        )

    def test_year_wrap_180_day_boundary_is_conservative(self):
        today = date(2026, 1, 1)
        self.assertEqual(
            _dated_sync_disposition(_job("day-180", "0 8 30 6 *"), today=today),
            "future",
        )
        self.assertEqual(
            _dated_sync_disposition(_job("day-181", "0 8 1 7 *"), today=today),
            "remove",
        )

    def test_invalid_calendar_date_is_skipped_and_warned(self):
        tenant = _tenant()
        invalid = _job("invalid", "0 8 30 2 *")

        with (
            self.assertLogs("apps.cron.post_reconcile", level="WARNING") as logs,
            patch("apps.cron.post_reconcile.tenant_today", return_value=date(2026, 7, 29)),
            patch("apps.cron.post_reconcile.cron_remove") as mock_remove,
        ):
            summary = _sweep_ghost_jobs(tenant, [invalid])

        self.assertEqual(summary["skipped_invalid"], 1)
        mock_remove.assert_not_called()
        self.assertIn(
            f"cron_ghost_sweep_invalid tenant={tenant.id} job=invalid expr=0 8 30 2 *",
            "\n".join(logs.output),
        )

    def test_non_sync_recurring_and_non_dated_jobs_are_untouched(self):
        today = date(2026, 7, 29)
        untouched = [
            _job("non-sync", "0 8 1 1 *", name="User reminder"),
            _job("recurring", "0 8 * * *"),
            _job("non-dated", "0 8 1 * *"),
            _job("at-kind", "0 8 1 1 *", kind="at"),
        ]
        for job in untouched:
            with self.subTest(job=job["id"]):
                self.assertEqual(_dated_sync_disposition(job, today=today), "keep")


class GhostSweepOrchestrationTest(SimpleTestCase):
    def setUp(self):
        self.tenant = _tenant()
        self.resync_patch = patch(
            "apps.cron.post_reconcile._resync_gateway_job_ids",
            return_value=0,
        )
        self.resync_patch.start()
        self.addCleanup(self.resync_patch.stop)
        self.today_patch = patch(
            "apps.cron.post_reconcile.tenant_today",
            return_value=date(2026, 7, 29),
        )
        self.today_patch.start()
        self.addCleanup(self.today_patch.stop)

    @override_settings(CRON_GHOST_SWEEP_TENANTS="")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_disabled_gate_does_nothing(self, mock_invoke):
        summary = run_post_reconcile_maintenance(self.tenant)
        self.assertEqual(summary["swept"], 0)
        mock_invoke.assert_not_called()

    @patch("apps.cron.post_reconcile.cron_remove")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_allowlist_gates_per_tenant(self, mock_invoke, mock_remove):
        mock_invoke.return_value = {"details": {"jobs": [_job("ghost", "0 8 1 7 *")]}}

        with override_settings(CRON_GHOST_SWEEP_TENANTS=str(self.tenant.id)):
            run_post_reconcile_maintenance(self.tenant)
        with override_settings(CRON_GHOST_SWEEP_TENANTS="22222222-2222-2222-2222-222222222222"):
            run_post_reconcile_maintenance(self.tenant)

        mock_invoke.assert_called_once_with(
            self.tenant,
            "cron.list",
            {"includeDisabled": True},
        )
        mock_remove.assert_called_once_with(
            self.tenant,
            job_id="ghost",
            error_log_level=logging.WARNING,
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch("apps.cron.post_reconcile.cron_remove")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_star_gate_sweeps_only_ghosts(self, mock_invoke, mock_remove):
        mock_invoke.return_value = {
            "details": {
                "jobs": [
                    _job("ghost", "0 8 1 7 *"),
                    _job("future", "0 8 30 7 *"),
                    _job("recurring", "0 8 * * *"),
                    _job("user", "0 8 1 7 *", name="User cron"),
                ]
            }
        }

        summary = run_post_reconcile_maintenance(self.tenant)

        self.assertEqual(summary["swept"], 1)
        self.assertEqual(summary["skipped_future"], 1)
        mock_remove.assert_called_once_with(
            self.tenant,
            job_id="ghost",
            error_log_level=logging.WARNING,
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch("apps.cron.post_reconcile.cron_remove")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_sweep_caps_at_100_and_logs_deferred(self, mock_invoke, mock_remove):
        mock_invoke.return_value = {"jobs": [_job(f"ghost-{index}", "0 8 1 7 *") for index in range(102)]}

        with self.assertLogs("apps.cron.post_reconcile", level="INFO") as logs:
            summary = run_post_reconcile_maintenance(self.tenant)

        self.assertEqual(mock_remove.call_count, 100)
        self.assertEqual(
            mock_remove.call_args_list,
            [
                call(
                    self.tenant,
                    job_id=f"ghost-{index}",
                    error_log_level=logging.WARNING,
                )
                for index in range(100)
            ],
        )
        self.assertEqual(summary["swept"], 100)
        self.assertEqual(summary["deferred"], 2)
        self.assertIn(
            f"cron_ghost_sweep tenant={self.tenant.id} list_size=102 "
            "list_total=102 list_has_more=False swept=100 failed=0 "
            "deferred=2 skipped_future=0 skipped_invalid=0",
            "\n".join(logs.output),
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch(
        "apps.cron.post_reconcile.cron_remove",
        side_effect=GatewayError("missing id", status_code=409),
    )
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_missing_id_conflict_is_idempotent_success(self, mock_invoke, mock_remove):
        mock_invoke.side_effect = [
            {"jobs": [_job("already-gone", "0 8 1 7 *")]},
            {"jobs": []},
        ]

        summary = run_post_reconcile_maintenance(self.tenant)

        self.assertEqual(summary["swept"], 1)
        mock_remove.assert_called_once_with(
            self.tenant,
            job_id="already-gone",
            error_log_level=logging.WARNING,
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch(
        "apps.cron.post_reconcile.cron_remove",
        side_effect=GatewayError("Gateway returned 500: tool_error", status_code=500),
    )
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_failed_remove_with_absent_id_is_converged_success(self, mock_invoke, mock_remove):
        mock_invoke.side_effect = [
            {"jobs": [_job("race-loser", "0 8 1 7 *")]},
            {"jobs": []},
        ]

        with self.assertLogs("apps.cron.post_reconcile", level="INFO") as logs:
            summary = run_post_reconcile_maintenance(self.tenant)

        self.assertEqual(summary["swept"], 1)
        self.assertEqual(summary["failed"], 0)
        mock_remove.assert_called_once_with(
            self.tenant,
            job_id="race-loser",
            error_log_level=logging.WARNING,
        )
        self.assertIn("cron_ghost_sweep_remove_converged", "\n".join(logs.output))
        self.assertNotIn("cron_ghost_sweep_remove_failed", "\n".join(logs.output))

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch("apps.cron.post_reconcile.cron_remove")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_failed_remove_with_present_id_retries_once_and_fails(self, mock_invoke, mock_remove):
        live_jobs = {
            "details": {
                "jobs": [
                    _job("flaky", "0 8 1 7 *"),
                    _job("healthy", "0 8 1 7 *"),
                ]
            }
        }
        mock_invoke.side_effect = [live_jobs, live_jobs]

        attempts: dict[str, int] = {}

        def _remove(_tenant, *, job_id, error_log_level):
            self.assertEqual(error_log_level, logging.WARNING)
            attempts[job_id] = attempts.get(job_id, 0) + 1
            if job_id == "flaky":
                raise GatewayError(
                    "Gateway returned 500: tool_error",
                    status_code=500,
                )

        mock_remove.side_effect = _remove
        with self.assertLogs("apps.cron.post_reconcile", level="WARNING") as logs:
            summary = run_post_reconcile_maintenance(self.tenant)

        self.assertEqual(summary["swept"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(
            mock_remove.call_args_list,
            [
                call(self.tenant, job_id="flaky", error_log_level=logging.WARNING),
                call(self.tenant, job_id="flaky", error_log_level=logging.WARNING),
                call(self.tenant, job_id="healthy", error_log_level=logging.WARNING),
            ],
        )
        joined = "\n".join(logs.output)
        self.assertIn("cron_ghost_sweep_remove_retry", joined)
        self.assertIn("cron_ghost_sweep_remove_failed", joined)
        self.assertEqual(sum(record.levelno >= logging.ERROR for record in logs.records), 1)

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch("apps.cron.post_reconcile.cron_remove")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_list_receipt_records_visible_page_and_daemon_total(self, mock_invoke, mock_remove):
        jobs = [
            _job("visible-1", "0 8 * * *"),
            _job("visible-2", "0 8 * * *"),
        ]
        mock_invoke.return_value = {
            "details": {
                "jobs": jobs,
                "total": 1155,
                "offset": 0,
                "limit": 200,
                "hasMore": True,
                "nextOffset": 200,
            }
        }

        with self.assertLogs("apps.cron.post_reconcile", level="INFO") as logs:
            summary = run_post_reconcile_maintenance(self.tenant)

        self.assertEqual(summary["list_size"], 2)
        mock_remove.assert_not_called()
        mock_invoke.assert_called_once_with(
            self.tenant,
            "cron.list",
            {"includeDisabled": True},
        )
        self.assertIn(
            f"cron_ghost_sweep tenant={self.tenant.id} list_size=2 list_total=1155 list_has_more=True",
            "\n".join(logs.output),
        )


class GatewayClientErrorLogLevelTest(SimpleTestCase):
    def setUp(self):
        self.tenant = SimpleNamespace(
            id=UUID("33333333-3333-3333-3333-333333333333"),
            container_fqdn="oc-log-level.example.com",
            internal_api_key="test-key",
        )
        self.response = SimpleNamespace(
            status_code=500,
            text='{"ok":false,"error":{"type":"tool_error"}}',
        )

    @patch("apps.cron.gateway_client.requests.post")
    def test_demoted_non_200_emits_no_error_record(self, mock_post):
        mock_post.return_value = self.response

        with (
            self.assertLogs("apps.cron.gateway_client", level="WARNING") as logs,
            self.assertRaises(GatewayError),
        ):
            invoke_gateway_tool(
                self.tenant,
                "cron.remove",
                {"jobId": "race-loser"},
                error_log_level=logging.WARNING,
            )

        self.assertTrue(any(record.levelno == logging.WARNING for record in logs.records))
        self.assertFalse(any(record.levelno >= logging.ERROR for record in logs.records))

    @patch("apps.cron.gateway_client.requests.post")
    def test_default_non_200_still_emits_error_record(self, mock_post):
        mock_post.return_value = self.response

        with (
            self.assertLogs("apps.cron.gateway_client", level="ERROR") as logs,
            self.assertRaises(GatewayError),
        ):
            invoke_gateway_tool(
                self.tenant,
                "cron.list",
                {"includeDisabled": True},
            )

        self.assertEqual(sum(record.levelno >= logging.ERROR for record in logs.records), 1)


class GatewayJobIdResyncTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="resync-user", password="testpass123")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_id="oc-resync",
            container_fqdn="oc-resync.example.com",
            postgres_cron_canonical=True,
        )

    def _row(self, name: str, gateway_job_id: str = "dead-id", *, managed: bool = True) -> CronJob:
        return CronJob.objects.create(
            tenant=self.tenant,
            name=name,
            gateway_job_id=gateway_job_id,
            data={},
            managed=managed,
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_single_name_match_updates_from_same_live_list(self, mock_invoke):
        row = self._row("Morning Briefing")
        mock_invoke.return_value = {
            "details": {
                "jobs": [
                    {
                        "id": "live-id",
                        "name": "Morning Briefing",
                        "schedule": {"kind": "cron", "expr": "0 7 * * *"},
                    }
                ]
            }
        }

        with self.assertLogs("apps.cron.post_reconcile", level="INFO") as logs:
            summary = run_post_reconcile_maintenance(self.tenant)

        row.refresh_from_db()
        self.assertEqual(row.gateway_job_id, "live-id")
        self.assertEqual(summary["resynced"], 1)
        mock_invoke.assert_called_once_with(
            self.tenant,
            "cron.list",
            {"includeDisabled": True},
        )
        self.assertIn(
            f"cron_gateway_id_resync tenant={self.tenant.id} row={row.pk} old=dead-id new=live-id",
            "\n".join(logs.output),
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_multiple_name_matches_warn_and_leave_row_untouched(self, mock_invoke):
        row = self._row("Duplicated")
        mock_invoke.return_value = {
            "jobs": [
                {"id": "live-1", "name": "Duplicated"},
                {"id": "live-2", "name": "Duplicated"},
            ]
        }

        with self.assertLogs("apps.cron.post_reconcile", level="WARNING") as logs:
            summary = run_post_reconcile_maintenance(self.tenant)

        row.refresh_from_db()
        self.assertEqual(row.gateway_job_id, "dead-id")
        self.assertEqual(summary["resynced"], 0)
        self.assertIn(
            f"cron_gateway_id_resync tenant={self.tenant.id} row={row.pk} old=dead-id matches=2",
            "\n".join(logs.output),
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_absent_name_logs_info_and_leaves_row_untouched(self, mock_invoke):
        row = self._row("Missing")
        mock_invoke.return_value = {"jobs": [{"id": "other-id", "name": "Other"}]}

        with self.assertLogs("apps.cron.post_reconcile", level="INFO") as logs:
            summary = run_post_reconcile_maintenance(self.tenant)

        row.refresh_from_db()
        self.assertEqual(row.gateway_job_id, "dead-id")
        self.assertEqual(summary["resynced"], 0)
        self.assertIn(
            f"cron_gateway_id_resync tenant={self.tenant.id} row={row.pk} old=dead-id matches=0",
            "\n".join(logs.output),
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="*")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_sync_job_is_never_a_resync_target(self, mock_invoke):
        row = self._row("_sync:Internal")
        mock_invoke.return_value = {"jobs": [{"id": "sync-live", "name": "_sync:Internal"}]}

        with self.assertLogs("apps.cron.post_reconcile", level="INFO") as logs:
            summary = run_post_reconcile_maintenance(self.tenant)

        row.refresh_from_db()
        self.assertEqual(row.gateway_job_id, "dead-id")
        self.assertEqual(summary["resynced"], 0)
        self.assertIn(
            f"cron_gateway_id_resync tenant={self.tenant.id} row={row.pk} old=dead-id matches=0",
            "\n".join(logs.output),
        )

    @override_settings(CRON_GHOST_SWEEP_TENANTS="")
    @patch("apps.cron.post_reconcile.invoke_gateway_tool")
    def test_disabled_gate_does_not_resync(self, mock_invoke):
        row = self._row("Morning Briefing")

        summary = run_post_reconcile_maintenance(self.tenant)

        row.refresh_from_db()
        self.assertEqual(row.gateway_job_id, "dead-id")
        self.assertEqual(summary["resynced"], 0)
        mock_invoke.assert_not_called()
