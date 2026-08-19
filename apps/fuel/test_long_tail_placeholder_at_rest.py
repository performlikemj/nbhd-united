"""P3 W3b real writer/read seams for Fuel's registered long-tail stores."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.common.llm_contracts import today_in_tenant_tz
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .models import FuelProfile, SleepLog, Workout, WorkoutPlan, WorkoutTemplate
from .serializers import (
    FuelProfileSerializer,
    SleepLogSerializer,
    WorkoutPlanSerializer,
    WorkoutSerializer,
    WorkoutTemplateSerializer,
)


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class FuelLongTailPlaceholderTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="W3b Fuel", telegram_chat_id=880311)
        self.tenant.fuel_enabled = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["fuel_enabled", "pii_entity_map"])
        FuelProfile.objects.create(tenant=self.tenant)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        self.runtime = APIClient()
        self.runtime_headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _enable_placeholder_writes(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

    @staticmethod
    def _next_monday():
        today = timezone.localdate()
        return today + timedelta(days=(7 - today.weekday()) % 7 or 7)

    def _plan_payload(self, name="Alice Plan"):
        return {
            "name": name,
            "start_date": self._next_monday().isoformat(),
            "weeks": 1,
            "days_per_week": 1,
            "objective": "Train with Alice",
            "notes": "Alice set the cadence",
            "schedule_json": {
                "0": {
                    "category": "cardio",
                    "activity": "Run with Alice",
                    "duration_minutes": 30,
                    "detail_json": {"coach_note": "Pace beside Alice"},
                }
            },
        }

    def test_owner_flag_off_real_workout_and_sleep_seams_preserve_bytes(self):
        workout_payload = {
            "date": today_in_tenant_tz(self.tenant).isoformat(),
            "category": "other",
            "activity": "Walk with Alice",
            "notes": "Exact Alice note",
            "detail_json": {"route": "Alice loop"},
        }
        created = self.client.post("/api/v1/fuel/workouts/", workout_payload, format="json")
        slept = self.client.post(
            "/api/v1/fuel/sleep/",
            {
                "date": today_in_tenant_tz(self.tenant).isoformat(),
                "duration_hours": "7.5",
                "notes": "Dreamed about Alice",
            },
            format="json",
        )

        self.assertEqual(created.status_code, 201, created.data)
        workout = Workout.objects.get(id=created.data["id"])
        self.assertEqual(workout.activity, workout_payload["activity"])
        self.assertEqual(workout.notes, workout_payload["notes"])
        self.assertEqual(workout.detail_json, workout_payload["detail_json"])
        self.assertEqual(workout.pii_receipts["activity"], {"state": "bypass", "writer": "owner"})
        self.assertEqual(created.data["activity"], workout_payload["activity"])
        self.assertEqual(slept.status_code, 201)
        sleep = SleepLog.objects.get(tenant=self.tenant)
        self.assertEqual(sleep.notes, "Dreamed about Alice")
        self.assertEqual(sleep.pii_receipts["notes"], {"state": "bypass", "writer": "owner"})

    def test_runtime_plan_flag_off_preserves_nested_plan_bytes(self):
        payload = {
            **self._plan_payload("Exact Alice Runtime Plan"),
            "concurrent": True,
            "week_overrides": {
                "0": {
                    "0": {
                        "category": "cardio",
                        "activity": "Recover with Alice",
                        "duration_minutes": 20,
                        "detail_json": {"cue": "Listen to Alice"},
                    }
                }
            },
        }
        with patch("apps.fuel.runtime_views._manage_fuel_cron"):
            response = self.runtime.post(
                f"/api/v1/fuel/runtime/{self.tenant.id}/plans/",
                payload,
                format="json",
                **self.runtime_headers,
            )

        self.assertEqual(response.status_code, 201, response.data)
        plan = WorkoutPlan.objects.get(id=response.data["id"])
        self.assertEqual(plan.schedule_json, payload["schedule_json"])
        self.assertEqual(plan.week_overrides, payload["week_overrides"])
        self.assertEqual(plan.pii_receipts["schedule_json"], {"state": "bypass", "writer": "runtime"})
        self.assertEqual(plan.pii_receipts["week_overrides"], {"state": "bypass", "writer": "runtime"})

    def test_owner_crud_and_dashboard_projections_rehydrate_and_emit_receipts(self):
        self._enable_placeholder_writes()
        today = today_in_tenant_tz(self.tenant)
        with _checked_detection():
            profile = self.client.patch(
                "/api/v1/fuel/profile/",
                {
                    "additional_context": "Recover with Alice",
                    "limitations": ["Avoid Alice's old drill"],
                    "pii_receipts": {"additional_context": {"state": "forged"}},
                },
                format="json",
            )
            template = self.client.post(
                "/api/v1/fuel/templates/",
                {
                    "name": "Alice intervals",
                    "category": "cardio",
                    "activity": "Intervals",
                    "duration_minutes": 30,
                    "detail_json": {"cue": "Follow Alice"},
                    "pii_receipts": {"name": {"state": "forged"}},
                },
                format="json",
            )
            workout = self.client.post(
                "/api/v1/fuel/workouts/",
                {
                    "date": today.isoformat(),
                    "category": "other",
                    "activity": "Walk with Alice",
                    "notes": "Started beside Alice",
                    "notes_thread": [
                        {
                            "at": "2026-08-08T12:00:00Z",
                            "who": "user",
                            "text": "Ask Alice about the route",
                        }
                    ],
                    "detail_json": {"route": "Alice loop"},
                },
                format="json",
            )
            skipped = self.client.post(
                f"/api/v1/fuel/workouts/{workout.data['id']}/skip/",
                {"reason": "Meeting Alice"},
                format="json",
            )
            completed = self.client.post(
                f"/api/v1/fuel/workouts/{workout.data['id']}/complete/",
                {"notes": "Finished with Alice"},
                format="json",
            )
            duplicated = self.client.post(
                f"/api/v1/fuel/workouts/{workout.data['id']}/duplicate/",
                {},
                format="json",
            )

        self.assertEqual(profile.status_code, 200, profile.data)
        stored_profile = FuelProfile.objects.get(tenant=self.tenant)
        self.assertEqual(stored_profile.additional_context, "Recover with [PERSON_1]")
        self.assertEqual(stored_profile.limitations, ["Avoid [PERSON_1]'s old drill"])
        self.assertEqual(stored_profile.pii_receipts["additional_context"]["writer"], "owner")
        self.assertEqual(profile.data["additional_context"], "Recover with Alice")
        self.assertNotEqual(profile.data["pii_receipts"]["additional_context"]["state"], "forged")

        self.assertEqual(template.status_code, 201, template.data)
        stored_template = WorkoutTemplate.objects.get(id=template.data["id"])
        self.assertEqual(stored_template.name, "[PERSON_1] intervals")
        self.assertEqual(stored_template.detail_json, {"cue": "Follow [PERSON_1]"})
        self.assertEqual(template.data["name"], "Alice intervals")

        self.assertEqual(skipped.data["skip_reason"], "Meeting Alice")
        self.assertEqual(completed.data["notes"], "Finished with Alice")
        stored_workout = Workout.objects.get(id=workout.data["id"])
        self.assertEqual(stored_workout.skip_reason, "Meeting [PERSON_1]")
        self.assertEqual(stored_workout.notes, "Finished with [PERSON_1]")
        self.assertEqual(stored_workout.notes_thread[0]["text"], "Ask [PERSON_1] about the route")
        self.assertEqual(workout.data["notes_thread"][0]["text"], "Ask Alice about the route")
        self.assertEqual(stored_workout.pii_receipts["notes"]["writer"], "owner")
        self.assertEqual(stored_workout.pii_receipts["notes_thread"]["writer"], "owner")
        self.assertEqual(
            workout.data["pii_receipts"]["notes_thread"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )
        copy = Workout.objects.get(id=duplicated.data["id"])
        self.assertEqual(set(copy.pii_receipts), {"activity", "detail_json"})
        self.assertEqual(copy.notes, "")
        self.assertEqual(copy.skip_reason, "")

        calendar = self.client.get(f"/api/v1/fuel/calendar/?year={today.year}&month={today.month}")
        overview = self.client.get(f"/api/v1/fuel/overview/?year={today.year}&month={today.month}")
        stub = next(item for day in calendar.data for item in day["workouts"] if item["id"] == str(stored_workout.id))
        self.assertEqual(stub["activity"], "Walk with Alice")
        self.assertEqual(stub["pii_receipts"]["activity"]["redactions"][0]["value"], "Alice")
        projected = next(item for item in overview.data["workouts"] if item["id"] == str(stored_workout.id))
        self.assertEqual(projected["notes"], "Finished with Alice")
        self.assertEqual(projected["pii_receipts"]["notes"]["writer"], "owner")

    def test_owner_plan_dedup_authors_derived_workouts_before_transaction(self):
        self._enable_placeholder_writes()
        payload = self._plan_payload()
        with _checked_detection():
            first = self.client.post("/api/v1/fuel/plans/", payload, format="json")
            second = self.client.post("/api/v1/fuel/plans/", payload, format="json")

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data["deduped"])
        self.assertEqual(WorkoutPlan.objects.filter(tenant=self.tenant).count(), 1)
        plan = WorkoutPlan.objects.get(tenant=self.tenant)
        child = Workout.objects.get(plan=plan)
        self.assertEqual(plan.name, "[PERSON_1] Plan")
        self.assertEqual(plan.schedule_json["0"]["activity"], "Run with [PERSON_1]")
        self.assertEqual(plan.schedule_json["0"]["detail_json"]["coach_note"], "Pace beside [PERSON_1]")
        self.assertEqual(plan.pii_receipts["schedule_json"]["writer"], "owner")
        self.assertEqual(child.activity, "Run with [PERSON_1]")
        self.assertEqual(child.detail_json["coach_note"], "Pace beside [PERSON_1]")
        self.assertEqual(child.pii_receipts["activity"]["writer"], "owner")
        self.assertEqual(first.data["name"], "Alice Plan")
        self.assertEqual(first.data["schedule_json"]["0"]["activity"], "Run with Alice")
        self.assertEqual(first.data["pii_receipts"]["schedule_json"]["redactions"][0]["value"], "Alice")

        owner_rows = self.client.get("/api/v1/fuel/workouts/")
        represented = next(item for item in owner_rows.data if item["id"] == str(child.id))
        self.assertEqual(represented["plan_name"], "Alice Plan")
        self.assertEqual(represented["pii_receipts"]["plan_name"]["redactions"][0]["value"], "Alice")

    def test_runtime_plan_dedup_and_lifecycle_stay_placeholder_space(self):
        self._enable_placeholder_writes()
        payload = {
            **self._plan_payload("Alice Runtime Plan"),
            # Two weeks so the week-1 override below is inside the plan. This
            # fixture used to ride on a 1-week plan, which the create path now
            # rejects: an override keyed to a week the plan does not have was
            # being stored and echoed back as if it had taken effect.
            "weeks": 2,
            "concurrent": True,
            "week_overrides": {
                "1": {
                    "0": {
                        "category": "cardio",
                        "activity": "Future run with Alice",
                        "duration_minutes": 25,
                        "detail_json": {"cue": "Ask Alice"},
                    }
                }
            },
        }
        url = f"/api/v1/fuel/runtime/{self.tenant.id}/plans/"
        with (
            _checked_detection(),
            patch("apps.fuel.runtime_views._manage_fuel_cron"),
        ):
            first = self.runtime.post(url, payload, format="json", **self.runtime_headers)
            second = self.runtime.post(url, payload, format="json", **self.runtime_headers)

        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertTrue(second.data["deduped"])
        self.assertEqual(first.data["name"], "[PERSON_1] Runtime Plan")
        self.assertNotIn("pii_receipts", first.data)
        plan = WorkoutPlan.objects.get(tenant=self.tenant)
        # Week 0's session — the one the week-0 override further down retemplates.
        child = Workout.objects.filter(plan=plan).order_by("date").first()
        self.assertEqual(plan.pii_receipts["name"]["writer"], "runtime")
        self.assertEqual(plan.schedule_json["0"]["activity"], "Run with [PERSON_1]")
        self.assertEqual(plan.week_overrides["1"]["0"]["detail_json"]["cue"], "Ask [PERSON_1]")
        self.assertEqual(plan.pii_receipts["schedule_json"]["writer"], "runtime")
        self.assertEqual(plan.pii_receipts["week_overrides"]["writer"], "runtime")
        self.assertEqual(child.activity, "Run with [PERSON_1]")
        self.assertEqual(child.pii_receipts["detail_json"]["writer"], "runtime")

        with (
            _checked_detection(),
            patch("apps.fuel.runtime_views._manage_fuel_cron"),
        ):
            updated = self.runtime.patch(
                f"{url}{plan.id}/",
                {
                    "week_overrides": {
                        "0": {
                            "0": {
                                "category": "cardio",
                                "activity": "Recover with Alice",
                                "duration_minutes": 20,
                                "detail_json": {"cue": "Listen to Alice"},
                            }
                        }
                    }
                },
                format="json",
                **self.runtime_headers,
            )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertNotIn("pii_receipts", updated.data)
        plan.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(plan.week_overrides["0"]["0"]["activity"], "Recover with [PERSON_1]")
        self.assertEqual(plan.pii_receipts["week_overrides"]["writer"], "runtime")
        self.assertEqual(child.activity, "Recover with [PERSON_1]")

        with _checked_detection():
            completed = self.runtime.post(
                f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{child.id}/complete/",
                {"notes": "Checked with Alice"},
                format="json",
                **self.runtime_headers,
            )
        child.refresh_from_db()
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(child.notes, "Checked with [PERSON_1]")
        self.assertEqual(child.pii_receipts["notes"]["writer"], "runtime")
        self.assertNotIn("pii_receipts", completed.data)

    def test_healthkit_owner_ingress_authors_registered_workout_fields(self):
        self._enable_placeholder_writes()
        started = timezone.now() - timedelta(hours=1)
        payload = {
            "workouts": [
                {
                    "external_id": "w3b-healthkit-1",
                    "activity": "Run with Alice",
                    "category": "cardio",
                    "raw_type": "running",
                    "source_bundle": "com.example.health",
                    "started_at": started.isoformat(),
                    "ended_at": (started + timedelta(minutes=30)).isoformat(),
                    "duration_minutes": 30,
                    "metrics": {"distance_km": 5, "avg_hr": 145},
                }
            ]
        }
        with (
            _checked_detection(),
            patch("apps.fuel.healthkit.push_visibility_refresh"),
            patch("apps.fuel.signals._enqueue_regen"),
        ):
            response = self.client.post("/api/v1/fuel/healthkit/sync/", payload, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        workout = Workout.objects.get(tenant=self.tenant, external_id="w3b-healthkit-1")
        self.assertEqual(workout.activity, "Run with [PERSON_1]")
        self.assertEqual(workout.pii_receipts["activity"]["writer"], "owner")
        self.assertEqual(workout.pii_receipts["detail_json"]["writer"], "owner")
        owner_read = self.client.get(f"/api/v1/fuel/workouts/{workout.id}/")
        self.assertEqual(owner_read.data["activity"], "Run with Alice")
        self.assertEqual(owner_read.data["pii_receipts"]["activity"]["redactions"][0]["value"], "Alice")

    def test_owner_receipt_fields_are_read_only(self):
        self.assertTrue(FuelProfileSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(WorkoutPlanSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(WorkoutSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(WorkoutTemplateSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(SleepLogSerializer().fields["pii_receipts"].read_only)
