"""Fuel module tests — services, models, consumer views, runtime views."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest import TestCase as UnitTestCase

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.services import create_tenant

from .models import BodyWeightLog, Workout
from .services import est_1rm

# ═════════════════════════════════════════════════════════════════════
# 1. Service Tests (pure math, no DB)
# ═════════════════════════════════════════════════════════════════════


class Est1RMTests(UnitTestCase):
    """Test the Epley 1RM estimation formula."""

    def test_single_rep_returns_weight(self):
        self.assertEqual(est_1rm(100, 1), 100.0)

    def test_standard_calculation(self):
        # 100kg x 5 reps → 100 * (1 + 5/30) = 116.7
        self.assertAlmostEqual(est_1rm(100, 5), 116.7, places=1)

    def test_ten_reps(self):
        # 80kg x 10 reps → 80 * (1 + 10/30) = 106.7
        self.assertAlmostEqual(est_1rm(80, 10), 106.7, places=1)

    def test_zero_weight(self):
        self.assertEqual(est_1rm(0, 8), 0.0)

    def test_zero_reps(self):
        self.assertEqual(est_1rm(100, 0), 0.0)

    def test_negative_reps(self):
        self.assertEqual(est_1rm(100, -1), 0.0)


# ═════════════════════════════════════════════════════════════════════
# 2. Model Tests
# ═════════════════════════════════════════════════════════════════════


class WorkoutModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Fuel Test", telegram_chat_id=800001)

    def test_create_strength_workout(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push — Chest & Shoulders",
            duration_minutes=60,
            rpe=7,
            detail_json={
                "exercises": [
                    {"name": "Bench Press", "sets": [{"reps": 8, "weight": 72.5}]},
                ]
            },
        )
        self.assertEqual(str(w.date), "2026-04-21")
        self.assertEqual(w.category, "strength")
        self.assertEqual(w.status, "done")

    def test_create_cardio_workout(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="cardio",
            activity="Zone 2 run",
            duration_minutes=42,
            detail_json={"distance_km": 7.2, "pace": "5:50", "avg_hr": 142},
        )
        self.assertEqual(w.detail_json["distance_km"], 7.2)

    def test_planned_status(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 25),
            status="planned",
            category="strength",
            activity="Leg Day",
        )
        self.assertEqual(w.status, "planned")

    def test_ordering(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 19),
            category="cardio",
            activity="Run",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        workouts = list(Workout.objects.filter(tenant=self.tenant))
        self.assertEqual(workouts[0].date, date(2026, 4, 21))
        self.assertEqual(workouts[1].date, date(2026, 4, 19))


class BodyWeightLogModelTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Weight Test", telegram_chat_id=800002)

    def test_create_entry(self):
        entry = BodyWeightLog.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            weight_kg=Decimal("82.50"),
        )
        self.assertEqual(entry.weight_kg, Decimal("82.50"))

    def test_unique_together_tenant_date(self):
        BodyWeightLog.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            weight_kg=Decimal("82.50"),
        )
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            BodyWeightLog.objects.create(
                tenant=self.tenant,
                date=date(2026, 4, 21),
                weight_kg=Decimal("83.00"),
            )


# ═════════════════════════════════════════════════════════════════════
# 3. Consumer View Tests
# ═════════════════════════════════════════════════════════════════════


class ConsumerFuelViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Consumer Test", telegram_chat_id=800003)
        self.user = self.tenant.user
        self.client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_settings_toggle(self):
        self.assertFalse(self.tenant.fuel_enabled)
        resp = self.client.patch("/api/v1/fuel/settings/", {"fuel_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["fuel_enabled"])
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.fuel_enabled)

    def test_create_workout(self):
        resp = self.client.post(
            "/api/v1/fuel/workouts/",
            {
                "date": "2026-04-21",
                "category": "strength",
                "activity": "Push Day",
                "duration_minutes": 60,
                "rpe": 7,
                "detail_json": {"exercises": []},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["activity"], "Push Day")

    def test_list_workouts(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="cardio",
            activity="Run",
        )
        resp = self.client.get("/api/v1/fuel/workouts/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_list_workouts_filter_category(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 20),
            category="cardio",
            activity="Run",
        )
        resp = self.client.get("/api/v1/fuel/workouts/?category=strength")
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(resp.data[0]["category"], "strength")

    def test_workout_detail_get(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.get(f"/api/v1/fuel/workouts/{w.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["activity"], "Push")

    def test_workout_detail_patch(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.patch(f"/api/v1/fuel/workouts/{w.id}/", {"rpe": 8}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["rpe"], 8)

    def test_workout_detail_delete(self):
        w = Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.delete(f"/api/v1/fuel/workouts/{w.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)

    def test_calendar_view(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="mobility",
            activity="Flow",
        )
        resp = self.client.get("/api/v1/fuel/calendar/?year=2026&month=4")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 1)  # one date entry
        self.assertEqual(len(resp.data[0]["workouts"]), 2)

    def test_tenant_isolation(self):
        other = create_tenant(display_name="Other", telegram_chat_id=800099)
        Workout.objects.create(
            tenant=other,
            date=date(2026, 4, 21),
            category="strength",
            activity="Other Push",
        )
        resp = self.client.get("/api/v1/fuel/workouts/")
        self.assertEqual(len(resp.data), 0)

    def test_body_weight_create(self):
        resp = self.client.post(
            "/api/v1/fuel/body-weight/",
            {"date": "2026-04-21", "weight_kg": "82.5"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["weight_kg"], "82.50")

    def test_body_weight_upsert(self):
        self.client.post(
            "/api/v1/fuel/body-weight/",
            {"date": "2026-04-21", "weight_kg": "82.5"},
            format="json",
        )
        resp = self.client.post(
            "/api/v1/fuel/body-weight/",
            {"date": "2026-04-21", "weight_kg": "83.0"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BodyWeightLog.objects.filter(tenant=self.tenant).count(), 1)

    def test_body_weight_list(self):
        BodyWeightLog.objects.create(tenant=self.tenant, date=date(2026, 4, 21), weight_kg=Decimal("82.50"))
        BodyWeightLog.objects.create(tenant=self.tenant, date=date(2026, 4, 20), weight_kg=Decimal("82.30"))
        resp = self.client.get("/api/v1/fuel/body-weight/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data), 2)

    def test_progress_strength(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 19),
            category="strength",
            activity="Push",
            status="done",
            detail_json={"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 70}]}]},
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
            status="done",
            detail_json={"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
        )
        resp = self.client.get("/api/v1/fuel/progress/?category=strength")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Bench", resp.data["progress"])
        self.assertEqual(len(resp.data["progress"]["Bench"]), 2)


# ═════════════════════════════════════════════════════════════════════
# 4. Runtime View Tests
# ═════════════════════════════════════════════════════════════════════


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeFuelViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Runtime Test", telegram_chat_id=800010)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_log_workout(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {
                "date": "2026-04-21",
                "category": "strength",
                "activity": "Push Day",
                "duration_minutes": 60,
                "rpe": 7,
                "detail_json": {"exercises": [{"name": "Bench", "sets": [{"reps": 8, "weight": 75}]}]},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["activity"], "Push Day")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 1)

    def test_log_workout_invalid_category_defaults(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {"date": "2026-04-21", "category": "nonsense", "activity": "Whatever"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["category"], "other")

    def test_auth_required(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {"date": "2026-04-21", "category": "strength", "activity": "Test"},
            format="json",
        )
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {"date": "2026-04-21", "category": "strength", "activity": "Test"},
            format="json",
            HTTP_X_NBHD_INTERNAL_KEY="wrong-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )
        self.assertEqual(resp.status_code, 401)

    def test_summary(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 4, 21),
            category="strength",
            activity="Push",
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data["recent_workouts"]), 1)

    def test_log_body_weight(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/body-weight/",
            {"date": "2026-04-21", "weight_kg": "82.5"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIn("82.5", resp.data["weight_kg"])

    def test_tenant_isolation_runtime(self):
        other = create_tenant(display_name="Other", telegram_chat_id=800099)
        Workout.objects.create(
            tenant=other,
            date=date(2026, 4, 21),
            category="strength",
            activity="Other",
        )
        resp = self.client.get(
            f"/api/v1/fuel/runtime/{self.tenant.id}/summary/",
            **self.headers,
        )
        self.assertEqual(len(resp.data["recent_workouts"]), 0)
