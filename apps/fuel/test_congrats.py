"""Tests for the workout-completion congratulations trigger + its four dedup layers.

Covers the JWT completion paths that SHOULD fire, the re-toggle / cooldown / recency
guards that suppress, the structural exclusion of the runtime + HealthKit paths, and
the fail-soft contract (a hook error never fails the workout save).

``NBHD_DISABLE_BACKGROUND_THREADS`` forces the off-request-path scheduling to run
synchronously so ``create_typed_cron`` (mocked — no real gateway push) is observable
inline.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone as dj_timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.common.llm_contracts import today_in_tenant_tz
from apps.cron.gateway_client import GatewayError
from apps.cron.models import CronJob, CronJobSource, CronPattern
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import Workout

_SCHEDULE_TARGET = "apps.cron.services.create_typed_cron"


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class WorkoutCongratsTriggerTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Congrats Test", telegram_chat_id=800900)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        self.today = today_in_tenant_tz(self.tenant)

    # ── helpers ──────────────────────────────────────────────────────────
    def _make_planned(self, **kw) -> Workout:
        defaults = dict(
            tenant=self.tenant,
            date=self.today,
            category="strength",
            activity="Push Day",
            status="planned",
        )
        defaults.update(kw)
        return Workout.objects.create(**defaults)

    def _detail_url(self, workout) -> str:
        return f"/api/v1/fuel/workouts/{workout.id}/"

    def _complete_url(self, workout) -> str:
        return f"/api/v1/fuel/workouts/{workout.id}/complete/"

    # ── the fire path ────────────────────────────────────────────────────
    @patch(_SCHEDULE_TARGET)
    def test_planned_to_done_patch_schedules_one_congrats(self, mock_create):
        w = self._make_planned()
        resp = self.client.patch(self._detail_url(w), {"status": "done"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)

        self.assertEqual(mock_create.call_count, 1)
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["pattern"], CronPattern.WORKOUT_CONGRATS)
        self.assertEqual(kwargs["source"], CronJobSource.SYSTEM)
        self.assertEqual(kwargs["schedule"]["kind"], "at")
        self.assertEqual(kwargs["name"], f"_congrats-{w.id}")
        # Repo invariant: a name that could ever feed a QStash dedup id must not
        # contain ':' or whitespace.
        self.assertNotIn(":", kwargs["name"])
        self.assertEqual(kwargs["typed_payload"]["activity"], "Push Day")

        # Durable stamp set atomically.
        w.refresh_from_db()
        self.assertIsNotNone(w.congratulated_at)

    @patch(_SCHEDULE_TARGET)
    def test_post_created_done_today_fires(self, mock_create):
        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            {
                "date": self.today.isoformat(),
                "category": "cardio",
                "activity": "Morning Run",
                "duration_minutes": 30,
                "status": "done",  # explicit — how both real clients log a finished workout
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(mock_create.call_count, 1)

    @patch(_SCHEDULE_TARGET)
    def test_post_omitting_status_does_not_fire(self, mock_create):
        # Workout.status defaults to DONE, but a POST that OMITS status is a non-UI
        # caller (script/backfill) — it must not auto-congratulate.
        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            {"date": self.today.isoformat(), "category": "cardio", "activity": "Run", "duration_minutes": 30},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        mock_create.assert_not_called()

    @patch(_SCHEDULE_TARGET)
    def test_pr_summary_included_in_payload(self, mock_create):
        # detect_prs (view) creates a PR for this strength session; the congrats
        # payload should surface a one-line PR summary.
        w = self._make_planned(
            category="strength",
            activity="Bench Day",
            detail_json={"exercises": [{"name": "Bench Press", "sets": [{"weight": 100, "reps": 5}]}]},
        )
        resp = self.client.patch(self._detail_url(w), {"status": "done"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(mock_create.call_count, 1)
        payload = mock_create.call_args.kwargs["typed_payload"]
        self.assertIn("pr_summary", payload)
        self.assertEqual(payload["pr_summary"], "New PR: Bench Press — est. 1RM 116.7 kg (from 100 kg × 5)")

    # ── layer 1: durable per-workout stamp ───────────────────────────────
    @patch(_SCHEDULE_TARGET)
    def test_retoggle_done_planned_done_does_not_refire(self, mock_create):
        w = self._make_planned()
        url = self._detail_url(w)
        self.assertEqual(self.client.patch(url, {"status": "done"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(url, {"status": "planned"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(url, {"status": "done"}, format="json").status_code, 200)
        self.assertEqual(mock_create.call_count, 1)

    @patch(_SCHEDULE_TARGET)
    def test_complete_endpoint_respects_stamp(self, mock_create):
        w = self._make_planned()
        url = self._complete_url(w)
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 200)
        self.assertEqual(self.client.post(url, {}, format="json").status_code, 200)
        self.assertEqual(mock_create.call_count, 1)

    # ── layer 2: per-tenant cooldown ─────────────────────────────────────
    @patch(_SCHEDULE_TARGET)
    def test_second_completion_within_cooldown_suppressed(self, mock_create):
        a = self._make_planned(activity="Session A")
        b = self._make_planned(activity="Session B")
        self.assertEqual(self.client.patch(self._detail_url(a), {"status": "done"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(self._detail_url(b), {"status": "done"}, format="json").status_code, 200)
        # A fired; B fell inside the cooldown window → suppressed.
        self.assertEqual(mock_create.call_count, 1)
        b.refresh_from_db()
        self.assertIsNone(b.congratulated_at)

    # ── layer 3: recency gate ────────────────────────────────────────────
    @patch(_SCHEDULE_TARGET)
    def test_post_created_done_last_week_does_not_fire(self, mock_create):
        # Explicit status=done so recency (not the status gate) is what suppresses.
        old_date = (self.today - timedelta(days=8)).isoformat()
        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            {"date": old_date, "category": "cardio", "activity": "Old Run", "duration_minutes": 30, "status": "done"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        mock_create.assert_not_called()

    # ── structural exclusion: non-JWT completion paths never congratulate ─
    @patch(_SCHEDULE_TARGET)
    def test_runtime_completion_never_fires(self, mock_create):
        seed_internal_key(self.tenant, key="test-internal-key")
        w = self._make_planned()
        runtime_client = APIClient()
        headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        resp = runtime_client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{w.id}/complete/",
            {"notes": "done by assistant"},
            format="json",
            **headers,
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        mock_create.assert_not_called()
        w.refresh_from_db()
        self.assertEqual(w.status, "done")
        self.assertIsNone(w.congratulated_at)

    @patch(_SCHEDULE_TARGET)
    def test_healthkit_ingest_never_fires(self, mock_create):
        from apps.fuel.healthkit import ingest_healthkit_payload

        payload = {
            "workouts": [
                {
                    "external_id": "hk-congrats-1",
                    "started_at": dj_timezone.now().isoformat(),
                    "duration_minutes": 30,
                    "category": "cardio",
                    "activity": "Evening Run",
                }
            ]
        }
        result = ingest_healthkit_payload(self.tenant, payload)
        self.assertEqual(result["summary"]["created"], 1, result)
        mock_create.assert_not_called()
        w = Workout.objects.get(tenant=self.tenant, external_id="hk-congrats-1")
        self.assertEqual(w.status, "done")  # a genuine, recent completion...
        self.assertIsNone(w.congratulated_at)  # ...but the model path never congratulates

    # ── hibernation: don't wake a container just to congratulate ─────────
    @patch(_SCHEDULE_TARGET)
    def test_hibernated_tenant_skips_without_stamp(self, mock_create):
        self.tenant.hibernated_at = dj_timezone.now()
        self.tenant.save(update_fields=["hibernated_at"])
        w = self._make_planned()
        resp = self.client.patch(self._detail_url(w), {"status": "done"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        # No scheduling attempt, and — crucially — no stamp consumed, so the tenant's
        # next completion while awake can still congratulate.
        mock_create.assert_not_called()
        w.refresh_from_db()
        self.assertIsNone(w.congratulated_at)

    @patch("apps.cron.services._push_at_cron_immediately", side_effect=GatewayError("unavailable", unavailable=True))
    def test_push_failure_rolls_back_stamp_and_orphan_row(self, _mock):
        # Tenant looked awake at hook time but the gateway push fails (raced into
        # hibernation). create_typed_cron commits the row before the push, so the rollback
        # must clear BOTH the durable stamp and the orphan CronJob row.
        w = self._make_planned()
        resp = self.client.patch(self._detail_url(w), {"status": "done"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        w.refresh_from_db()
        self.assertIsNone(w.congratulated_at)  # stamp rolled back → a later completion retries
        self.assertFalse(CronJob.objects.filter(tenant=self.tenant, name=f"_congrats-{w.id}").exists())

    def test_gateway_success_but_bookkeeping_failure_keeps_stamp(self):
        # The gateway ACCEPTS the cron.add (message is live) but the post-push
        # gateway_job_id save wedges on an idle connection. That is POST-delivery, so the
        # claim must be KEPT — clearing it would drop a live message and let a re-toggle
        # schedule a duplicate. (_push_at_cron_immediately swallows the bookkeeping blip,
        # so create_typed_cron returns normally and no rollback fires.)
        from django.db import OperationalError

        real_save = CronJob.save

        def _flaky_save(cron_self, *args, **kwargs):
            if kwargs.get("update_fields") == ["gateway_job_id"]:
                raise OperationalError("idle connection wedge")
            return real_save(cron_self, *args, **kwargs)

        w = self._make_planned()
        url = self._detail_url(w)
        name = f"_congrats-{w.id}"
        gw_ok = {"details": {"id": "gw-x"}}
        with (
            patch("apps.cron.gateway_client.invoke_gateway_tool", return_value=gw_ok) as mock_gw,
            patch.object(CronJob, "save", autospec=True, side_effect=_flaky_save),
        ):
            resp = self.client.patch(url, {"status": "done"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        mock_gw.assert_called_once()  # the gateway accepted the job (cron.add)
        w.refresh_from_db()
        self.assertIsNotNone(w.congratulated_at)  # claim KEPT — message is live
        self.assertTrue(CronJob.objects.filter(tenant=self.tenant, name=name).exists())

        # Re-toggle done→planned→done must NOT re-push (layer-1 stamp still blocks).
        with patch("apps.cron.gateway_client.invoke_gateway_tool", return_value=gw_ok) as mock_gw2:
            self.client.patch(url, {"status": "planned"}, format="json")
            self.client.patch(url, {"status": "done"}, format="json")
        mock_gw2.assert_not_called()
        self.assertEqual(CronJob.objects.filter(tenant=self.tenant, name=name).count(), 1)

    # ── fail-soft: a hook error must not fail the workout save ───────────
    @patch("apps.fuel.congrats._maybe_congratulate_workout", side_effect=RuntimeError("boom"))
    def test_exception_in_hook_does_not_fail_save(self, _mock):
        w = self._make_planned()
        resp = self.client.patch(self._detail_url(w), {"status": "done"}, format="json")
        self.assertEqual(resp.status_code, 200)
        w.refresh_from_db()
        self.assertEqual(w.status, "done")  # the save landed despite the hook blowing up
