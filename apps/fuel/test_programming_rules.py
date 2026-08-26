"""Materialization proof for the concrete accessory-rotation rules example."""

from __future__ import annotations

from datetime import date

from django.test import TestCase

from apps.tenants.services import create_tenant

from .models import Workout, WorkoutPlan
from .runtime_views import _author_plan_expansion_inputs, _expand_plan_workouts


def _exercise(name, reps, weight):
    return {"name": name, "sets": [{"type": "weighted_reps", "reps": reps, "weight": weight}]}


class AccessoryRotationRulesExampleTests(TestCase):
    def test_complete_week_two_override_keeps_main_lifts_and_rotates_accessories(self):
        tenant = create_tenant(display_name="Rules Rotation", telegram_chat_id=811303)
        start = date(2026, 6, 1)
        base_day = {
            "activity": "Upper Strength",
            "category": "strength",
            "detail_json": {
                "exercises": [
                    _exercise("Bench Press", 5, 80),
                    _exercise("Overhead Press", 6, 40),
                    _exercise("Lateral Raise", 12, 8),
                    _exercise("Tricep Pushdown", 12, 20),
                ]
            },
        }
        week_two_day = {
            "activity": "Upper Strength",
            "category": "strength",
            "detail_json": {
                "exercises": [
                    _exercise("Bench Press", 5, 82.5),
                    _exercise("Overhead Press", 6, 42.5),
                    _exercise("Incline Dumbbell Curl", 10, 12),
                    _exercise("Face Pull", 15, 18),
                ]
            },
        }
        schedule = {"0": base_day}
        overrides = {"2": {"0": week_two_day}}
        plan = WorkoutPlan.objects.create(
            tenant=tenant,
            name="Rules Example",
            start_date=start,
            weeks=3,
            days_per_week=1,
            schedule_json=schedule,
            week_overrides=overrides,
        )
        authored = _author_plan_expansion_inputs(
            tenant,
            schedule,
            3,
            week_overrides=overrides,
            writer="runtime",
        )
        _expand_plan_workouts(
            plan,
            tenant,
            schedule,
            start,
            3,
            week_overrides=overrides,
            authored_workouts=authored,
        )

        weeks = list(Workout.objects.filter(plan=plan).order_by("date"))
        self.assertEqual(len(weeks), 3)
        week_zero_names = [item["name"] for item in weeks[0].detail_json["exercises"]]
        week_one_names = [item["name"] for item in weeks[1].detail_json["exercises"]]
        week_two_names = [item["name"] for item in weeks[2].detail_json["exercises"]]
        self.assertEqual(week_zero_names[:2], ["Bench Press", "Overhead Press"])
        self.assertEqual(week_two_names[:2], ["Bench Press", "Overhead Press"])
        self.assertEqual(week_one_names, week_zero_names)
        self.assertEqual(week_zero_names[2:], ["Lateral Raise", "Tricep Pushdown"])
        self.assertEqual(week_two_names[2:], ["Incline Dumbbell Curl", "Face Pull"])
