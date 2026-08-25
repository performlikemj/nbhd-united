from __future__ import annotations

import io
import json
import time
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, override_settings

from apps.orchestrator.smoke_external_deps import (
    SmokeCheckResult,
    SmokeReport,
    SmokeSkipped,
    run_smoke,
)


def _report(*checks: SmokeCheckResult) -> SmokeReport:
    return SmokeReport(
        ok=all(check.ok for check in checks),
        build="build-123",
        checks=list(checks),
        total_ms=sum(check.ms for check in checks),
    )


class SmokeRunnerTests(SimpleTestCase):
    @override_settings(SENTRY_RELEASE="build-123")
    def test_aggregates_pass_failure_and_skip(self):
        def passes():
            return None

        def fails():
            raise RuntimeError("provider unavailable")

        def skips():
            raise SmokeSkipped("not configured")

        with patch(
            "apps.orchestrator.smoke_external_deps._CHECKS",
            {"pass": passes, "fail": fails, "skip": skips},
        ):
            report = run_smoke()

        self.assertFalse(report.ok)
        self.assertEqual(report.build, "build-123")
        by_name = {check.name: check for check in report.checks}
        self.assertTrue(by_name["pass"].ok)
        self.assertFalse(by_name["fail"].ok)
        self.assertEqual(by_name["fail"].error_type, "RuntimeError")
        self.assertTrue(by_name["skip"].ok)
        self.assertEqual(by_name["skip"].skipped_reason, "not configured")

    def test_per_check_timeout_is_a_failure_without_waiting_for_hanging_check(self):
        def hangs():
            time.sleep(0.5)

        started = time.monotonic()
        with (
            patch("apps.orchestrator.smoke_external_deps._CHECKS", {"hang": hangs}),
            patch("apps.orchestrator.smoke_external_deps.PER_CHECK_TIMEOUT_S", 0.03),
        ):
            report = run_smoke(budget_s=0.2)

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(report.ok)
        self.assertEqual(report.checks[0].error_type, "TimeoutError")

    def test_failure_logs_at_error(self):
        def fails():
            raise RuntimeError("nope")

        with (
            patch("apps.orchestrator.smoke_external_deps._CHECKS", {"stripe": fails}),
            self.assertLogs("apps.orchestrator.smoke_external_deps", level="ERROR") as logs,
        ):
            run_smoke()

        self.assertIn("smoke_external_deps FAILED: ['stripe']", "\n".join(logs.output))


@override_settings(DEPLOY_SECRET="deploy-test-secret")
class SmokeEndpointTests(SimpleTestCase):
    url = "/api/internal/deploy/smoke/"

    def setUp(self):
        self.client = Client()

    def test_missing_or_wrong_secret_matches_sibling_unauthorized_status(self):
        self.assertEqual(self.client.post(self.url).status_code, 401)
        self.assertEqual(
            self.client.post(self.url, HTTP_X_DEPLOY_SECRET="wrong").status_code,
            401,
        )

    def test_get_is_method_not_allowed(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_all_ok_returns_200(self):
        report = _report(SmokeCheckResult("db", True, 2))
        with patch("apps.orchestrator.smoke_external_deps.run_smoke", return_value=report):
            response = self.client.post(self.url, HTTP_X_DEPLOY_SECRET="deploy-test-secret")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["build"], "build-123")

    def test_one_failure_returns_503_and_names_check(self):
        report = _report(
            SmokeCheckResult(
                "azure_file_share_rw",
                False,
                15,
                error_type="RuntimeError",
                error_msg="unavailable",
            )
        )
        with patch("apps.orchestrator.smoke_external_deps.run_smoke", return_value=report):
            response = self.client.post(self.url, HTTP_X_DEPLOY_SECRET="deploy-test-secret")

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(response.json()["checks"][0]["name"], "azure_file_share_rw")


class SmokeManagementCommandTests(SimpleTestCase):
    def test_success_exit_and_json_shape(self):
        report = _report(SmokeCheckResult("db", True, 1))
        stdout = io.StringIO()
        with patch(
            "apps.orchestrator.management.commands.smoke_external_deps.run_smoke",
            return_value=report,
        ) as mocked:
            call_command("smoke_external_deps", "--json", "--only", "db,cache", stdout=stdout)

        mocked.assert_called_once_with(checks=["db", "cache"])
        self.assertEqual(json.loads(stdout.getvalue())["checks"][0]["name"], "db")

    def test_failure_exits_one(self):
        report = _report(SmokeCheckResult("stripe", False, 2, error_type="RuntimeError", error_msg="unavailable"))
        with (
            patch(
                "apps.orchestrator.management.commands.smoke_external_deps.run_smoke",
                return_value=report,
            ),
            self.assertRaises(CommandError) as raised,
        ):
            call_command("smoke_external_deps", stdout=io.StringIO(), stderr=io.StringIO())

        self.assertIn("stripe", str(raised.exception))
