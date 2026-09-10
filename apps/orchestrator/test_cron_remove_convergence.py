"""Lost cron.remove races must verify outcomes, including dependent recreates."""

from __future__ import annotations

import logging
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.cron.gateway_client import GatewayError
from apps.cron.models import CronJob
from apps.orchestrator.cron_reconcile import regenerate_tenant_crons
from apps.orchestrator.test_cron_reconcile_caps import _desired_row, _gateway_job
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

RECONCILE = "apps.orchestrator.cron_reconcile"
INVOKE = "apps.cron.gateway_client.invoke_gateway_tool"
PUBLISH = "apps.cron.publish.publish_task"


def _remove_error():
    # The gateway's generic body carries no evidence that the ID is missing.
    return GatewayError('{"type":"tool_error","message":"tool execution failed"}', status_code=500)


def _page(jobs, **metadata):
    return {"jobs": jobs, "total": len(jobs), **metadata}


def _at_job(name, *, stale=False, created=0):
    job = _gateway_job(name)
    job["schedule"] = {"kind": "at"}
    now_ms = int(timezone.now().timestamp() * 1000)
    job["state"] = {"nextRunAtMs": now_ms + (-7_200_000 if stale else 7_200_000)}
    job["createdAtMs"] = created
    return job


@override_settings(QSTASH_TOKEN="test-token", API_BASE_URL="https://example.test")
class CronRemoveConvergenceTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Remove Race", telegram_chat_id=864209754)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-remove-race"
        self.tenant.container_fqdn = "oc-remove-race.internal"
        self.tenant.postgres_cron_canonical = True
        self.tenant.save()
        self.invoke = self.enterContext(patch(INVOKE))
        self.publish = self.enterContext(patch(PUBLISH))
        self.cap_log = self.enterContext(patch(f"{RECONCILE}._log_at_cron_cap_breach"))

    def _cleanup_fixture(self, site):
        if site == "removed":
            return [_gateway_job("obsolete")]
        if site == "stuck_reaped":
            return [_at_job("stale", stale=True)]
        # Production's 200-job page cannot reach the excess-removal loop.
        # Lower the cap in these tests to exercise the same removal code.
        return [_at_job("newest", created=3), _at_job("older", created=2), _at_job("oldest", created=1)]

    def _make_recreation(self, name="Morning Briefing"):
        row = _desired_row(self.tenant, name)
        CronJob.objects.bulk_create([row])  # Avoid save-time task publication.
        stale = _gateway_job(name)
        stale["payload"] = {"kind": "agentTurn", "message": "old prompt"}
        return row, stale

    def _assert_verified(self):
        self.assertEqual(
            [call.args[1] for call in self.invoke.call_args_list],
            ["cron.list", "cron.remove", "cron.list"],
        )
        self.assertEqual(self.invoke.call_args_list[1].kwargs, {"error_log_level": logging.WARNING})
        self.assertEqual(self.invoke.call_args_list[2].args[2], {"includeDisabled": True})

    def test_cleanup_failed_remove_converges_only_after_observed_absence(self):
        for site in ("removed", "stuck_reaped", "cap_reaped"):
            with self.subTest(site=site), patch(f"{RECONCILE}._AT_CRON_CATASTROPHIC_CAP", 2):
                jobs = self._cleanup_fixture(site)
                self.invoke.reset_mock()
                self.invoke.side_effect = [_page(jobs), _remove_error(), _page(jobs[1:])]
                with self.assertLogs(RECONCILE, level="INFO") as logs:
                    result = regenerate_tenant_crons(self.tenant)
                self.assertEqual(result[site], 1)
                self.assertEqual(result["errors"], 0)
                self._assert_verified()
                self.assertIn("converged after live verification", "\n".join(logs.output))
                self.assertFalse(any(record.levelno >= logging.WARNING for record in logs.records))
        self.publish.assert_not_called()

    def test_cleanup_unknown_absence_preserves_error_at_every_site(self):
        full_page = [_gateway_job(f"unrelated-{i}") for i in range(200)]
        for site in ("removed", "stuck_reaped", "cap_reaped"):
            jobs = self._cleanup_fixture(site)
            observations = {
                "still live": _page(jobs),
                "verification failure": GatewayError("list failed", status_code=503),
                "invalid envelope": {"details": {}},
                "invalid entry": _page([None]),
                "missing identity": _page([{"name": "unknown"}]),
                "full page without metadata": {"jobs": full_page},
                "full page claiming complete": _page(full_page, hasMore=False),
                "short partial page": _page([], total=201, hasMore=True),
                "wrapped partial page": {"details": _page([], total=1)},
                "outer pagination": {"details": _page([]), "hasMore": True},
                "has more": _page([], hasMore=True),
                "next offset": _page([], nextOffset=1),
                "nonzero offset": _page([], offset=1),
                "invalid total": _page([], total="unknown"),
                "smaller page limit reached": {"jobs": full_page[:50], "limit": 50},
                "invalid limit": _page([], limit="unknown"),
            }
            for label, observation in observations.items():
                with (
                    self.subTest(site=site, observation=label),
                    patch(f"{RECONCILE}._AT_CRON_CATASTROPHIC_CAP", 2),
                ):
                    self.invoke.reset_mock()
                    self.invoke.side_effect = [_page(jobs), _remove_error(), observation]
                    with self.assertLogs(RECONCILE, level="WARNING") as logs:
                        result = regenerate_tenant_crons(self.tenant)
                    self.assertEqual(result[site], 0)
                    self.assertEqual(result["errors"], 1)
                    if site == "stuck_reaped":
                        self.assertEqual(result["at_pending"], 1)
                    self._assert_verified()
                    if not isinstance(observation, GatewayError):
                        self.assertTrue(any(record.levelno >= logging.ERROR for record in logs.records))
        self.publish.assert_not_called()

    def test_short_legacy_and_wrapped_lists_can_prove_absence(self):
        for observation in (
            [],
            {"jobs": []},
            {"details": _page([], hasMore=False, offset=0, nextOffset=None, limit=200)},
            _page([_gateway_job(f"unrelated-{i}") for i in range(199)]),
        ):
            with self.subTest(observation=observation):
                self.invoke.reset_mock()
                self.invoke.side_effect = [_page([_gateway_job("obsolete")]), _remove_error(), observation]
                result = regenerate_tenant_crons(self.tenant)
                self.assertEqual(result["removed"], 1)
                self.assertEqual(result["errors"], 0)
                self._assert_verified()

    def test_stale_absence_excludes_snapshot_id_and_prevents_false_hard_cap_alert(self):
        pending = [_at_job(f"pending-{i}") for i in range(50)]
        stale = _at_job("stale", stale=True)
        # Cover OpenClaw's alternate ID field as well.
        stale["jobId"] = stale.pop("id")
        self.invoke.side_effect = [_page([stale, *pending]), _remove_error(), _page(pending)]
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["stuck_reaped"], 1)
        self.assertEqual(result["at_pending"], 50)
        self.assertEqual(result["errors"], 0)
        self.cap_log.assert_not_called()

    def test_recreation_accepts_one_matching_replacement_without_duplicate_add(self):
        row, stale = self._make_recreation()
        replacement = _gateway_job(row.name)
        replacement["jobId"] = "replacement-id"
        replacement.pop("id")
        self.invoke.side_effect = [_page([stale]), _remove_error(), _page([replacement])]
        with self.assertLogs(RECONCILE, level="INFO") as logs:
            result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["recreated"], 1)
        self.assertEqual(result["errors"], 0)
        self._assert_verified()
        row.refresh_from_db()
        self.assertIsNotNone(row.last_pushed_to_container_at)
        self.publish.assert_not_called()
        self.assertFalse(any(record.levelno >= logging.WARNING for record in logs.records))

    def test_missing_replacement_requests_fresh_pass_which_adds_and_updates_timestamp(self):
        row, stale = self._make_recreation()
        self.invoke.side_effect = [_page([stale]), _remove_error(), _page([])]
        with self.assertLogs(RECONCILE, level="WARNING") as logs:
            first = regenerate_tenant_crons(self.tenant)
        self.assertEqual(first["errors"], 1)
        self.assertEqual(first["recreated"], 0)
        self._assert_verified()
        self.assertIn("replacement is missing", "\n".join(logs.output))
        self.publish.assert_called_once_with("regenerate_tenant_crons", str(self.tenant.id), delay_seconds=30)
        row.refresh_from_db()
        self.assertIsNone(row.last_pushed_to_container_at)

        # Model delivery of that scheduled pass: it plans from a fresh list.
        self.invoke.reset_mock()
        self.invoke.side_effect = [_page([]), {"ok": True}]
        second = regenerate_tenant_crons(self.tenant)
        self.assertEqual(second["added"], 1)
        self.assertEqual(second["errors"], 0)
        self.assertEqual([call.args[1] for call in self.invoke.call_args_list], ["cron.list", "cron.add"])
        self.assertEqual(self.invoke.call_args_list[1].args[2]["job"]["payload"]["message"], row.name)
        row.refresh_from_db()
        self.assertIsNotNone(row.last_pushed_to_container_at)

    def test_unresolved_recreation_never_adds_or_claims_success_and_requests_retry(self):
        row, stale = self._make_recreation()
        replacement = {**_gateway_job(row.name), "id": "new-id"}
        observations = {
            "old still live": _page([stale, replacement]),
            "replacement drifted": _page([{**stale, "id": "new-id"}]),
            "replacement malformed": _page([{**replacement, "schedule": "invalid"}]),
            "duplicate replacements": _page([replacement, {**replacement, "id": "another-id"}]),
            "truncated with matching replacement": _page([replacement], total=201, hasMore=True),
            "failed list": GatewayError("list failed", status_code=503),
            "invalid list": {"details": {}},
        }
        for label, observation in observations.items():
            with self.subTest(observation=label):
                self.invoke.reset_mock()
                self.publish.reset_mock()
                self.invoke.side_effect = [_page([stale]), _remove_error(), observation]
                with self.assertLogs(RECONCILE, level="WARNING"):
                    result = regenerate_tenant_crons(self.tenant)
                self.assertEqual(result["errors"], 1)
                self.assertEqual(result["recreated"], 0)
                self._assert_verified()
                self.publish.assert_called_once_with("regenerate_tenant_crons", str(self.tenant.id), delay_seconds=30)
                row.refresh_from_db()
                self.assertIsNone(row.last_pushed_to_container_at)

    def test_multiple_unresolved_recreations_schedule_only_one_followup(self):
        _, stale_a = self._make_recreation("A")
        _, stale_b = self._make_recreation("B")
        self.invoke.side_effect = [
            _page([stale_a, stale_b]),
            _remove_error(),
            _page([stale_b]),
            _remove_error(),
            _page([]),
        ]
        result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["errors"], 2)
        self.assertEqual(result["recreated"], 0)
        self.publish.assert_called_once_with("regenerate_tenant_crons", str(self.tenant.id), delay_seconds=30)

    def test_retry_publication_failure_remains_visible_without_claiming_recreation(self):
        row, stale = self._make_recreation()
        self.invoke.side_effect = [_page([stale]), _remove_error(), _page([])]
        self.publish.side_effect = RuntimeError("queue unavailable")
        with self.assertLogs(RECONCILE, level="ERROR") as logs:
            result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["recreated"], 0)
        self.publish.assert_called_once()
        self.assertIn("failed to schedule removal recovery", "\n".join(logs.output))

    @override_settings(QSTASH_TOKEN="")
    def test_unconfigured_queue_does_not_recursively_run_reconciliation(self):
        _, stale = self._make_recreation()
        self.invoke.side_effect = [_page([stale]), _remove_error(), _page([])]
        with self.assertLogs(RECONCILE, level="ERROR") as logs:
            result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["errors"], 1)
        self._assert_verified()
        self.publish.assert_not_called()
        self.assertIn("cannot schedule removal recovery", "\n".join(logs.output))

    def test_hibernated_initial_list_remains_quiet_noop(self):
        self.invoke.side_effect = GatewayError("hibernated", unavailable=True)
        with self.assertLogs(RECONCILE, level="INFO") as logs:
            result = regenerate_tenant_crons(self.tenant)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(self.invoke.call_count, 1)
        self.publish.assert_not_called()
        self.assertFalse(any(record.levelno >= logging.WARNING for record in logs.records))

    def test_hibernation_during_remove_or_verification_does_not_enqueue_recovery(self):
        _, stale = self._make_recreation()
        for during_verification in (False, True):
            with self.subTest(during_verification=during_verification):
                self.invoke.reset_mock()
                self.invoke.side_effect = [
                    _page([stale]),
                    *([_remove_error()] if during_verification else []),
                    GatewayError("hibernated", unavailable=True),
                ]
                result = regenerate_tenant_crons(self.tenant)
                self.assertEqual(result["recreated"], 0)
                self.assertEqual(result["errors"], 1)  # Existing mid-pass accounting.
                self.assertEqual(self.invoke.call_count, 3 if during_verification else 2)
                self.publish.assert_not_called()
