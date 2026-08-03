"""sautai Phase 0 M2M client + QStash task + notify tests.

The response-shape fixtures are copied verbatim from sautai's
``api/tests/fixtures/m2m/`` (docs/sautai-phase0-contract.md — the fixture
handshake gate: both sides decode the same bytes). Both Phase 0 endpoints are
exercised against the golden fixtures: ``/generate/`` (async QStash task) via
``CallSautaiGeneratePlanTests`` and ``/current/`` (fast synchronous read) via
``FetchSautaiCurrentPlanTests``.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .models import Integration, SautaiMealPlanAddressedBy, SautaiMealPlanJob, SautaiMealPlanJobStatus
from .sautai_client import call_sautai_generate_plan, fetch_sautai_current_plan
from .tasks import generate_sautai_meal_plan_task, recover_sautai_generation_jobs_task

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "m2m"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES_DIR / name).read_text())


def _mock_response(fixture: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = fixture["status_code"]
    resp.json.return_value = fixture["body"]
    return resp


class SautaiGoldenFixtureContractTests(SimpleTestCase):
    """The checked-in copies must decode the authoritative async/fill-only bytes."""

    def test_all_generate_fixtures_are_async_acknowledgements(self):
        for name in (
            "generate_ok.json",
            "generate_ok_funnel.json",
            "generate_regenerated.json",
            "generate_user_created.json",
        ):
            with self.subTest(name=name):
                fixture = _load_fixture(name)
                self.assertEqual(fixture["status_code"], 202)
                self.assertEqual(
                    set(fixture["body"]) & {"job_id", "status_url", "regeneration"},
                    {"job_id", "status_url", "regeneration"},
                )
                self.assertNotIn("plan", fixture["body"])

    def test_generate_ok_declares_fill_only_without_replacements(self):
        regeneration = _load_fixture("generate_ok.json")["body"]["regeneration"]
        self.assertEqual(regeneration["mode"], "fill_gaps_and_replace_listed_slots")
        self.assertIs(regeneration["requested"], False)
        self.assertEqual(regeneration["replace_slots"], [])

    def test_regenerated_fixture_requires_explicit_replacement_slots(self):
        regeneration = _load_fixture("generate_regenerated.json")["body"]["regeneration"]
        self.assertIs(regeneration["requested"], True)
        self.assertEqual(regeneration["replace_slots"], [{"day": "Monday", "meal_type": "Dinner"}])


# ═════════════════════════════════════════════════════════════════════
# call_sautai_generate_plan — parses the real sautai contract fixtures
# ═════════════════════════════════════════════════════════════════════


@override_settings(SAUTAI_M2M_BASE_URL="https://app.sautai.test", SAUTAI_PLATFORM_SECRET="test-secret")
class CallSautaiGeneratePlanTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sautai Client Test", telegram_chat_id=848484)
        self.tenant.user.email = "diner@example.com"
        self.tenant.user.save(update_fields=["email"])
        Integration.objects.create(
            tenant=self.tenant,
            provider=Integration.Provider.SAUTAI,
            status=Integration.Status.ACTIVE,
            sautai_user_id=501,
        )

    def _job(self, **kwargs) -> SautaiMealPlanJob:
        return SautaiMealPlanJob.objects.create(tenant=self.tenant, **kwargs)

    def test_generate_ok_persists_async_job_for_polling(self):
        from .sautai_client import ASYNC_GENERATION_STATE_KEY, SAUTAI_GENERATE_POLL_PENDING

        fixture = _load_fixture("generate_ok.json")
        job = self._job(week_start=date(2026, 8, 3))

        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post:
            action = call_sautai_generate_plan(job)

        job.refresh_from_db()
        self.assertEqual(action, SAUTAI_GENERATE_POLL_PENDING)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)
        state = job.result[ASYNC_GENERATION_STATE_KEY]
        self.assertEqual(state["job_id"], fixture["body"]["job_id"])
        self.assertEqual(
            state["status_url"],
            f"https://app.sautai.test/api/m2m/generation-jobs/{fixture['body']['job_id']}/",
        )
        self.assertEqual(state["poll_attempts"], 0)
        self.assertEqual(state["poll_generation"], 1)
        self.assertEqual(state["regeneration"], fixture["body"]["regeneration"])
        self.assertIs(job.funnel["async_contract_request_enabled"], False)
        self.assertNotIn("async_contract_confirmed", job.funnel)
        from .sautai_client import sautai_async_contract_confirmed

        self.assertFalse(sautai_async_contract_confirmed())
        self.assertEqual(job.error, "")
        self.assertEqual(job.addressed_by, SautaiMealPlanAddressedBy.LINKED_ID)
        self.assertEqual(job.sautai_user_id, 501)

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["X-NBHD-Platform-Secret"], "test-secret")
        self.assertEqual(kwargs["json"]["sautai_user_id"], 501)
        self.assertNotIn("user_email", kwargs["json"])
        self.assertEqual(kwargs["json"]["week_start"], "2026-08-03")
        # The first request preserves the legacy server's exact budget. Its 202
        # alone is not enough to shorten later acknowledgement calls.
        self.assertEqual(kwargs["timeout"], 125.0)

    def test_post_timeout_shortens_only_after_ack_and_valid_status_poll(self):
        from .sautai_client import (
            ASYNC_GENERATION_STATE_KEY,
            sautai_async_contract_confirmed,
            sautai_poll_generation_filter,
        )

        fixture = _load_fixture("generate_ok.json")
        first = self._job()
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as first_post:
            call_sautai_generate_plan(first)

        self.assertEqual(first_post.call_args.kwargs["timeout"], 125.0)
        self.assertFalse(sautai_async_contract_confirmed())
        first.refresh_from_db()
        poll_generation = first.result[ASYNC_GENERATION_STATE_KEY]["poll_generation"]
        claimed = SautaiMealPlanJob.objects.filter(
            id=first.id,
            status=SautaiMealPlanJobStatus.PENDING,
            **sautai_poll_generation_filter(poll_generation),
        ).update(status=SautaiMealPlanJobStatus.GENERATING)
        self.assertEqual(claimed, 1)
        first.refresh_from_db()
        running = {
            "status_code": 200,
            "body": {
                "status": "running",
                "remaining_count": 4,
                "failed_slots": [],
                "plan_id": 123,
                "week_start_date": "2026-08-03",
            },
        }
        with patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(running)) as status_get:
            call_sautai_generate_plan(first, poll_generation=poll_generation)

        self.assertTrue(sautai_async_contract_confirmed())
        self.assertEqual(status_get.call_args.kwargs["headers"]["X-NBHD-Platform-Secret"], "test-secret")

        second = self._job(funnel={"async_contract_request_enabled": sautai_async_contract_confirmed()})
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as second_post:
            call_sautai_generate_plan(second)

        self.assertEqual(second_post.call_args.kwargs["timeout"], 20.0)
        second.refresh_from_db()
        self.assertIs(second.funnel["async_contract_request_enabled"], True)

    def test_worker_uses_admission_snapshot_without_second_global_read(self):
        from .sautai_client import ASYNC_CONTRACT_REQUEST_DECISION_KEY

        job = self._job(funnel={ASYNC_CONTRACT_REQUEST_DECISION_KEY: True})
        with (
            patch(
                "apps.integrations.sautai_client.sautai_async_contract_confirmed",
                side_effect=AssertionError("worker re-read global capability after admission"),
            ) as global_capability,
            patch(
                "apps.integrations.sautai_client.httpx.post",
                return_value=_mock_response(_load_fixture("generate_ok.json")),
            ) as mock_post,
        ):
            call_sautai_generate_plan(job)

        global_capability.assert_not_called()
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 20.0)

    def test_malformed_202_does_not_flip_or_shorten_the_next_post(self):
        from .sautai_client import sautai_async_contract_confirmed

        malformed_ack = {"status_code": 202, "body": {}}
        first = self._job()
        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=_mock_response(malformed_ack),
        ) as first_post:
            call_sautai_generate_plan(first)

        first.refresh_from_db()
        self.assertEqual(first.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("malformed async generation acknowledgement", first.error)
        self.assertEqual(first_post.call_args.kwargs["timeout"], 125.0)
        self.assertFalse(sautai_async_contract_confirmed())

        second = self._job()
        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=_mock_response(_load_fixture("current_ok.json")),
        ) as second_post:
            call_sautai_generate_plan(second)

        self.assertEqual(second_post.call_args.kwargs["timeout"], 125.0)
        self.assertFalse(sautai_async_contract_confirmed())

    def test_generate_user_created_fixture_uses_same_async_shape(self):
        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        fixture = _load_fixture("generate_user_created.json")
        job = self._job()

        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            call_sautai_generate_plan(job)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)
        self.assertEqual(job.result[ASYNC_GENERATION_STATE_KEY]["job_id"], fixture["body"]["job_id"])

    def test_legacy_synchronous_200_marks_job_ready_exactly_as_before(self):
        fixture = _load_fixture("current_ok.json")
        job = self._job(week_start=date(2026, 8, 3))

        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready") as mock_notify,
        ):
            action = call_sautai_generate_plan(job)

        job.refresh_from_db()
        self.assertIsNone(action)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.READY)
        self.assertEqual(job.result, fixture["body"]["plan"])
        self.assertEqual(job.web_link, fixture["body"]["web_link"])
        self.assertEqual(job.funnel["complete"], fixture["body"]["complete"])
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 125.0)
        mock_notify.assert_called_once()

    def test_invalid_secret_marks_job_failed_with_safe_error(self):
        fixture = _load_fixture("error_invalid_secret.json")
        job = self._job()

        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            call_sautai_generate_plan(job)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("401", job.error)
        self.assertIn("invalid_secret", job.error)

    def test_rehydrates_user_prompt_before_egress(self):
        self.tenant.pii_entity_map = {"[PERSON_0]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        fixture = _load_fixture("generate_ok.json")
        job = self._job(user_prompt="cook more for [PERSON_0]")

        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post:
            call_sautai_generate_plan(job)

        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["user_prompt"], "cook more for Alice")

    def test_no_link_fails_without_network_call(self):
        Integration.objects.filter(
            tenant=self.tenant,
            provider=Integration.Provider.SAUTAI,
        ).delete()
        job = self._job()

        with patch("apps.integrations.sautai_client.httpx.post") as mock_post:
            call_sautai_generate_plan(job)

        mock_post.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("sautai_link_required", job.error)
        self.assertIn("connection invitation in Fuel", job.error)

    def test_missing_platform_secret_fails_without_network_call(self):
        fixture = _load_fixture("generate_ok.json")
        job = self._job()

        with (
            override_settings(SAUTAI_PLATFORM_SECRET=""),
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post,
        ):
            call_sautai_generate_plan(job)

        mock_post.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("not_configured", job.error)

    # ── Retryable vs terminal failure classification (QStash redelivery) ──

    def test_busy_503_marks_failed_and_raises_retryable(self):
        from .sautai_client import RetryableSautaiError

        busy = {"status_code": 503, "body": {"status": "error", "code": "busy", "detail": "in progress"}}
        job = self._job()
        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(busy)),
            self.assertRaises(RetryableSautaiError),
        ):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("503", job.error)

    def test_5xx_marks_failed_and_raises_retryable(self):
        from .sautai_client import RetryableSautaiError

        err = {"status_code": 500, "body": {"status": "error", "code": "generation_failed", "detail": "boom"}}
        job = self._job()
        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(err)),
            self.assertRaises(RetryableSautaiError),
        ):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)

    def test_cloudflare_524_on_post_remains_retryable(self):
        from .sautai_client import RetryableSautaiError

        timeout = {"status_code": 524, "body": {"status": "error", "code": "timeout", "detail": "upstream"}}
        job = self._job()
        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(timeout)) as mock_post,
            self.assertRaisesRegex(RetryableSautaiError, "sautai_error_524"),
        ):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("sautai_error_524", job.error)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 125.0)

    def test_transport_error_marks_failed_and_raises_retryable(self):
        import httpx

        from .sautai_client import RetryableSautaiError

        job = self._job()
        with (
            patch("apps.integrations.sautai_client.httpx.post", side_effect=httpx.ConnectTimeout("boom")),
            self.assertRaises(RetryableSautaiError),
        ):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("request_failed", job.error)

    def test_terminal_4xx_marks_failed_without_raising(self):
        # 400 bad request can never succeed on retry — terminal, no raise.
        bad = {"status_code": 400, "body": {"status": "error", "code": "validation", "detail": "bad"}}
        job = self._job()
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(bad)):
            call_sautai_generate_plan(job)  # must NOT raise
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("400", job.error)

    @override_settings(QSTASH_TOKEN="qstash-test", API_BASE_URL="https://nbhd.test")
    def test_redelivery_after_retryable_failure_reclaims_and_succeeds(self):
        # The whole point of raising: delivery 1 (503) fails the job and raises;
        # QStash redelivers; delivery 2 re-claims the FAILED row and succeeds.
        from .sautai_client import RetryableSautaiError

        job = self._job(week_start=date(2026, 8, 3))
        busy = {"status_code": 503, "body": {"status": "error", "code": "busy", "detail": "in progress"}}
        ok = _load_fixture("generate_ok.json")

        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(busy)),
            self.assertRaises(RetryableSautaiError),
        ):
            generate_sautai_meal_plan_task(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)

        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(ok)),
            patch("apps.cron.publish.publish_task"),
        ):
            generate_sautai_meal_plan_task(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)


@override_settings(SAUTAI_M2M_BASE_URL="https://app.sautai.test", SAUTAI_PLATFORM_SECRET="test-secret")
class AsyncSautaiGenerationTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sautai Async Test", telegram_chat_id=848583)
        Integration.objects.create(
            tenant=self.tenant,
            provider=Integration.Provider.SAUTAI,
            status=Integration.Status.ACTIVE,
            sautai_user_id=501,
        )

    def _acknowledged_job(self) -> SautaiMealPlanJob:
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 8, 3))
        fixture = _load_fixture("generate_ok.json")
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        return job

    def _claim_poll(self, job: SautaiMealPlanJob) -> int:
        from .sautai_client import ASYNC_GENERATION_STATE_KEY, sautai_poll_generation_filter

        state = job.result[ASYNC_GENERATION_STATE_KEY]
        generation = state["poll_generation"]
        claimed = SautaiMealPlanJob.objects.filter(
            id=job.id,
            status=SautaiMealPlanJobStatus.PENDING,
            **sautai_poll_generation_filter(generation),
        ).update(status=SautaiMealPlanJobStatus.GENERATING)
        self.assertEqual(claimed, 1)
        job.refresh_from_db()
        return generation

    def test_running_status_updates_same_row_and_requests_another_poll(self):
        from .sautai_client import (
            ASYNC_CONTRACT_EVENT_VALIDATED,
            ASYNC_GENERATION_STATE_KEY,
            SAUTAI_GENERATE_POLL_PENDING,
            sautai_async_contract_confirmed,
        )

        job = self._acknowledged_job()
        running = {
            "status_code": 200,
            "body": {
                "status": "running",
                "remaining_count": 4,
                "failed_slots": [],
                "plan_id": 123,
                "week_start_date": "2026-08-03",
                "future_addition": {"safe": True},
            },
        }
        poll_generation = self._claim_poll(job)
        with (
            patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(running)) as mock_get,
            patch("apps.integrations.sautai_client.httpx.post") as mock_post,
        ):
            action = call_sautai_generate_plan(job, poll_generation=poll_generation)

        job.refresh_from_db()
        self.assertEqual(action, SAUTAI_GENERATE_POLL_PENDING)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)
        state = job.result[ASYNC_GENERATION_STATE_KEY]
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["remaining_count"], 4)
        self.assertEqual(state["poll_attempts"], 1)
        self.assertNotIn("future_addition", state)
        self.assertIs(job.funnel["async_contract_confirmed"], True)
        self.assertEqual(job.funnel["async_contract_capability"], ASYNC_CONTRACT_EVENT_VALIDATED)
        self.assertTrue(sautai_async_contract_confirmed())
        mock_post.assert_not_called()
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 10)

    def test_capability_recording_waits_for_http_json_and_contract_validation(self):
        from .sautai_client import sautai_async_contract_confirmed

        non_json = MagicMock()
        non_json.status_code = 200
        non_json.json.side_effect = ValueError("not json")
        invalid_responses = (
            (
                "http",
                _mock_response({"status_code": 401, "body": {"code": "invalid_secret"}}),
            ),
            ("json", non_json),
            ("contract", _mock_response({"status_code": 200, "body": {"status": "running"}})),
        )

        for validation_layer, response in invalid_responses:
            with self.subTest(validation_layer=validation_layer):
                job = self._acknowledged_job()
                poll_generation = self._claim_poll(job)
                with (
                    patch("apps.integrations.sautai_client.httpx.get", return_value=response),
                    patch(
                        "apps.integrations.sautai_client._record_validated_async_contract",
                        side_effect=AssertionError("capability recorded before status validation"),
                    ) as record_capability,
                ):
                    call_sautai_generate_plan(job, poll_generation=poll_generation)

                record_capability.assert_not_called()
                job.refresh_from_db()
                self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
                self.assertFalse(sautai_async_contract_confirmed())

    def test_poll_401_and_transport_timeout_do_not_flip_capability(self):
        import httpx

        from .sautai_client import SAUTAI_GENERATE_POLL_PENDING, sautai_async_contract_confirmed

        failures = (
            (
                "unauthorized",
                {"return_value": _mock_response({"status_code": 401, "body": {"code": "invalid_secret"}})},
                SautaiMealPlanJobStatus.FAILED,
                None,
            ),
            (
                "timeout",
                {"side_effect": httpx.ReadTimeout("status poll timed out")},
                SautaiMealPlanJobStatus.PENDING,
                SAUTAI_GENERATE_POLL_PENDING,
            ),
        )

        for failure, get_behavior, expected_status, expected_action in failures:
            with self.subTest(failure=failure):
                job = self._acknowledged_job()
                poll_generation = self._claim_poll(job)
                with patch("apps.integrations.sautai_client.httpx.get", **get_behavior):
                    action = call_sautai_generate_plan(job, poll_generation=poll_generation)

                job.refresh_from_db()
                self.assertEqual(action, expected_action)
                self.assertEqual(job.status, expected_status)
                self.assertNotIn("async_contract_capability", job.funnel)
                self.assertFalse(sautai_async_contract_confirmed())

    def test_validated_flip_then_legacy_200_reverts_capability(self):
        from .sautai_client import sautai_async_contract_confirmed

        validated = self._acknowledged_job()
        running = {
            "status_code": 200,
            "body": {
                "status": "running",
                "remaining_count": 4,
                "failed_slots": [],
                "plan_id": 123,
                "week_start_date": "2026-08-03",
            },
        }
        poll_generation = self._claim_poll(validated)
        with patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(running)):
            call_sautai_generate_plan(validated, poll_generation=poll_generation)
        self.assertTrue(sautai_async_contract_confirmed())

        legacy = SautaiMealPlanJob.objects.create(
            tenant=self.tenant,
            week_start=date(2026, 8, 10),
            funnel={"async_contract_request_enabled": sautai_async_contract_confirmed()},
        )
        with (
            patch(
                "apps.integrations.sautai_client.httpx.post",
                return_value=_mock_response(_load_fixture("current_ok.json")),
            ) as legacy_post,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready"),
            self.assertLogs("apps.integrations.sautai_client", level="WARNING") as logs,
        ):
            call_sautai_generate_plan(legacy)

        legacy.refresh_from_db()
        self.assertEqual(legacy_post.call_args.kwargs["timeout"], 20.0)
        self.assertEqual(legacy.status, SautaiMealPlanJobStatus.READY)
        self.assertEqual(legacy.funnel["async_contract_capability"], "legacy")
        self.assertIs(legacy.funnel["async_contract_confirmed"], False)
        self.assertTrue(any("reverted async capability" in message for message in logs.output))
        self.assertFalse(sautai_async_contract_confirmed())

        after_revert = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 8, 17))
        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=_mock_response(_load_fixture("current_ok.json")),
        ) as next_post:
            call_sautai_generate_plan(after_revert)
        self.assertEqual(next_post.call_args.kwargs["timeout"], 125.0)

    def test_false_snapshot_late_legacy_200_reverts_concurrent_flip(self):
        from .sautai_client import ASYNC_CONTRACT_REQUEST_DECISION_KEY, sautai_async_contract_confirmed

        slow_legacy = SautaiMealPlanJob.objects.create(
            tenant=self.tenant,
            week_start=date(2026, 8, 10),
            funnel={ASYNC_CONTRACT_REQUEST_DECISION_KEY: False},
        )

        validated = self._acknowledged_job()
        running = {
            "status_code": 200,
            "body": {
                "status": "running",
                "remaining_count": 4,
                "failed_slots": [],
                "plan_id": 123,
                "week_start_date": "2026-08-03",
            },
        }
        poll_generation = self._claim_poll(validated)
        with patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(running)):
            call_sautai_generate_plan(validated, poll_generation=poll_generation)
        self.assertTrue(sautai_async_contract_confirmed())

        with (
            patch(
                "apps.integrations.sautai_client.httpx.post",
                return_value=_mock_response(_load_fixture("current_ok.json")),
            ) as legacy_post,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready"),
        ):
            call_sautai_generate_plan(slow_legacy)

        slow_legacy.refresh_from_db()
        self.assertEqual(legacy_post.call_args.kwargs["timeout"], 125.0)
        self.assertIs(slow_legacy.funnel[ASYNC_CONTRACT_REQUEST_DECISION_KEY], False)
        self.assertEqual(slow_legacy.funnel["async_contract_capability"], "legacy")
        self.assertFalse(sautai_async_contract_confirmed())

    def test_completed_fetches_current_and_uses_legacy_ready_path(self):
        job = self._acknowledged_job()
        completed = {
            "status_code": 200,
            "body": {
                "status": "completed",
                "remaining_count": 0,
                "failed_slots": [],
                "plan_id": 1,
                "week_start_date": "2026-08-03",
                "future_addition": "ignored",
            },
        }
        current = _load_fixture("current_ok.json")
        current["body"]["future_addition"] = {"ignored": True}
        poll_generation = self._claim_poll(job)
        with (
            patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(completed)),
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(current)),
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready") as mock_notify,
        ):
            action = call_sautai_generate_plan(job, poll_generation=poll_generation)

        job.refresh_from_db()
        self.assertIsNone(action)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.READY)
        self.assertEqual(job.result, current["body"]["plan"])
        self.assertEqual(job.funnel["generation_status"], "completed")
        self.assertEqual(job.funnel["failed_slot_count"], 0)
        mock_notify.assert_called_once()

    def test_completed_with_failures_is_ready_with_honest_failed_slot_count(self):
        job = self._acknowledged_job()
        completed = {
            "status_code": 200,
            "body": {
                "status": "completed_with_failures",
                "remaining_count": 0,
                "failed_slots": [
                    {"day": "Tuesday", "meal_type": "Lunch"},
                    {"day": "Friday", "meal_type": "Dinner"},
                ],
                "plan_id": 1,
                "week_start_date": "2026-08-03",
            },
        }
        poll_generation = self._claim_poll(job)
        with (
            patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(completed)),
            patch(
                "apps.integrations.sautai_client.httpx.post",
                return_value=_mock_response(_load_fixture("current_ok.json")),
            ),
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready"),
        ):
            call_sautai_generate_plan(job, poll_generation=poll_generation)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.READY)
        self.assertEqual(job.funnel["generation_status"], "completed_with_failures")
        self.assertEqual(job.funnel["failed_slot_count"], 2)
        self.assertEqual(job.funnel["failed_slots"], completed["body"]["failed_slots"])

    def test_failed_remote_status_uses_existing_failure_path(self):
        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        job = self._acknowledged_job()
        failed = {
            "status_code": 200,
            "body": {
                "status": "failed",
                "remaining_count": 3,
                "failed_slots": [{"day": "Monday", "meal_type": "Dinner"}],
                "plan_id": 1,
                "week_start_date": "2026-08-03",
            },
        }
        poll_generation = self._claim_poll(job)
        with (
            patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(failed)),
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready") as mock_notify,
        ):
            action = call_sautai_generate_plan(job, poll_generation=poll_generation)

        job.refresh_from_db()
        self.assertIsNone(action)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("1 failed slot", job.error)
        self.assertEqual(job.result[ASYNC_GENERATION_STATE_KEY]["status"], "failed")
        mock_notify.assert_not_called()

    def test_polling_stops_after_ten_minutes_without_network(self):
        from django.utils import timezone

        from .sautai_client import ASYNC_GENERATION_STATE_KEY, sautai_async_contract_confirmed

        job = self._acknowledged_job()
        state = job.result[ASYNC_GENERATION_STATE_KEY]
        state["started_at"] = (timezone.now() - timedelta(minutes=10, seconds=1)).isoformat()
        job.result = {ASYNC_GENERATION_STATE_KEY: state}
        job.save(update_fields=["result", "updated_at"])
        poll_generation = self._claim_poll(job)

        with patch("apps.integrations.sautai_client.httpx.get") as mock_get:
            action = call_sautai_generate_plan(job, poll_generation=poll_generation)

        job.refresh_from_db()
        self.assertIsNone(action)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("sautai_poll_timeout", job.error)
        self.assertFalse(sautai_async_contract_confirmed())
        mock_get.assert_not_called()

    def test_completed_with_failures_and_remaining_work_fails_closed(self):
        job = self._acknowledged_job()
        contradictory = {
            "status_code": 200,
            "body": {
                "status": "completed_with_failures",
                "remaining_count": 1,
                "failed_slots": [{"day": "Tuesday", "meal_type": "Lunch"}],
                "plan_id": 1,
                "week_start_date": "2026-08-03",
                "future_addition": "ignored",
            },
        }
        poll_generation = self._claim_poll(job)

        with (
            patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(contradictory)),
            patch("apps.integrations.sautai_client.httpx.post") as mock_post,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready") as mock_notify,
        ):
            call_sautai_generate_plan(job, poll_generation=poll_generation)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("malformed generation status", job.error)
        mock_post.assert_not_called()
        mock_notify.assert_not_called()

    def test_status_io_cannot_run_past_the_strict_deadline(self):
        from django.utils import timezone

        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        job = self._acknowledged_job()
        base = timezone.now()
        state = job.result[ASYNC_GENERATION_STATE_KEY]
        state["started_at"] = (base - timedelta(seconds=599)).isoformat()
        job.result = {ASYNC_GENERATION_STATE_KEY: state}
        job.save(update_fields=["result", "updated_at"])
        poll_generation = self._claim_poll(job)
        completed = {
            "status_code": 200,
            "body": {
                "status": "completed",
                "remaining_count": 0,
                "failed_slots": [],
                "plan_id": 1,
                "week_start_date": "2026-08-03",
            },
        }

        with (
            patch(
                "apps.integrations.sautai_client.timezone.now",
                side_effect=[base, base, base + timedelta(seconds=2), base + timedelta(seconds=2)],
            ),
            patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(completed)) as mock_get,
            patch("apps.integrations.sautai_client.httpx.post") as mock_post,
        ):
            call_sautai_generate_plan(job, poll_generation=poll_generation)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("sautai_poll_timeout", job.error)
        self.assertEqual(mock_get.call_args.kwargs["timeout"], 1.0)
        mock_post.assert_not_called()

    def test_current_plan_io_uses_remaining_budget_and_fails_after_deadline(self):
        from django.utils import timezone

        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        job = self._acknowledged_job()
        base = timezone.now()
        state = job.result[ASYNC_GENERATION_STATE_KEY]
        state["started_at"] = (base - timedelta(seconds=595)).isoformat()
        job.result = {ASYNC_GENERATION_STATE_KEY: state}
        job.save(update_fields=["result", "updated_at"])
        poll_generation = self._claim_poll(job)
        completed = {
            "status_code": 200,
            "body": {
                "status": "completed",
                "remaining_count": 0,
                "failed_slots": [],
                "plan_id": 1,
                "week_start_date": "2026-08-03",
            },
        }
        current = _load_fixture("current_ok.json")

        with (
            patch(
                "apps.integrations.sautai_client.timezone.now",
                side_effect=[
                    base,
                    base,
                    base,
                    base,
                    base,
                    base + timedelta(seconds=6),
                    base + timedelta(seconds=6),
                ],
            ),
            patch("apps.integrations.sautai_client.httpx.get", return_value=_mock_response(completed)),
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(current)) as mock_post,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready") as mock_notify,
        ):
            call_sautai_generate_plan(job, poll_generation=poll_generation)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("sautai_poll_timeout", job.error)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 5.0)
        mock_notify.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# generate_sautai_meal_plan_task — claim guard (idempotent on redelivery)
# ═════════════════════════════════════════════════════════════════════


class GenerateSautaiMealPlanTaskTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sautai Task Test", telegram_chat_id=848585)
        Integration.objects.create(
            tenant=self.tenant,
            provider=Integration.Provider.SAUTAI,
            status=Integration.Status.ACTIVE,
            sautai_user_id=501,
        )

    def test_missing_job_is_a_noop(self):
        generate_sautai_meal_plan_task(str(uuid4()))  # must not raise

    def test_pending_job_is_claimed_and_generated(self):
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant)
        # Patched at the SOURCE module — tasks.py locally re-imports this
        # name at call time (docs/agents/backend.md local-reimport pattern).
        with patch("apps.integrations.sautai_client.call_sautai_generate_plan") as mock_call:
            generate_sautai_meal_plan_task(str(job.id))
        mock_call.assert_called_once()
        claimed_job = mock_call.call_args.args[0]
        self.assertEqual(claimed_job.id, job.id)
        # The atomic CAS must have already flipped the row to GENERATING
        # before call_sautai_generate_plan is ever invoked.
        self.assertEqual(claimed_job.status, SautaiMealPlanJobStatus.GENERATING)

    def test_ready_job_is_skipped(self):
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, status=SautaiMealPlanJobStatus.READY)
        with patch("apps.integrations.sautai_client.call_sautai_generate_plan") as mock_call:
            generate_sautai_meal_plan_task(str(job.id))
        mock_call.assert_not_called()

    def test_failed_job_is_retried(self):
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, status=SautaiMealPlanJobStatus.FAILED)
        with patch("apps.integrations.sautai_client.call_sautai_generate_plan") as mock_call:
            generate_sautai_meal_plan_task(str(job.id))
        mock_call.assert_called_once()

    def test_overlapping_qstash_deliveries_second_one_skips(self):
        """Two QStash deliveries for the same job racing the atomic claim.

        Simulates the real hazard: delivery A's claim UPDATE lands first
        (job now GENERATING); delivery B arrives (retry, redelivery, or a
        genuine race) and must find zero claimable rows and skip WITHOUT
        ever calling the HTTP client — no second sautai call, no second
        completion notify.
        """
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant)

        # Delivery A's claim (the real generate_sautai_meal_plan_task would
        # do this itself; asserting the guard in isolation from the mocked
        # HTTP call, mirroring how render_meditation's CAS is unit-tested).
        first_claim = SautaiMealPlanJob.objects.filter(
            id=job.id,
            status__in=[SautaiMealPlanJobStatus.PENDING, SautaiMealPlanJobStatus.FAILED],
        ).update(status=SautaiMealPlanJobStatus.GENERATING)
        self.assertEqual(first_claim, 1)

        # Delivery B — the task's own claim attempt now finds nothing to
        # take, and must never reach the HTTP client.
        with patch("apps.integrations.sautai_client.call_sautai_generate_plan") as mock_call:
            generate_sautai_meal_plan_task(str(job.id))
        mock_call.assert_not_called()

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.GENERATING)

    def test_retryable_failure_propagates_out_of_task(self):
        # The task must NOT swallow a retryable failure — it has to reach
        # trigger_task (500) so QStash redelivers.
        from .sautai_client import RetryableSautaiError

        job = SautaiMealPlanJob.objects.create(tenant=self.tenant)
        with (
            patch(
                "apps.integrations.sautai_client.call_sautai_generate_plan",
                side_effect=RetryableSautaiError("busy"),
            ),
            self.assertRaises(RetryableSautaiError),
        ):
            generate_sautai_meal_plan_task(str(job.id))

    def test_terminal_failure_does_not_propagate(self):
        # A terminal failure returns normally from the client — the task must
        # not raise (QStash returns 200, no retry).
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant)
        with patch("apps.integrations.sautai_client.call_sautai_generate_plan", return_value=None):
            generate_sautai_meal_plan_task(str(job.id))  # must not raise

    @override_settings(
        QSTASH_TOKEN="qstash-test",
        API_BASE_URL="https://nbhd.test",
        SAUTAI_M2M_BASE_URL="https://app.sautai.test",
        SAUTAI_PLATFORM_SECRET="test-secret",
    )
    def test_async_flow_reenqueues_delayed_without_sleep_and_finishes_once(self):
        ack = _mock_response(_load_fixture("generate_ok.json"))
        current = _mock_response(_load_fixture("current_ok.json"))
        running = _mock_response(
            {
                "status_code": 200,
                "body": {
                    "status": "running",
                    "remaining_count": 2,
                    "failed_slots": [],
                    "plan_id": 1,
                    "week_start_date": "2026-08-03",
                },
            }
        )
        completed = _mock_response(
            {
                "status_code": 200,
                "body": {
                    "status": "completed",
                    "remaining_count": 0,
                    "failed_slots": [],
                    "plan_id": 1,
                    "week_start_date": "2026-08-03",
                },
            }
        )
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 8, 3))

        with (
            patch("apps.integrations.sautai_client.httpx.post", side_effect=[ack, current]),
            patch("apps.integrations.sautai_client.httpx.get", side_effect=[running, completed]),
            patch("apps.cron.publish.publish_task") as mock_publish,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready") as mock_notify,
        ):
            generate_sautai_meal_plan_task(str(job.id))
            job.refresh_from_db()
            self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING, job.error)
            first_generation = mock_publish.call_args_list[0].kwargs["poll_generation"]

            generate_sautai_meal_plan_task(str(job.id), poll_generation=first_generation)
            job.refresh_from_db()
            self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)
            second_generation = mock_publish.call_args_list[1].kwargs["poll_generation"]

            generate_sautai_meal_plan_task(str(job.id), poll_generation=second_generation)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.READY)
        self.assertEqual(mock_publish.call_count, 2)
        first_publish = mock_publish.call_args_list[0]
        second_publish = mock_publish.call_args_list[1]
        self.assertEqual(first_publish.args[:2], ("generate_sautai_meal_plan", str(job.id)))
        self.assertEqual(first_publish.kwargs["delay_seconds"], 15)
        self.assertEqual(first_publish.kwargs["poll_generation"], 1)
        self.assertEqual(second_publish.kwargs["poll_generation"], 2)
        self.assertTrue(first_publish.kwargs["idempotency_key"].endswith("-1"))
        self.assertTrue(second_publish.kwargs["idempotency_key"].endswith("-2"))
        mock_notify.assert_called_once()

    @override_settings(
        QSTASH_TOKEN="qstash-test",
        API_BASE_URL="https://nbhd.test",
        SAUTAI_M2M_BASE_URL="https://app.sautai.test",
        SAUTAI_PLATFORM_SECRET="test-secret",
    )
    def test_serial_redelivery_cannot_fork_the_poll_chain(self):
        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        ack = _mock_response(_load_fixture("generate_ok.json"))
        running = _mock_response(
            {
                "status_code": 200,
                "body": {
                    "status": "running",
                    "remaining_count": 2,
                    "failed_slots": [],
                    "plan_id": 1,
                    "week_start_date": "2026-08-03",
                },
            }
        )
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 8, 3))

        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=ack),
            patch("apps.integrations.sautai_client.httpx.get", return_value=running) as mock_get,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            generate_sautai_meal_plan_task(str(job.id))
            first_generation = mock_publish.call_args.kwargs["poll_generation"]
            generate_sautai_meal_plan_task(str(job.id), poll_generation=first_generation)

            job.refresh_from_db()
            self.assertEqual(job.result[ASYNC_GENERATION_STATE_KEY]["poll_generation"], 2)
            self.assertEqual(mock_publish.call_count, 2)

            # The original delivery arrives again after the row returned to
            # PENDING. Its generation no longer matches, so it cannot poll or
            # publish another branch.
            generate_sautai_meal_plan_task(str(job.id), poll_generation=first_generation)

        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(mock_publish.call_count, 2)

    @override_settings(
        QSTASH_TOKEN="",
        API_BASE_URL="",
        SAUTAI_M2M_BASE_URL="https://app.sautai.test",
        SAUTAI_PLATFORM_SECRET="test-secret",
    )
    def test_async_ack_without_qstash_fails_instead_of_remaining_pending(self):
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant)
        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=_mock_response(_load_fixture("generate_ok.json")),
        ):
            generate_sautai_meal_plan_task(str(job.id))

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("QStash is not configured", job.error)

    @override_settings(
        QSTASH_TOKEN="qstash-test",
        API_BASE_URL="https://nbhd.test",
        SAUTAI_M2M_BASE_URL="https://app.sautai.test",
        SAUTAI_PLATFORM_SECRET="test-secret",
    )
    def test_dropped_successor_is_recovered_with_a_new_generation(self):
        from django.utils import timezone

        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        job = SautaiMealPlanJob.objects.create(tenant=self.tenant)
        with (
            patch(
                "apps.integrations.sautai_client.httpx.post",
                return_value=_mock_response(_load_fixture("generate_ok.json")),
            ),
            patch("apps.integrations.tasks._publish_sautai_poll", side_effect=RuntimeError("dropped")),
            self.assertRaisesRegex(RuntimeError, "dropped"),
        ):
            generate_sautai_meal_plan_task(str(job.id))

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)
        self.assertEqual(job.result[ASYNC_GENERATION_STATE_KEY]["poll_generation"], 1)
        SautaiMealPlanJob.objects.filter(id=job.id).update(updated_at=timezone.now() - timedelta(seconds=46))

        with patch("apps.cron.publish.publish_task") as mock_publish:
            counts = recover_sautai_generation_jobs_task()

        job.refresh_from_db()
        self.assertEqual(counts["recovered"], 1)
        self.assertEqual(counts["published"], 1)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)
        self.assertEqual(job.result[ASYNC_GENERATION_STATE_KEY]["poll_generation"], 2)
        self.assertEqual(mock_publish.call_args.kwargs["poll_generation"], 2)

    @override_settings(
        QSTASH_TOKEN="qstash-test",
        API_BASE_URL="https://nbhd.test",
        SAUTAI_M2M_BASE_URL="https://app.sautai.test",
        SAUTAI_PLATFORM_SECRET="test-secret",
    )
    def test_recovery_revokes_a_stale_generating_lease(self):
        from django.utils import timezone

        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        job = SautaiMealPlanJob.objects.create(tenant=self.tenant)
        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=_mock_response(_load_fixture("generate_ok.json")),
        ):
            call_sautai_generate_plan(job)
        SautaiMealPlanJob.objects.filter(id=job.id).update(
            status=SautaiMealPlanJobStatus.GENERATING,
            updated_at=timezone.now() - timedelta(seconds=46),
        )

        with patch("apps.cron.publish.publish_task") as mock_publish:
            counts = recover_sautai_generation_jobs_task()

        job.refresh_from_db()
        self.assertEqual(counts["recovered"], 1)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)
        self.assertEqual(job.result[ASYNC_GENERATION_STATE_KEY]["poll_generation"], 2)
        self.assertEqual(mock_publish.call_args.kwargs["poll_generation"], 2)

    @override_settings(
        QSTASH_TOKEN="qstash-test",
        API_BASE_URL="https://nbhd.test",
        SAUTAI_M2M_BASE_URL="https://app.sautai.test",
        SAUTAI_PLATFORM_SECRET="test-secret",
    )
    def test_recovery_terminalizes_a_job_past_the_strict_deadline(self):
        from django.utils import timezone

        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        job = SautaiMealPlanJob.objects.create(tenant=self.tenant)
        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=_mock_response(_load_fixture("generate_ok.json")),
        ):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        state = job.result[ASYNC_GENERATION_STATE_KEY]
        state["started_at"] = (timezone.now() - timedelta(minutes=10, seconds=1)).isoformat()
        SautaiMealPlanJob.objects.filter(id=job.id).update(
            result={ASYNC_GENERATION_STATE_KEY: state},
            updated_at=timezone.now() - timedelta(seconds=46),
        )

        with patch("apps.cron.publish.publish_task") as mock_publish:
            counts = recover_sautai_generation_jobs_task()

        job.refresh_from_db()
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("sautai_poll_timeout", job.error)
        mock_publish.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# notify_sautai_plan_ready — meditation-style completion (all channels)
# ═════════════════════════════════════════════════════════════════════


class NotifySautaiPlanReadyTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sautai Notify Test", telegram_chat_id=848686)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status"])

    def _job(self, **kwargs) -> SautaiMealPlanJob:
        defaults = {
            "status": SautaiMealPlanJobStatus.READY,
            "result": {"week_start": "2026-08-03"},
            "web_link": "https://sautai.com/meal-plans?week_start=2026-08-03",
        }
        defaults.update(kwargs)
        return SautaiMealPlanJob.objects.create(tenant=self.tenant, **defaults)

    def test_telegram_send_and_record(self):
        from .sautai_notify import notify_sautai_plan_ready

        job = self._job()
        with (
            patch("apps.router.services.send_telegram_message", return_value=True) as mock_send,
            patch("apps.router.proactive_context.record_proactive_outbound") as mock_record,
        ):
            delivered = notify_sautai_plan_ready(job)

        self.assertTrue(delivered)
        mock_send.assert_called_once()
        chat_id, text = mock_send.call_args.args[0], mock_send.call_args.args[1]
        self.assertEqual(chat_id, 848686)
        self.assertIn("2026-08-03", text)
        self.assertIn("https://sautai.com/meal-plans?week_start=2026-08-03", text)
        mock_record.assert_called_once()
        self.assertEqual(mock_record.call_args.kwargs["job_name"], "_sautai:plan_ready")

    def test_app_only_user_delivered_via_app_channel(self):
        from apps.router.models import DeviceToken

        from .sautai_notify import notify_sautai_plan_ready

        user = self.tenant.user
        user.telegram_chat_id = None
        user.save(update_fields=["telegram_chat_id"])
        DeviceToken.objects.create(user=user, tenant=self.tenant, token="a" * 64, environment="sandbox")
        job = self._job()

        with (
            patch("apps.router.services.send_telegram_message") as mock_send,
            patch("apps.router.proactive_context.record_proactive_outbound") as mock_record,
        ):
            delivered = notify_sautai_plan_ready(job)

        self.assertTrue(delivered)
        mock_send.assert_not_called()
        mock_record.assert_called_once()
        self.assertEqual(mock_record.call_args.kwargs["channel"], "app")
        self.assertEqual(mock_record.call_args.kwargs["channel_user_id"], str(user.id))

    def test_eval_sink_records_evidence_without_telegram(self):
        from .sautai_notify import notify_sautai_plan_ready

        self.tenant.is_synthetic = True
        self.tenant.is_eval_sink = True
        self.tenant.save(update_fields=["is_synthetic", "is_eval_sink"])
        job = self._job()

        with (
            patch("apps.router.services.send_telegram_message") as mock_send,
            patch("apps.router.proactive_context.record_proactive_outbound") as mock_record,
        ):
            delivered = notify_sautai_plan_ready(job)

        self.assertTrue(delivered)
        mock_send.assert_not_called()
        mock_record.assert_called_once()
        self.assertEqual(mock_record.call_args.kwargs["channel"], "eval")
        self.assertEqual(mock_record.call_args.kwargs["channel_user_id"], str(self.tenant.user_id))

    def test_no_channel_linked_does_not_send(self):
        from .sautai_notify import notify_sautai_plan_ready

        self.tenant.user.telegram_chat_id = None
        self.tenant.user.save(update_fields=["telegram_chat_id"])
        job = self._job()

        with patch("apps.router.services.send_telegram_message") as mock_send:
            delivered = notify_sautai_plan_ready(job)
        self.assertFalse(delivered)
        mock_send.assert_not_called()

    def test_inactive_tenant_does_not_send(self):
        from .sautai_notify import notify_sautai_plan_ready

        self.tenant.status = Tenant.Status.PENDING
        self.tenant.save(update_fields=["status"])
        job = self._job()

        with patch("apps.router.services.send_telegram_message") as mock_send:
            delivered = notify_sautai_plan_ready(job)
        self.assertFalse(delivered)
        mock_send.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# fetch_sautai_current_plan — parses the real /current/ contract fixtures
# ═════════════════════════════════════════════════════════════════════


@override_settings(SAUTAI_M2M_BASE_URL="https://app.sautai.test", SAUTAI_PLATFORM_SECRET="test-secret")
class FetchSautaiCurrentPlanTests(SimpleTestCase):
    def test_current_ok_returns_plan_and_link(self):
        fixture = _load_fixture("current_ok.json")
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post:
            result = fetch_sautai_current_plan(identity={"sautai_user_id": 501}, week_start_iso="2026-08-03")

        self.assertEqual(result["outcome"], "ok")
        self.assertIsInstance(result["plan"]["id"], int)
        self.assertGreater(result["plan"]["id"], 0)
        self.assertEqual(result["plan"]["week_start"], "2026-08-03")
        self.assertEqual(
            result["plan"]["days"][0]["meals"][0]["web_link"],
            "https://sautai.com/meal-plans?week_start=2026-08-03&day=Monday&meal=Dinner",
        )
        self.assertEqual(result["web_link"], "https://sautai.com/meal-plans?week_start=2026-08-03")
        self.assertIs(result["complete"], fixture["body"]["complete"])
        self.assertEqual(result["missing_days"], fixture["body"]["missing_days"])
        self.assertIs(result["funnel"]["complete"], fixture["body"]["complete"])
        self.assertEqual(result["funnel"]["missing_days"], fixture["body"]["missing_days"])

        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["X-NBHD-Platform-Secret"], "test-secret")
        self.assertEqual(kwargs["json"]["sautai_user_id"], 501)
        self.assertNotIn("user_email", kwargs["json"])
        self.assertEqual(kwargs["json"]["week_start"], "2026-08-03")
        # Fast read: a short timeout the plugin can wait on inside its 20s budget.
        self.assertLessEqual(kwargs["timeout"], 15)

    def test_current_not_found_maps_to_not_found(self):
        fixture = _load_fixture("current_not_found.json")
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            result = fetch_sautai_current_plan(identity={"sautai_user_id": 501}, week_start_iso=None)
        self.assertEqual(result["outcome"], "not_found")

    def test_current_invalid_secret_is_error(self):
        fixture = _load_fixture("error_invalid_secret.json")
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            result = fetch_sautai_current_plan(identity={"sautai_user_id": 501}, week_start_iso="2026-08-03")
        self.assertEqual(result["outcome"], "error")
        self.assertIn("401", result["detail"])

    def test_not_configured_short_circuits_without_network(self):
        fixture = _load_fixture("current_ok.json")
        with (
            override_settings(SAUTAI_M2M_BASE_URL=""),
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post,
        ):
            result = fetch_sautai_current_plan(identity={"sautai_user_id": 501}, week_start_iso=None)
        mock_post.assert_not_called()
        self.assertEqual(result["outcome"], "not_configured")

    def test_email_identity_is_rejected_without_network(self):
        with patch("apps.integrations.sautai_client.httpx.post") as mock_post:
            result = fetch_sautai_current_plan(
                identity={"user_email": "diner@example.com"},
                week_start_iso=None,
            )
        mock_post.assert_not_called()
        self.assertEqual(result["outcome"], "link_required")

    def test_remote_link_required_maps_to_link_required(self):
        link_required = {
            "status_code": 403,
            "body": {
                "status": "error",
                "code": "link_required",
                "detail": "Link this sautai account to NBHD.",
            },
        }
        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=_mock_response(link_required),
        ):
            result = fetch_sautai_current_plan(
                identity={"sautai_user_id": 501},
                week_start_iso=None,
            )
        self.assertEqual(result["outcome"], "link_required")

    def test_transport_error_maps_to_error(self):
        import httpx

        with patch("apps.integrations.sautai_client.httpx.post", side_effect=httpx.ConnectTimeout("boom")):
            result = fetch_sautai_current_plan(identity={"sautai_user_id": 501}, week_start_iso=None)
        self.assertEqual(result["outcome"], "error")
        self.assertIn("request_failed", result["detail"])


# ═════════════════════════════════════════════════════════════════════
# Phase 0.5 — link resolve, identity selection, funnel, regenerate, stale link
# ═════════════════════════════════════════════════════════════════════


@override_settings(SAUTAI_M2M_BASE_URL="https://app.sautai.test", SAUTAI_PLATFORM_SECRET="test-secret")
class ResolveSautaiLinkKeyTests(SimpleTestCase):
    """DB-free contract-fixture coverage for the server-side key exchange."""

    def test_resolve_link_ok(self):
        from .sautai_client import resolve_sautai_link_key

        fixture = _load_fixture("link_resolve_ok.json")
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post:
            result = resolve_sautai_link_key(
                "KEY123",
                nbhd_tenant_id="nbhd-tenant-fixture",
                account_email="diner@example.com",
                display_name="Dinner Friend",
            )
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["sautai_user_id"], fixture["body"]["sautai_user_id"])
        self.assertEqual(result["email"], fixture["body"]["email"])
        self.assertEqual(result["nbhd_tenant_id"], fixture["body"]["nbhd_tenant_id"])
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "link_key": "KEY123",
                "nbhd_tenant_id": "nbhd-tenant-fixture",
                "nbhd_account_email": "diner@example.com",
                "nbhd_display_name": "Dinner Friend",
            },
        )
        self.assertEqual(kwargs["headers"]["X-NBHD-Platform-Secret"], "test-secret")

    def test_resolve_link_omits_empty_identity_fields(self):
        from .sautai_client import resolve_sautai_link_key

        fixture = _load_fixture("link_resolve_ok.json")
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post:
            resolve_sautai_link_key(
                "KEY123",
                nbhd_tenant_id="nbhd-tenant-fixture",
                account_email="",
                display_name=None,
            )
        _, kwargs = mock_post.call_args
        self.assertEqual(
            kwargs["json"],
            {"link_key": "KEY123", "nbhd_tenant_id": "nbhd-tenant-fixture"},
        )

    def test_resolve_link_trims_identity_fields_to_255_characters(self):
        from .sautai_client import resolve_sautai_link_key

        fixture = _load_fixture("link_resolve_ok.json")
        account_email = "e" * 256
        display_name = "n" * 300
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post:
            resolve_sautai_link_key(
                "KEY123",
                nbhd_tenant_id="nbhd-tenant-fixture",
                account_email=account_email,
                display_name=display_name,
            )
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["nbhd_account_email"], account_email[:255])
        self.assertEqual(kwargs["json"]["nbhd_display_name"], display_name[:255])

    def test_resolve_link_invalid_key(self):
        from .sautai_client import resolve_sautai_link_key

        fixture = _load_fixture("link_resolve_invalid.json")
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            result = resolve_sautai_link_key("BAD", nbhd_tenant_id="nbhd-tenant-fixture")
        self.assertEqual(result["outcome"], "invalid_key")

    def test_resolve_link_busy_is_retryable(self):
        from .sautai_client import resolve_sautai_link_key

        busy = {"status_code": 503, "body": {"status": "error", "code": "busy", "detail": "try again"}}
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(busy)):
            result = resolve_sautai_link_key("KEY", nbhd_tenant_id="nbhd-tenant-fixture")
        self.assertEqual(result["outcome"], "retryable")
        self.assertIn("503", result["detail"])
        self.assertIn("try again", result["detail"])

    def test_resolve_link_rejects_mismatched_tenant_echo(self):
        from .sautai_client import resolve_sautai_link_key

        fixture = _load_fixture("link_resolve_ok.json")
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            result = resolve_sautai_link_key("KEY", nbhd_tenant_id="different-tenant")
        self.assertEqual(result["outcome"], "error")
        self.assertIn("echo mismatch", result["detail"])

    def test_resolve_link_not_configured_no_network(self):
        from .sautai_client import resolve_sautai_link_key

        with (
            override_settings(SAUTAI_M2M_BASE_URL=""),
            patch("apps.integrations.sautai_client.httpx.post") as mock_post,
        ):
            result = resolve_sautai_link_key("KEY", nbhd_tenant_id="nbhd-tenant-fixture")
        mock_post.assert_not_called()
        self.assertEqual(result["outcome"], "not_configured")


@override_settings(SAUTAI_M2M_BASE_URL="https://app.sautai.test", SAUTAI_PLATFORM_SECRET="test-secret")
class SautaiPhase05ClientTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sautai P05", telegram_chat_id=848490)
        self.tenant.user.email = "diner@example.com"
        self.tenant.user.save(update_fields=["email"])

    def _link(self, sautai_user_id=501):
        from django.utils import timezone

        from .models import Integration

        return Integration.objects.create(
            tenant=self.tenant,
            provider=Integration.Provider.SAUTAI,
            status=Integration.Status.ACTIVE,
            sautai_user_id=sautai_user_id,
            linked_at=timezone.now(),
            provider_email="diner@example.com",
        )

    # ── sautai_identity: only a stored link is an identity ──
    def test_identity_returns_linked_id(self):
        from .sautai_client import sautai_identity

        self._link(sautai_user_id=777)
        identity, integration = sautai_identity(self.tenant)
        self.assertEqual(identity, {"sautai_user_id": 777})
        self.assertIsNotNone(integration)

    def test_identity_does_not_fall_back_to_email(self):
        from .sautai_client import sautai_identity

        identity, _integration = sautai_identity(self.tenant)
        self.assertEqual(identity, {})

    def test_identity_empty_when_no_link_and_no_email(self):
        from .sautai_client import sautai_identity

        self.tenant.user.email = ""
        self.tenant.user.save(update_fields=["email"])
        identity, _integration = sautai_identity(self.tenant)
        self.assertEqual(identity, {})

    # ── generate addresses a linked account by id, captures funnel ──
    def test_generate_linked_sends_user_id_not_email(self):
        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        self._link(sautai_user_id=501)
        fixture = _load_fixture("generate_regenerated.json")
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 7, 13))
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post:
            call_sautai_generate_plan(job)
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["sautai_user_id"], 501)
        self.assertNotIn("user_email", kwargs["json"])
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.PENDING)
        self.assertEqual(job.addressed_by, SautaiMealPlanAddressedBy.LINKED_ID)
        self.assertEqual(job.sautai_user_id, 501)
        self.assertEqual(
            job.result[ASYNC_GENERATION_STATE_KEY]["regeneration"],
            fixture["body"]["regeneration"],
        )

    def test_generate_regenerate_passthrough(self):
        self._link()
        fixture = _load_fixture("generate_regenerated.json")
        job = SautaiMealPlanJob.objects.create(
            tenant=self.tenant,
            week_start=date(2026, 7, 13),
            regenerate=True,
            user_prompt="  more veg  ",
        )
        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready"),
        ):
            call_sautai_generate_plan(job)
        _, kwargs = mock_post.call_args
        self.assertIs(kwargs["json"]["regenerate"], True)
        self.assertEqual(kwargs["json"]["user_prompt"], "  more veg  ")
        self.assertNotIn("replace_slots", kwargs["json"])

    def test_generate_ack_tolerates_unknown_additive_keys(self):
        from .sautai_client import ASYNC_GENERATION_STATE_KEY

        self._link()
        fixture = _load_fixture("generate_ok_funnel.json")
        fixture["body"]["future_top_level"] = {"new": True}
        fixture["body"]["regeneration"]["future_regeneration_key"] = "accepted"
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 7, 13))
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        state = job.result[ASYNC_GENERATION_STATE_KEY]
        self.assertEqual(state["regeneration"]["future_regeneration_key"], "accepted")
        self.assertNotIn("future_top_level", state)

    # ── stale link: unknown_user clears the link and fails terminally ──
    def test_generate_unknown_user_clears_link_and_fails_terminally(self):
        integration = self._link(sautai_user_id=999)
        fixture = _load_fixture("generate_unknown_user.json")
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 7, 13))
        # Terminal (4xx) — must NOT raise (no QStash retry of a dead id).
        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("connection invitation in Fuel", job.error)
        integration.refresh_from_db()
        self.assertIsNone(integration.sautai_user_id)
        self.assertIsNone(integration.linked_at)

    def test_generate_link_required_clears_link_and_fails_with_connect_guidance(self):
        integration = self._link(sautai_user_id=501)
        link_required = {
            "status_code": 403,
            "body": {
                "status": "error",
                "code": "link_required",
                "detail": "Link this sautai account to NBHD.",
            },
        }
        job = SautaiMealPlanJob.objects.create(
            tenant=self.tenant,
            week_start=date(2026, 7, 13),
        )

        with patch(
            "apps.integrations.sautai_client.httpx.post",
            return_value=_mock_response(link_required),
        ):
            call_sautai_generate_plan(job)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.FAILED)
        self.assertIn("sautai_link_required", job.error)
        self.assertIn("connection invitation in Fuel", job.error)
        integration.refresh_from_db()
        self.assertIsNone(integration.sautai_user_id)
        self.assertIsNone(integration.linked_at)


class SautaiReadyMessageTests(TestCase):
    """Phase 0.5 funnel copy in the "meal plan ready" push."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Sautai Msg", telegram_chat_id=848491)

    def _job(self, **kwargs):
        defaults = {"week_start": date(2026, 7, 13), "result": {"week_start": "2026-07-13"}}
        defaults.update(kwargs)
        return SautaiMealPlanJob.objects.create(tenant=self.tenant, **defaults)

    def test_unclaimed_uses_claim_link_and_plan_count(self):
        from .sautai_notify import _ready_message

        job = self._job(
            web_link="https://sautai.com/plan",
            funnel={
                "account_claimed": False,
                "plan_count": 4,
                "claim_link": "https://sautai.com/claim?src=nbhd",
                "already_existed": False,
            },
        )
        msg = _ready_message(job)
        self.assertIn("powered by sautai", msg)
        self.assertIn("4 plans", msg)
        self.assertIn("https://sautai.com/claim?src=nbhd", msg)
        # For an unclaimed account the CTA is the claim link, not the plain web link.
        self.assertNotIn("https://sautai.com/plan", msg)

    def test_claimed_uses_web_link(self):
        from .sautai_notify import _ready_message

        job = self._job(
            web_link="https://sautai.com/plan",
            funnel={"account_claimed": True, "plan_count": 4, "claim_link": "", "already_existed": False},
        )
        msg = _ready_message(job)
        self.assertIn("powered by sautai", msg)
        self.assertIn("https://sautai.com/plan", msg)

    def test_already_existed_with_guidance_explains_fill_only_semantics(self):
        from .sautai_notify import _ready_message

        job = self._job(
            web_link="https://sautai.com/plan",
            user_prompt="high protein",
            regenerate=False,
            funnel={
                "account_claimed": True,
                "already_existed": True,
                "async_contract_confirmed": True,
            },
        )
        msg = _ready_message(job)
        self.assertIn("regeneration only fills gaps", msg.lower())
        self.assertIn("occupied meals untouched", msg.lower())

    def test_legacy_already_existed_guidance_keeps_original_regenerate_nudge(self):
        from .sautai_notify import _ready_message

        job = self._job(
            web_link="https://sautai.com/plan",
            user_prompt="high protein",
            regenerate=False,
            funnel={"account_claimed": True, "already_existed": True},
        )
        msg = _ready_message(job)
        self.assertIn(
            "This was your existing plan for the week — ask me to regenerate it if you want your latest notes applied.",
            msg,
        )
        self.assertNotIn("fills gaps", msg.lower())

    def test_regenerated_plan_has_no_stale_nudge(self):
        from .sautai_notify import _ready_message

        job = self._job(
            web_link="https://sautai.com/plan",
            user_prompt="high protein",
            regenerate=True,
            funnel={"account_claimed": True, "already_existed": True},
        )
        msg = _ready_message(job)
        self.assertNotIn("existing plan", msg.lower())

    def test_missing_days_warns_that_some_days_could_not_be_filled(self):
        from .sautai_notify import _ready_message

        job = self._job(
            web_link="https://sautai.com/plan",
            funnel={
                "account_claimed": True,
                "complete": False,
                "missing_days": ["2026-07-15", "2026-07-16"],
            },
        )
        msg = _ready_message(job)
        self.assertIn("some days could not be filled", msg.lower())
        self.assertNotIn("is ready —", msg.lower())

    def test_completed_with_failures_surfaces_exact_failed_slot_count(self):
        from .sautai_notify import _ready_message

        job = self._job(
            web_link="https://sautai.com/plan",
            funnel={
                "account_claimed": True,
                "generation_status": "completed_with_failures",
                "failed_slot_count": 2,
                "failed_slots": [
                    {"day": "Tuesday", "meal_type": "Lunch"},
                    {"day": "Friday", "meal_type": "Dinner"},
                ],
            },
        )
        msg = _ready_message(job)
        self.assertIn("2 meal slots could not be filled", msg.lower())
