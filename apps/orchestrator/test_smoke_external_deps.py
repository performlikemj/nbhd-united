from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import yaml
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, SimpleTestCase, override_settings

from apps.orchestrator.smoke_external_deps import (
    SmokeCheck,
    SmokeCheckResult,
    SmokeReport,
    SmokeSkipped,
    _check_gemini_tts,
    run_smoke,
)


def _report(*checks: SmokeCheckResult) -> SmokeReport:
    return SmokeReport(
        ok=all(check.ok for check in checks),
        build="build-123",
        checks=list(checks),
        total_ms=sum(check.ms for check in checks),
    )


def _gemini_response(audio: bytes | None):
    part = SimpleNamespace(
        inline_data=SimpleNamespace(data=audio) if audio is not None else None,
        text=None if audio is not None else "ok",
    )
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
    )


class _FakeGeminiModels:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeGeminiClient:
    def __init__(self, outcomes):
        self.models = _FakeGeminiModels(outcomes)


@override_settings(GEMINI_API_KEY="test-gemini-key", GEMINI_TTS_MODEL="test-gemini-model")
class GeminiTtsSmokeTests(SimpleTestCase):
    def _patch_dependencies(self, client):
        return (
            patch("apps.orchestrator.azure_client._is_mock", return_value=False),
            patch("apps.core.render.make_gemini_client", return_value=client),
            patch("apps.orchestrator.smoke_external_deps.time.sleep"),
        )

    def test_retries_no_audio_then_passes_when_audio_arrives(self):
        client = _FakeGeminiClient([_gemini_response(None), _gemini_response(b"pcm")])
        mock_is_mock, mock_make_client, mock_sleep = self._patch_dependencies(client)

        with mock_is_mock, mock_make_client as make_client, mock_sleep as sleep:
            _check_gemini_tts()

        self.assertEqual(len(client.models.calls), 2)
        make_client.assert_called_once_with("test-gemini-key", timeout_ms=10_000)
        sleep.assert_called_once_with(2)
        self.assertTrue(client.models.calls[0]["contents"].endswith("Take a slow breath in, and let it go gently."))

    def test_always_no_audio_fails_with_attempt_count(self):
        client = _FakeGeminiClient([_gemini_response(None)] * 2)
        mock_is_mock, mock_make_client, mock_sleep = self._patch_dependencies(client)

        with (
            mock_is_mock,
            mock_make_client,
            mock_sleep as sleep,
            self.assertRaisesRegex(RuntimeError, "failed after 2 attempts: no audio bytes"),
        ):
            _check_gemini_tts()

        self.assertEqual(len(client.models.calls), 2)
        self.assertEqual(sleep.call_args_list, [call(2)])

    def test_rate_limit_is_skipped(self):
        client = _FakeGeminiClient([RuntimeError("429 RESOURCE_EXHAUSTED")])
        mock_is_mock, mock_make_client, mock_sleep = self._patch_dependencies(client)

        with (
            mock_is_mock,
            mock_make_client,
            mock_sleep as sleep,
            self.assertRaisesRegex(SmokeSkipped, "^gemini rate-limited$"),
        ):
            _check_gemini_tts()

        self.assertEqual(len(client.models.calls), 1)
        sleep.assert_not_called()

    def test_deadline_invalid_argument_fails_without_retry(self):
        client = _FakeGeminiClient([RuntimeError("400 INVALID_ARGUMENT: Manually set deadline 3s is too short")])
        mock_is_mock, mock_make_client, mock_sleep = self._patch_dependencies(client)

        with (
            mock_is_mock,
            mock_make_client,
            mock_sleep as sleep,
            self.assertRaisesRegex(RuntimeError, "INVALID_ARGUMENT.*deadline"),
        ):
            _check_gemini_tts()

        self.assertEqual(len(client.models.calls), 1)
        sleep.assert_not_called()


class SmokeWorkflowConfigTests(SimpleTestCase):
    def test_real_calls_step_uses_compatible_curl_failure_flags(self):
        workflow_path = Path(__file__).parents[2] / ".github/workflows/ci-cd.yml"
        workflow = yaml.safe_load(workflow_path.read_text())
        steps = (step for job in workflow["jobs"].values() for step in job.get("steps", []))
        step = next(step for step in steps if step.get("name") == "Smoke external dependencies (real calls)")
        run = step["run"]

        has_fail = re.search(r"(^|\s)--fail(\s|$)", run) is not None
        self.assertFalse(has_fail and "--fail-with-body" in run)
        self.assertIn("--fail-with-body", run)
        self.assertIn("X-Deploy-Secret", run)
        self.assertIn("/api/internal/deploy/smoke/", run)


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
            {"pass": SmokeCheck(passes), "fail": SmokeCheck(fails), "skip": SmokeCheck(skips)},
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

    def test_per_check_timeout_override_is_honored_while_default_check_passes(self):
        def slow():
            time.sleep(2)

        def passes():
            return None

        started = time.monotonic()
        with patch(
            "apps.orchestrator.smoke_external_deps._CHECKS",
            {"slow": SmokeCheck(slow, timeout_s=1), "pass": SmokeCheck(passes)},
        ):
            report = run_smoke()

        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(report.ok)
        by_name = {check.name: check for check in report.checks}
        self.assertEqual(by_name["slow"].error_type, "TimeoutError")
        self.assertTrue(by_name["pass"].ok)

    def test_failure_logs_at_error(self):
        def fails():
            raise RuntimeError("nope")

        with (
            patch("apps.orchestrator.smoke_external_deps._CHECKS", {"stripe": SmokeCheck(fails)}),
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
