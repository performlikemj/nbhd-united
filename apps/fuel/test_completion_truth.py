"""Every workout completion writer stamps time; reversals clear evidence."""

from datetime import UTC, date, datetime
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.cron.models import CronJob, CronPattern
from apps.cron.patterns.workout_congrats import completion_still_valid
from apps.fuel import congrats
from apps.fuel import tests as fuel_tests
from apps.fuel.models import Workout
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True, NBHD_INTERNAL_API_KEY="test-internal-key")
class WorkoutCompletionTruthTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Workout truth", telegram_chat_id=801990)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.tenant.user).access_token}")
        self.runtime = APIClient()
        self.runtime.credentials(
            HTTP_X_NBHD_INTERNAL_KEY="test-internal-key", HTTP_X_NBHD_TENANT_ID=str(self.tenant.id)
        )

    def workout(self, **kwargs):
        return Workout.objects.create(
            tenant=self.tenant, date=date.today(), activity="Mobility", category="mobility", status="planned", **kwargs
        )

    def test_jwt_and_runtime_complete_patch_and_revert(self):
        for client, base in ((self.client, "/api/v1/fuel"), (self.runtime, f"/api/v1/fuel/runtime/{self.tenant.id}")):
            for complete in (True, False):
                with self.subTest(base=base, complete=complete), patch("apps.fuel.congrats.maybe_congratulate_workout"):
                    workout = self.workout()
                    url = f"{base}/workouts/{workout.id}/"
                    response = (
                        client.post(url + "complete/", {}, format="json")
                        if complete
                        else client.patch(url, {"status": "done"}, format="json")
                    )
                    self.assertEqual(response.status_code, 200, response.data)
                    workout.refresh_from_db()
                    self.assertIsNotNone(workout.completed_at)
                    stamp = workout.completed_at
                    client.post(url + "complete/", {}, format="json")
                    workout.refresh_from_db()
                    self.assertEqual(workout.completed_at, stamp)
                    for state in ("planned", "rest", "in_progress", "skipped", "rescheduled"):
                        client.patch(url, {"status": state}, format="json")
                        workout.refresh_from_db()
                        self.assertEqual(workout.status, state)
                        self.assertIsNone(workout.completed_at)
                        client.post(url + "complete/", {}, format="json")

    def test_log_done_defaults_and_planned_remains_unstamped(self):
        for client, url in (
            (self.client, "/api/v1/fuel/workouts/"),
            (self.runtime, f"/api/v1/fuel/runtime/{self.tenant.id}/log/"),
        ):
            for state in (None, "done", "planned"):
                with self.subTest(url=url, state=state), patch("apps.fuel.congrats.maybe_congratulate_workout"):
                    data = {"date": str(date.today()), "category": "other", "activity": "Easy session"}
                    if state is not None:
                        data["status"] = state
                    result = client.post(url, data, format="json")
                    self.assertEqual(result.status_code, 201, result.data)
                    workout = Workout.objects.get(id=result.data["id"])
                    self.assertEqual(workout.completed_at is not None, state != "planned")

    def test_legacy_done_row_not_backfilled_by_unrelated_patch(self):
        workout = self.workout()
        Workout.objects.filter(pk=workout.pk).update(status="done")
        response = self.client.patch(
            f"/api/v1/fuel/workouts/{workout.id}/", {"rpe": 4, "completed_at": "2000-01-01T00:00:00Z"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        workout.refresh_from_db()
        self.assertIsNone(workout.completed_at)

    def test_skip_clears_completion(self):
        for client, base in ((self.client, "/api/v1/fuel"), (self.runtime, f"/api/v1/fuel/runtime/{self.tenant.id}")):
            workout = self.workout()
            Workout.objects.filter(pk=workout.pk).update(status="done", completed_at=timezone.now())
            result = client.post(f"{base}/workouts/{workout.id}/skip/", {}, format="json")
            self.assertEqual(result.status_code, 200, result.data)
            workout.refresh_from_db()
            self.assertIsNone(workout.completed_at)

    def test_summary_and_audit_label_planned_and_done(self):
        planned = self.workout()
        done = self.workout()
        Workout.objects.filter(pk=done.pk).update(status="done", completed_at=timezone.now())
        base = f"/api/v1/fuel/runtime/{self.tenant.id}"
        summary = self.runtime.get(base + "/summary/").data
        self.assertEqual(summary["planned_workouts"][0]["status"], "planned")
        self.assertEqual(summary["recent_workouts"][0]["status"], "done")
        self.assertIsNotNone(summary["recent_workouts"][0]["completed_at"])
        audit = self.runtime.get(base + "/audit/").data
        rows = {row["id"]: row for row in audit["today_plan"]["workouts"]}
        self.assertEqual(rows[str(planned.id)]["status"], "planned")
        self.assertIsNotNone(rows[str(done.id)]["completed_at"])

    def test_delivery_rechecks_before_sending(self):
        self.tenant.status = "active"
        self.tenant.save(update_fields=["status"])
        workout = self.workout()
        with patch("apps.router.cron_delivery.CronDeliveryView._send_via_telegram") as send:
            response = self.runtime.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/",
                {"message": "Nice workout!"},
                format="json",
                HTTP_X_NBHD_JOB_NAME=congrats._congrats_cron_name(str(workout.id)),
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["skipped"], "workout_not_done")
        send.assert_not_called()

    def assert_cron_delivered(self, *, gateway_job_id, job_name=""):
        self.tenant.status = "active"
        self.tenant.save(update_fields=["status"])
        with patch(
            "apps.router.cron_delivery.CronDeliveryView._send_via_telegram",
            return_value=Response({"status": "sent", "channel": "telegram"}),
        ) as send:
            response = self.runtime.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/",
                {"message": "Your daily update."},
                format="json",
                HTTP_X_NBHD_CRON_JOB_ID=gateway_job_id,
                **({"HTTP_X_NBHD_JOB_NAME": job_name} if job_name else {}),
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"status": "sent", "channel": "telegram"})
        send.assert_called_once()

    def test_delivery_unknown_cron_id_without_job_name_is_delivered(self):
        CronJob.objects.create(tenant=self.tenant, name="daily briefing", pattern=CronPattern.DAILY_BRIEFING)
        self.assert_cron_delivered(gateway_job_id="not-yet-synced")

    def test_delivery_non_congrats_cron_is_delivered(self):
        CronJob.objects.create(
            tenant=self.tenant,
            name="daily briefing",
            gateway_job_id="runtime-briefing-id",
            pattern=CronPattern.DAILY_BRIEFING,
        )
        self.assert_cron_delivered(gateway_job_id="runtime-briefing-id")

    def test_delivery_congrats_done_workout_is_delivered(self):
        workout = self.workout()
        Workout.objects.filter(pk=workout.pk).update(status="done", completed_at=timezone.now())
        CronJob.objects.create(
            tenant=self.tenant,
            name="stored congrats",
            gateway_job_id="runtime-cron-id",
            pattern=CronPattern.WORKOUT_CONGRATS,
            typed_payload={"workout_id": str(workout.id), "activity": "Mobility"},
        )
        self.assert_cron_delivered(gateway_job_id="runtime-cron-id")

    def test_delivery_unknown_cron_id_still_checks_legacy_congrats_name(self):
        self.tenant.status = "active"
        self.tenant.save(update_fields=["status"])
        workout = self.workout()
        name = congrats._congrats_cron_name(str(workout.id))
        with patch("apps.router.cron_delivery.CronDeliveryView._send_via_telegram") as send:
            response = self.runtime.post(
                f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/",
                {"message": "Nice workout!"},
                format="json",
                HTTP_X_NBHD_CRON_JOB_ID="not-yet-synced",
                HTTP_X_NBHD_JOB_NAME=name,
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"delivered": False, "skipped": "workout_not_done"})
        send.assert_not_called()
        Workout.objects.filter(pk=workout.pk).update(status="done", completed_at=timezone.now())
        self.assert_cron_delivered(gateway_job_id="not-yet-synced", job_name=name)

    def test_delivery_without_job_name_rechecks_payload_workout(self):
        self.tenant.status = "active"
        self.tenant.save(update_fields=["status"])
        workout = self.workout()
        Workout.objects.filter(pk=workout.pk).update(status="done", completed_at=timezone.now())
        CronJob.objects.create(
            tenant=self.tenant,
            name="stored congrats",
            gateway_job_id="runtime-cron-id",
            pattern=CronPattern.WORKOUT_CONGRATS,
            typed_payload={"workout_id": str(workout.id), "activity": "Mobility"},
        )
        self.assertTrue(completion_still_valid(self.tenant, "", gateway_job_id="runtime-cron-id"))
        Workout.objects.filter(pk=workout.pk).update(status="planned", completed_at=None)
        for headers in ({}, {"HTTP_X_NBHD_JOB_NAME": "misleading-name"}):
            with (
                self.subTest(headers=headers),
                patch("apps.router.cron_delivery.CronDeliveryView._send_via_telegram") as send,
            ):
                response = self.runtime.post(
                    f"/api/v1/integrations/runtime/{self.tenant.id}/send-to-user/",
                    {"message": "Nice workout!"},
                    format="json",
                    HTTP_X_NBHD_CRON_JOB_ID="runtime-cron-id",
                    **headers,
                )
            self.assertEqual(response.status_code, 200, response.data)
            self.assertFalse(response.data["delivered"])
            self.assertEqual(response.data["skipped"], "workout_not_done")
            send.assert_not_called()
        other = create_tenant(display_name="Other", telegram_chat_id=801991)
        self.assertTrue(completion_still_valid(other, "", gateway_job_id="runtime-cron-id"))
        workout.delete()
        self.assertFalse(completion_still_valid(self.tenant, "", gateway_job_id="runtime-cron-id"))

    def test_reverted_or_deleted_workout_suppresses_congrats(self):
        workout = self.workout()
        name = congrats._congrats_cron_name(str(workout.id))
        self.assertFalse(completion_still_valid(self.tenant, name))
        with patch("apps.fuel.congrats._schedule_congrats_cron") as schedule:
            congrats._dispatch_schedule(self.tenant, workout_id=str(workout.id), payload={"activity": "Mobility"})
        schedule.assert_not_called()
        Workout.objects.filter(pk=workout.pk).update(status="done")
        self.assertTrue(completion_still_valid(self.tenant, name))
        workout.delete()
        self.assertFalse(completion_still_valid(self.tenant, name))
        self.assertTrue(completion_still_valid(self.tenant, "morning briefing"))


class HealthKitCompletionTruthTests(TestCase):
    def test_create_match_and_adopt_use_sample_end(self):
        # Reuse the established JWT/HealthKit fixture without inheriting its tests.
        fixture = fuel_tests.HealthKitSyncTests()
        fixture.setUp()
        expected = datetime(2026, 6, 9, 23, 12, tzinfo=UTC)
        for mode in ("create", "match", "adopt"):
            with self.subTest(mode=mode):
                Workout.objects.filter(tenant=fixture.tenant).delete()
                existing = None
                if mode != "create":
                    existing = Workout.objects.create(
                        tenant=fixture.tenant,
                        date=date(2026, 6, 10),
                        activity="Outdoor Run",
                        category="cardio",
                        status="planned" if mode == "match" else "done",
                        duration_minutes=42,
                        completed_at=None if mode == "match" else timezone.now(),
                    )
                result = fixture._post({"workouts": [fixture._workout_item(external_id=f"hk-{mode}")]})
                self.assertEqual(result.status_code, 200, result.data)
                workout = Workout.objects.get(tenant=fixture.tenant)
                self.assertEqual(workout.completed_at, expected)
                if existing:
                    self.assertEqual(workout.id, existing.id)
                fixture._post({"workouts": [fixture._workout_item(external_id=f"hk-{mode}")]})
                workout.refresh_from_db()
                self.assertEqual(workout.completed_at, expected)
