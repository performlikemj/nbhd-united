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
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .models import Integration, SautaiMealPlanAddressedBy, SautaiMealPlanJob, SautaiMealPlanJobStatus
from .sautai_client import call_sautai_generate_plan, fetch_sautai_current_plan
from .tasks import generate_sautai_meal_plan_task

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "m2m"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES_DIR / name).read_text())


def _mock_response(fixture: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = fixture["status_code"]
    resp.json.return_value = fixture["body"]
    return resp


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

    def test_generate_ok_marks_job_ready_with_plan_and_link(self):
        fixture = _load_fixture("generate_ok.json")
        job = self._job(week_start=date(2026, 8, 3))

        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post:
            call_sautai_generate_plan(job)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.READY)
        # auto-increment on sautai's side — drifts on every fixture regen,
        # assert shape not value (see week_start/web_link below for the
        # deterministically-pinned fields, which stay exact).
        self.assertIsInstance(job.result["id"], int)
        self.assertGreater(job.result["id"], 0)
        self.assertEqual(job.result["week_start"], "2026-08-03")
        self.assertEqual(len(job.result["days"]), 7)
        self.assertEqual(job.web_link, "https://sautai.com/meal-plans?week_start=2026-08-03")
        self.assertEqual(job.error, "")
        self.assertEqual(job.addressed_by, SautaiMealPlanAddressedBy.LINKED_ID)
        self.assertEqual(job.sautai_user_id, 501)

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["X-NBHD-Platform-Secret"], "test-secret")
        self.assertEqual(kwargs["json"]["sautai_user_id"], 501)
        self.assertNotIn("user_email", kwargs["json"])
        self.assertEqual(kwargs["json"]["week_start"], "2026-08-03")
        self.assertGreaterEqual(kwargs["timeout"], 120)

    def test_generate_user_created_marks_job_ready(self):
        fixture = _load_fixture("generate_user_created.json")
        job = self._job()

        with patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)):
            call_sautai_generate_plan(job)

        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.READY)
        self.assertTrue(job.result.get("id"))

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
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready"),
        ):
            generate_sautai_meal_plan_task(str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.READY)


# ═════════════════════════════════════════════════════════════════════
# generate_sautai_meal_plan_task — claim guard (idempotent on redelivery)
# ═════════════════════════════════════════════════════════════════════


class GenerateSautaiMealPlanTaskTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Sautai Task Test", telegram_chat_id=848585)

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
        self._link(sautai_user_id=501)
        fixture = _load_fixture("generate_regenerated.json")
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 7, 13))
        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready"),
        ):
            call_sautai_generate_plan(job)
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["sautai_user_id"], 501)
        self.assertNotIn("user_email", kwargs["json"])
        job.refresh_from_db()
        self.assertEqual(job.status, SautaiMealPlanJobStatus.READY)
        self.assertEqual(job.addressed_by, SautaiMealPlanAddressedBy.LINKED_ID)
        self.assertEqual(job.sautai_user_id, 501)
        self.assertIs(job.funnel.get("account_claimed"), fixture["body"]["account_claimed"])
        self.assertEqual(job.funnel.get("plan_count"), fixture["body"]["plan_count"])

    def test_generate_regenerate_passthrough(self):
        self._link()
        fixture = _load_fixture("generate_regenerated.json")
        job = SautaiMealPlanJob.objects.create(
            tenant=self.tenant, week_start=date(2026, 7, 13), regenerate=True, user_prompt="more veg"
        )
        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)) as mock_post,
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready"),
        ):
            call_sautai_generate_plan(job)
        _, kwargs = mock_post.call_args
        self.assertIs(kwargs["json"]["regenerate"], True)

    def test_generate_captures_funnel_fields(self):
        self._link()
        fixture = _load_fixture("generate_ok_funnel.json")
        job = SautaiMealPlanJob.objects.create(tenant=self.tenant, week_start=date(2026, 7, 13))
        with (
            patch("apps.integrations.sautai_client.httpx.post", return_value=_mock_response(fixture)),
            patch("apps.integrations.sautai_notify.notify_sautai_plan_ready"),
        ):
            call_sautai_generate_plan(job)
        job.refresh_from_db()
        self.assertIs(job.funnel["account_claimed"], False)
        self.assertEqual(job.funnel["plan_count"], 1)
        self.assertIn("/claim?", job.funnel["claim_link"])
        self.assertIn("token=", job.funnel["claim_link"])
        self.assertIs(job.funnel["already_existed"], False)
        self.assertIs(job.funnel["complete"], fixture["body"]["complete"])
        self.assertEqual(job.funnel["missing_days"], fixture["body"]["missing_days"])

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

    def test_already_existed_with_guidance_adds_regenerate_nudge(self):
        from .sautai_notify import _ready_message

        job = self._job(
            web_link="https://sautai.com/plan",
            user_prompt="high protein",
            regenerate=False,
            funnel={"account_claimed": True, "already_existed": True},
        )
        msg = _ready_message(job)
        self.assertIn("regenerate", msg.lower())

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
