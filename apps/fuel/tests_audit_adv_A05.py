"""Adversarial-audit regression tests for cluster A05.

FA-0584: PATCH on the runtime workout-plan endpoint accepts a
``week_overrides`` block (per-week progression/deload + rest-day drops) and
stores it, but the reconcile/materialize path that regenerates the calendar
ignored it — so a PATCH that asked to deload week 2 or rest a day returned
200 but left the Workout calendar unchanged. This file pins the
materialization parity between the POST/create path and the PATCH path.
"""

from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.fuel.models import Workout, WorkoutPlan
from apps.tenants.services import create_tenant


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimePlanPatchWeekOverridesMaterializationTests(TestCase):
    """PATCH-with-overrides must materialize the same calendar the POST path
    does, not merely store + round-trip the overrides (FA-0584)."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Patch Override", telegram_chat_id=800584)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        cron_patch = patch("apps.fuel.runtime_views._manage_fuel_cron", return_value=None)
        cron_patch.start()
        self.addCleanup(cron_patch.stop)

    def _create_url(self):
        return f"/api/v1/fuel/runtime/{self.tenant.id}/plans/"

    def _patch_url(self, plan_id):
        return f"/api/v1/fuel/runtime/{self.tenant.id}/plans/{plan_id}/"

    def _create_base_plan(self):
        # Mon (Heavy Squats) + Wed (Heavy Bench) over 2 weeks, no overrides yet.
        resp = self.client.post(
            self._create_url(),
            data={
                "name": "Periodized PATCH",
                "weeks": 2,
                "days_per_week": 2,
                "start_date": "2026-06-15",
                "schedule_json": {
                    "0": {"category": "strength", "activity": "Heavy Squats", "target_rpe": 9},
                    "2": {"category": "strength", "activity": "Heavy Bench", "target_rpe": 9},
                },
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        return WorkoutPlan.objects.get(id=resp.data["id"])

    @patch("apps.fuel.runtime_views.today_in_tenant_tz", return_value=date(2026, 6, 15))
    def test_patch_week_overrides_materializes_deload_and_rest(self, _mock_today):
        plan = self._create_base_plan()

        # Baseline: 4 workouts (Wk1 Mon/Wed, Wk2 Mon/Wed), all "Heavy".
        baseline = list(Workout.objects.filter(plan=plan).order_by("date"))
        self.assertEqual(
            [str(w.date) for w in baseline],
            ["2026-06-15", "2026-06-17", "2026-06-22", "2026-06-24"],
        )

        # PATCH week 2 (index 1): deload Monday + drop the Wednesday (rest).
        resp = self.client.patch(
            self._patch_url(plan.id),
            data={
                "week_overrides": {
                    "1": {
                        "0": {"category": "strength", "activity": "Deload Squats", "target_rpe": 5},
                        "2": None,
                    }
                }
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.data)

        dates = [str(w.date) for w in Workout.objects.filter(plan=plan).order_by("date")]
        # Wk1 unchanged (Mon+Wed); Wk2 Mon kept (deloaded), Wk2 Wed rested out.
        self.assertEqual(dates, ["2026-06-15", "2026-06-17", "2026-06-22"])

        # Week-2 Monday must now be the deload prescription, not "Heavy Squats".
        deload = Workout.objects.get(plan=plan, date=date(2026, 6, 22))
        self.assertEqual(deload.activity, "Deload Squats")
        self.assertEqual(deload.rpe, 5)

        # Week-1 stays heavy.
        wk1_mon = Workout.objects.get(plan=plan, date=date(2026, 6, 15))
        self.assertEqual(wk1_mon.activity, "Heavy Squats")
        self.assertEqual(wk1_mon.rpe, 9)

        # Overrides persist + round-trip on the serialized plan.
        self.assertEqual(resp.data.get("week_overrides", {}).get("1", {}).get("2"), None)
        self.assertIn("1", resp.data.get("week_overrides", {}))
