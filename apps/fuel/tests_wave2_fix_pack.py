"""Wave 2 fuel fix pack — the legal-but-wrong writes.

Every case here is a call that used to SUCCEED while storing something other
than what the user said, or a stored value that made a read path fail forever.
The shared shape of the bug is that nothing complained at the time:

  1. Empty prescription — a strength/calisthenics log with no exercises got a
     201 and became an invisible workout (proven on the canary 2026-08-19:
     three set-validation 400s, then a 201 carrying ``skills: []``).
  2. Cardio detail — ``distance_km: "5 miles"`` stored fine and then raised on
     every load of that tenant's cardio Progress view, permanently.
  3. preferred_days — ``["1", "3"]`` was filtered to ``[]``, wiping the user's
     stated training days behind a 200.
  4. week_overrides — a deload keyed to week 9 of a 4-week plan was accepted
     and echoed back, while no such week existed.
  5. Workout status — anything unrecognised became "done", so a MISSED session
     was recorded as a completed one.
  6. rpe — clamped to 1-10 without saying so, leaving the assistant reporting
     a number that is not on the row.
"""

from datetime import date, timedelta

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.fuel.models import FuelProfile, Workout, WorkoutPlan
from apps.platform_logs.models import ToolContractEvent
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


def _strength_detail():
    return {"exercises": [{"name": "Bench Press", "sets": [{"type": "weighted_reps", "reps": 5, "weight": 60}]}]}


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class _RuntimeFuelCase(TestCase):
    chat_id = 810000

    def setUp(self):
        self.tenant = create_tenant(display_name="Wave2 Fuel", telegram_chat_id=self.chat_id)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def base(self, suffix):
        return f"/api/v1/fuel/runtime/{self.tenant.id}/{suffix}"

    def log(self, payload):
        return self.client.post(self.base("log/"), payload, format="json", **self.headers)

    def reasons(self):
        return list(ToolContractEvent.objects.values_list("reason_code", flat=True))


class EmptyPrescriptionGuardTests(_RuntimeFuelCase):
    """Fix 1 — the live bug. A CREATE-style path must not mint an empty workout."""

    chat_id = 810001

    def test_empty_skills_container_rejected(self):
        resp = self.log(
            {
                "category": "calisthenics",
                "activity": "Skills Session",
                "detail_json": {"skills": []},
            }
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["error"], "validation_failed")
        self.assertEqual(resp.data["details"][0]["type"], "missing_prescription")
        self.assertIn("example", resp.data["details"][0])
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)
        self.assertIn("empty_prescription", self.reasons())

    def test_absent_detail_json_rejected_on_strength(self):
        # The create path is strict in the same way the PLAN create path is:
        # silence is not "leave it alone" here, there is nothing to leave.
        resp = self.log({"category": "strength", "activity": "Push Day"})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["details"][0]["type"], "missing_prescription")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)

    def test_real_prescription_still_accepted(self):
        resp = self.log({"category": "strength", "activity": "Push Day", "detail_json": _strength_detail()})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 1)

    def test_skipped_strength_needs_no_prescription(self):
        # "I missed leg day" — nothing was performed, so there are no sets to
        # demand. Requiring them here would re-close the gap fix 5 just opened.
        resp = self.log({"category": "strength", "activity": "Leg Day", "status": "skipped"})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Workout.objects.get(tenant=self.tenant).status, "skipped")

    def test_planned_cardio_without_prescription_rejected(self):
        resp = self.log({"status": "planned", "category": "cardio", "activity": "Morning Run"})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["details"][0]["type"], "missing_prescription")
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)

    def test_done_cardio_without_prescription_allowed(self):
        # Completed logs describe what happened rather than a prescription the
        # user should open and follow, so they remain outside the planned guard.
        resp = self.log({"category": "cardio", "activity": "Morning Run"})
        self.assertEqual(resp.status_code, 201, resp.data)


class CardioDetailWriteTests(_RuntimeFuelCase):
    """Fix 2, write side — numbers must be numbers before they reach the store."""

    chat_id = 810002

    def test_distance_with_units_rejected(self):
        resp = self.log(
            {"category": "cardio", "activity": "Long Run", "detail_json": {"distance_km": "5 miles"}},
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["details"][0]["type"], "cardio_detail_invalid")
        self.assertEqual(resp.data["details"][0]["loc"], ["detail_json", "distance_km"])
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)
        self.assertIn("cardio_detail_invalid", self.reasons())

    def test_numeric_string_coerced_not_rejected(self):
        resp = self.log(
            {"category": "cardio", "activity": "Run", "detail_json": {"distance_km": "5.2", "avg_hr": "150"}},
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        w = Workout.objects.get(tenant=self.tenant)
        self.assertEqual(w.detail_json["distance_km"], 5.2)
        self.assertEqual(w.detail_json["avg_hr"], 150)

    def test_hiit_rounds_with_words_rejected(self):
        resp = self.log(
            {"category": "hiit", "activity": "Intervals", "detail_json": {"rounds": "eight"}},
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["details"][0]["loc"], ["detail_json", "rounds"])

    def test_bool_is_not_a_number(self):
        resp = self.log(
            {"category": "cardio", "activity": "Run", "detail_json": {"distance_km": True}},
        )
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_patch_path_guarded_too(self):
        w = Workout.objects.create(tenant=self.tenant, date=date(2026, 5, 1), category="cardio", activity="Run")
        resp = self.client.patch(
            self.base(f"workouts/{w.id}/"),
            {"detail_json": {"distance_km": "about ten k"}},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        w.refresh_from_db()
        self.assertEqual(w.detail_json, {})


class CardioProgressLegacyRowTests(TestCase):
    """Fix 2, read side — one bad legacy row must not take the view down.

    Console endpoint (JWT), because that is where the 500 was user-visible.
    """

    def setUp(self):
        from rest_framework_simplejwt.tokens import RefreshToken

        self.tenant = create_tenant(display_name="Wave2 Cardio", telegram_chat_id=810003)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_progress_survives_unparseable_distance(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 1),
            category="cardio",
            activity="Good Run",
            status="done",
            detail_json={"distance_km": 5, "pace": "5:30"},
        )
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 2),
            category="cardio",
            activity="Legacy Run",
            status="done",
            detail_json={"distance_km": "5 miles"},
        )

        resp = self.client.get("/api/v1/fuel/progress/?category=cardio")
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", resp))
        progress = resp.data["progress"]
        self.assertEqual(progress["total_km"], 5.0)
        self.assertEqual(len(progress["distance"]), 1)
        self.assertEqual(progress["skipped_rows"], 1)
        self.assertIn(
            "cardio_legacy_row_skipped",
            list(ToolContractEvent.objects.values_list("reason_code", flat=True)),
        )

    def test_clean_rows_report_no_skips(self):
        Workout.objects.create(
            tenant=self.tenant,
            date=date(2026, 5, 1),
            category="cardio",
            activity="Run",
            status="done",
            detail_json={"distance_km": 5},
        )
        resp = self.client.get("/api/v1/fuel/progress/?category=cardio")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("skipped_rows", resp.data["progress"])


class PreferredDaysTests(_RuntimeFuelCase):
    """Fix 3 — the silent wipe."""

    chat_id = 810004

    def patch_profile(self, payload):
        return self.client.patch(self.base("profile/"), payload, format="json", **self.headers)

    def test_int_strings_stored(self):
        resp = self.patch_profile({"preferred_days": ["1", "3"]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["preferred_days"], [1, 3])
        self.assertEqual(FuelProfile.objects.get(tenant=self.tenant).preferred_days, [1, 3])

    def test_weekday_names_and_abbreviations_stored(self):
        resp = self.patch_profile({"preferred_days": ["monday", "wed"]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["preferred_days"], [0, 2])

    def test_plain_ints_still_work(self):
        resp = self.patch_profile({"preferred_days": [0, 2, 4]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["preferred_days"], [0, 2, 4])

    def test_booleans_rejected(self):
        # isinstance(True, int) is True — the old filter stored a bool as day 1.
        resp = self.patch_profile({"preferred_days": [True]})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["error"], "validation_failed")

    def test_garbage_rejects_instead_of_reducing(self):
        FuelProfile.objects.update_or_create(tenant=self.tenant, defaults={"preferred_days": [0, 2]})
        resp = self.patch_profile({"preferred_days": ["monday", "someday"]})
        self.assertEqual(resp.status_code, 400, resp.data)
        # The stored preference is untouched — never partially overwritten.
        self.assertEqual(FuelProfile.objects.get(tenant=self.tenant).preferred_days, [0, 2])
        self.assertIn("preferred_days_invalid", self.reasons())

    def test_non_list_rejected(self):
        resp = self.patch_profile({"preferred_days": "monday"})
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_explicit_empty_list_clears(self):
        FuelProfile.objects.update_or_create(tenant=self.tenant, defaults={"preferred_days": [0, 2]})
        resp = self.patch_profile({"preferred_days": []})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(FuelProfile.objects.get(tenant=self.tenant).preferred_days, [])

    def test_duplicate_spellings_collapse(self):
        resp = self.patch_profile({"preferred_days": ["wednesday", "2", 2]})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["preferred_days"], [2])

    def test_coercion_is_reported(self):
        self.patch_profile({"preferred_days": ["1", "3"]})
        self.assertIn("preferred_days_coerced", self.reasons())


class WeekOverrideBoundsTests(_RuntimeFuelCase):
    """Fix 4a/4b — an override for a week the plan does not have."""

    chat_id = 810005

    def _plan_payload(self, **extra):
        payload = {
            "name": "Bounds Plan",
            "weeks": 4,
            "days_per_week": 1,
            "start_date": "2026-06-01",
            "schedule_json": {
                "monday": {"category": "strength", "activity": "Full Body", "detail_json": _strength_detail()}
            },
        }
        payload.update(extra)
        return payload

    def test_out_of_range_week_rejected_on_create(self):
        resp = self.client.post(
            self.base("plans/"),
            self._plan_payload(week_overrides={"9": {"monday": None}}),
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["error"], "invalid_week_overrides")
        self.assertIn("0-3", resp.data["detail"])
        self.assertEqual(WorkoutPlan.objects.filter(tenant=self.tenant).count(), 0)
        self.assertIn("week_override_out_of_range", self.reasons())

    def test_last_legal_week_accepted(self):
        resp = self.client.post(
            self.base("plans/"),
            self._plan_payload(week_overrides={"3": {"monday": None}}),
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["week_overrides"], {"3": {"0": None}})

    def test_update_plan_accepts_week_overrides(self):
        created = self.client.post(self.base("plans/"), self._plan_payload(), format="json", **self.headers)
        self.assertEqual(created.status_code, 201, created.data)
        plan_id = created.data["id"]

        resp = self.client.patch(
            self.base(f"plans/{plan_id}/"),
            {"week_overrides": {"2": {"monday": None}}},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["week_overrides"], {"2": {"0": None}})
        plan = WorkoutPlan.objects.get(id=plan_id)
        self.assertEqual(plan.week_overrides, {"2": {"0": None}})

    def test_out_of_range_week_rejected_on_update(self):
        created = self.client.post(self.base("plans/"), self._plan_payload(), format="json", **self.headers)
        plan_id = created.data["id"]
        resp = self.client.patch(
            self.base(f"plans/{plan_id}/"),
            {"week_overrides": {"9": {"monday": None}}},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(WorkoutPlan.objects.get(id=plan_id).week_overrides, {})

    def test_extending_weeks_widens_the_bound_in_one_call(self):
        created = self.client.post(self.base("plans/"), self._plan_payload(), format="json", **self.headers)
        plan_id = created.data["id"]
        resp = self.client.patch(
            self.base(f"plans/{plan_id}/"),
            {
                "weeks": 8,
                "week_overrides": {"6": {"monday": None}},
                "repeat_policy": "intentional",
                "repeat_reason": "Bounds test keeps the legacy recipe intentionally",
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200, resp.data)


class WeekOverrideAnchorTests(TestCase):
    """Fix 4c — overrides are keyed to ABSOLUTE plan weeks, not loop offsets."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Wave2 Anchor", telegram_chat_id=810006)

    def test_midplan_expansion_resolves_overrides_by_absolute_week(self):
        from apps.fuel.runtime_views import _author_plan_expansion_inputs, _expand_plan_workouts

        start = date(2026, 6, 1)  # a Monday
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Anchor Plan",
            start_date=start,
            weeks=4,
            days_per_week=1,
            schedule_json={"0": {"category": "cardio", "activity": "Base Run"}},
            week_overrides={"2": {"0": {"category": "cardio", "activity": "Deload Run"}}},
        )

        # Regenerate from week 2 onward — the offset restarts at 0 while the
        # plan is in its third week. Keyed by offset, override "2" would have
        # landed on absolute week 4 (which does not exist) and week 2 would
        # have been rebuilt from the base template.
        regen_start = start + timedelta(weeks=2)
        authored = _author_plan_expansion_inputs(
            self.tenant,
            plan.schedule_json,
            2,
            week_overrides=plan.week_overrides,
            writer="runtime",
            week_index_base=2,
        )
        _expand_plan_workouts(
            plan,
            self.tenant,
            plan.schedule_json,
            regen_start,
            2,
            week_overrides=plan.week_overrides,
            authored_workouts=authored,
        )

        activities = {str(w.date): w.activity for w in Workout.objects.filter(plan=plan).order_by("date")}
        self.assertEqual(activities[str(regen_start)], "Deload Run")
        self.assertEqual(activities[str(regen_start + timedelta(weeks=1))], "Base Run")

    def test_create_path_anchor_unchanged(self):
        from apps.fuel.runtime_views import _author_plan_expansion_inputs, _expand_plan_workouts

        start = date(2026, 6, 1)
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant,
            name="Create Anchor",
            start_date=start,
            weeks=3,
            days_per_week=1,
            schedule_json={"0": {"category": "cardio", "activity": "Base Run"}},
            week_overrides={"0": {"0": {"category": "cardio", "activity": "Week One Run"}}},
        )
        authored = _author_plan_expansion_inputs(
            self.tenant, plan.schedule_json, 3, week_overrides=plan.week_overrides, writer="runtime"
        )
        _expand_plan_workouts(
            plan,
            self.tenant,
            plan.schedule_json,
            start,
            3,
            week_overrides=plan.week_overrides,
            authored_workouts=authored,
        )
        first = Workout.objects.filter(plan=plan).order_by("date").first()
        self.assertEqual(first.activity, "Week One Run")


class WorkoutStatusTests(_RuntimeFuelCase):
    """Fix 5 — no silent coercion in either direction."""

    chat_id = 810007

    def test_unknown_status_rejected_on_log(self):
        resp = self.log(
            {"category": "cardio", "activity": "Run", "status": "missed"},
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertEqual(resp.data["details"][0]["type"], "unknown_status")
        self.assertIn("skipped", resp.data["details"][0]["allowed"])
        self.assertEqual(Workout.objects.filter(tenant=self.tenant).count(), 0)
        self.assertIn("unknown_status_rejected", self.reasons())

    def test_skipped_is_expressible(self):
        resp = self.log({"category": "cardio", "activity": "Run", "status": "skipped"})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Workout.objects.get(tenant=self.tenant).status, "skipped")

    def test_absent_status_still_defaults_to_done(self):
        resp = self.log({"category": "cardio", "activity": "Run"})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["status"], "done")

    def test_unknown_status_rejected_on_patch(self):
        w = Workout.objects.create(
            tenant=self.tenant, date=date(2026, 5, 1), category="cardio", activity="Run", status="planned"
        )
        resp = self.client.patch(self.base(f"workouts/{w.id}/"), {"status": "missed"}, format="json", **self.headers)
        self.assertEqual(resp.status_code, 400, resp.data)
        w.refresh_from_db()
        self.assertEqual(w.status, "planned")


class RpeClampVisibilityTests(_RuntimeFuelCase):
    """Fix 6 — the clamp stays, the silence does not."""

    chat_id = 810008

    def test_clamped_rpe_is_echoed_and_flagged(self):
        resp = self.log({"category": "cardio", "activity": "Run", "rpe": 99})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["rpe"], 10)
        self.assertTrue(resp.data["rpe_clamped"])
        self.assertEqual(Workout.objects.get(tenant=self.tenant).rpe, 10)
        self.assertIn("rpe_clamped", self.reasons())

    def test_in_range_rpe_is_not_flagged(self):
        resp = self.log({"category": "cardio", "activity": "Run", "rpe": 7})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["rpe"], 7)
        self.assertNotIn("rpe_clamped", resp.data)
        self.assertNotIn("rpe_clamped", self.reasons())


class ScheduleTelemetryTests(_RuntimeFuelCase):
    """Fix 7 — retro-enrichment for the PR #1481 paths."""

    chat_id = 810009

    def test_name_keys_recorded_as_name_style(self):
        resp = self.client.post(
            self.base("plans/"),
            {
                "name": "Name Keys",
                "weeks": 2,
                "days_per_week": 1,
                "start_date": "2026-06-01",
                "schedule_json": {
                    "monday": {"category": "strength", "activity": "Full", "detail_json": _strength_detail()}
                },
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        event = ToolContractEvent.objects.get(reason_code="weekday_key_style")
        self.assertEqual(event.detail["weekday_key_style"], "name")
        self.assertEqual(event.outcome, "accepted")

    def test_legacy_int_keys_recorded_as_normalized(self):
        resp = self.client.post(
            self.base("plans/"),
            {
                "name": "Int Keys",
                "weeks": 2,
                "days_per_week": 1,
                "start_date": "2026-06-01",
                "schedule_json": {"0": {"category": "strength", "activity": "Full", "detail_json": _strength_detail()}},
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        event = ToolContractEvent.objects.get(reason_code="weekday_key_style")
        self.assertEqual(event.detail["weekday_key_style"], "int")
        self.assertEqual(event.outcome, "normalized")

    def test_start_today_reject_is_recorded(self):
        from apps.common.llm_contracts import today_in_tenant_tz

        today = today_in_tenant_tz(self.tenant)
        wrong_day = (today.weekday() + 1) % 7
        names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        resp = self.client.post(
            self.base("plans/"),
            {
                "name": "Starts Today",
                "weeks": 2,
                "days_per_week": 1,
                "start_date": today.isoformat(),
                "schedule_json": {
                    names[wrong_day]: {
                        "category": "strength",
                        "activity": "Full",
                        "detail_json": _strength_detail(),
                    }
                },
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        event = ToolContractEvent.objects.get(reason_code="start_today_reject")
        self.assertTrue(event.detail["start_today_reject"])

    def test_events_carry_no_free_text(self):
        # The allowlist is the guarantee; this pins that no fuel call site is
        # smuggling caller prose in under a legitimate key.
        self.log({"category": "cardio", "activity": "Totally Free Text Activity", "detail_json": {"rounds": "many"}})
        for event in ToolContractEvent.objects.all():
            for value in event.detail.values():
                if isinstance(value, str):
                    self.assertNotIn(" ", value)
