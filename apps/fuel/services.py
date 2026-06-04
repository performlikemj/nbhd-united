"""Fuel business logic — est1RM calculation and progress aggregation."""

from __future__ import annotations

from datetime import timedelta

from .set_contract import METRIC_HOLD_TIME, set_metric


def backfill_plan_slots(WorkoutPlanModel, PlanSlotModel, WorkoutModel) -> dict[str, int]:
    """Materialize PlanSlot rows for every plan and back-link planned workouts.

    Idempotent — re-running is a no-op for slots that already exist and for
    workouts already linked. Accepts the model classes as arguments so the
    same body works under both the live ORM (test code) and the migration
    framework's historical models (RunPython callback).

    Returns counts for migrate-log telemetry.
    """
    plan_skipped = 0
    slots_created = 0
    workouts_linked = 0
    workouts_skipped = 0

    for plan in WorkoutPlanModel.objects.iterator():
        schedule = plan.schedule_json or {}
        if not isinstance(schedule, dict) or not schedule:
            plan_skipped += 1
            continue

        template_by_weekday: dict[int, str] = {}
        valid_weekdays: list[int] = []
        for day_str, workout_def in schedule.items():
            try:
                day_int = int(day_str)
            except (TypeError, ValueError):
                continue
            if day_int < 0 or day_int > 6:
                continue
            if not isinstance(workout_def, dict):
                continue
            valid_weekdays.append(day_int)
            activity = workout_def.get("activity")
            if isinstance(activity, str):
                template_by_weekday[day_int] = activity.strip()

        weeks = max(1, min(52, int(plan.weeks or 1)))
        slot_lookup: dict[tuple[int, int], object] = {}
        for week_idx in range(weeks):
            for weekday in valid_weekdays:
                existing = PlanSlotModel.objects.filter(
                    plan=plan,
                    week_index=week_idx,
                    weekday=weekday,
                    archived_at__isnull=True,
                ).first()
                if existing is not None:
                    slot_lookup[(week_idx, weekday)] = existing
                    continue
                slot = PlanSlotModel.objects.create(
                    tenant_id=plan.tenant_id,
                    plan=plan,
                    week_index=week_idx,
                    weekday=weekday,
                )
                slot_lookup[(week_idx, weekday)] = slot
                slots_created += 1

        start_date = plan.start_date
        if start_date is None:
            continue
        plan_monday = start_date - timedelta(days=start_date.weekday())

        for w in WorkoutModel.objects.filter(plan=plan, slot__isnull=True).iterator():
            if w.date is None:
                workouts_skipped += 1
                continue
            elapsed_days = (w.date - plan_monday).days
            week_idx = elapsed_days // 7
            weekday = w.date.weekday()
            if week_idx < 0 or week_idx >= weeks:
                workouts_skipped += 1
                continue
            slot = slot_lookup.get((week_idx, weekday))
            if slot is None:
                workouts_skipped += 1
                continue
            template_activity = template_by_weekday.get(weekday)
            if template_activity is None or w.activity.strip() != template_activity:
                workouts_skipped += 1
                continue
            w.slot = slot
            w.save(update_fields=["slot"])
            workouts_linked += 1

    return {
        "plans_skipped": plan_skipped,
        "slots_created": slots_created,
        "workouts_linked": workouts_linked,
        "workouts_skipped": workouts_skipped,
    }


def _safe_num(val, default=0) -> float:
    """Coerce a value to float, returning default if not numeric."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def est_1rm(weight, reps) -> float:
    """Epley formula: estimated one-rep max from weight and reps."""
    w = _safe_num(weight)
    r = _safe_num(reps)
    if not w or r < 1:
        return 0.0
    if r == 1:
        return w
    return round(w * (1 + r / 30), 1)


def enrich_strength_detail(detail: dict) -> dict:
    """Add est_1rm to each set in a strength detail_json."""
    for exercise in detail.get("exercises", []):
        for s in exercise.get("sets", []):
            s["est_1rm"] = est_1rm(s.get("weight", 0), s.get("reps", 0))
    return detail


def aggregate_strength_progress(workouts) -> dict:
    """Build per-exercise est1RM trend data from strength workouts.

    Returns: {exercise_name: [{date, value}]} sorted oldest-first.
    """
    by_lift: dict[str, list[dict]] = {}
    for w in sorted(workouts, key=lambda w: w.date):
        for ex in (w.detail_json or {}).get("exercises", []):
            name = ex.get("name", "").strip()
            if not name:
                continue
            top = max(
                (est_1rm(s.get("weight", 0), s.get("reps", 0)) for s in ex.get("sets", [])),
                default=0,
            )
            by_lift.setdefault(name, []).append({"date": str(w.date), "value": top})
    return by_lift


def aggregate_cardio_progress(workouts) -> dict:
    """Build pace and distance trends from cardio workouts."""
    pace_points = []
    dist_points = []
    total_km = 0.0

    for w in sorted(workouts, key=lambda w: w.date):
        d = w.detail_json or {}
        if d.get("pace"):
            parts = str(d["pace"]).split(":")
            try:
                secs = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0]) * 60
                pace_points.append({"date": str(w.date), "value": secs})
            except (ValueError, IndexError):
                pass
        if d.get("distance_km"):
            km = float(d["distance_km"])
            total_km += km
            dist_points.append({"date": str(w.date), "value": km})

    return {"pace": pace_points, "distance": dist_points, "total_km": round(total_km, 1)}


def aggregate_hiit_progress(workouts) -> dict:
    """Build peak HR trend and totals from HIIT workouts."""
    hr_points = []
    total_minutes = 0

    for w in sorted(workouts, key=lambda w: w.date):
        d = w.detail_json or {}
        if d.get("peak_hr"):
            hr_points.append({"date": str(w.date), "value": d["peak_hr"]})
        total_minutes += w.duration_minutes or 0

    return {"peak_hr": hr_points, "session_count": len(workouts), "total_minutes": total_minutes}


def aggregate_calisthenics_progress(workouts) -> dict:
    """Build per-skill trend data from calisthenics workouts.

    Returns: {skill_name: {points: [{date, value}], is_hold: bool}}
    """
    by_skill: dict[str, dict] = {}

    for w in sorted(workouts, key=lambda w: w.date):
        for sk in (w.detail_json or {}).get("skills", []):
            name = sk.get("name", "").strip()
            if not name:
                continue
            sets = sk.get("sets", [])
            # Shape-agnostic: explicit `type` (Phase 2+), else field
            # presence — identical to the historical hold_s null-sniff.
            is_hold = bool(sets) and set_metric(sets[0]) == METRIC_HOLD_TIME
            top = max(
                (s.get("hold_s", 0) if is_hold else s.get("reps", 0) for s in sets),
                default=0,
            )
            entry = by_skill.setdefault(name, {"points": [], "is_hold": is_hold})
            entry["points"].append({"date": str(w.date), "value": top})

    return by_skill


def detect_prs(tenant, workout) -> list[dict]:
    """Detect personal records from a workout. Returns list of new PRs created."""
    from .models import PersonalRecord

    if workout.status != "done":
        return []

    new_prs = []

    if workout.category == "strength":
        for ex in (workout.detail_json or {}).get("exercises", []):
            name = ex.get("name", "").strip()
            if not name:
                continue
            top_1rm = max(
                (est_1rm(s.get("weight", 0), s.get("reps", 0)) for s in ex.get("sets", [])),
                default=0,
            )
            if top_1rm <= 0:
                continue

            from decimal import Decimal

            top_decimal = Decimal(str(top_1rm))

            # Check previous best
            prev = (
                PersonalRecord.objects.filter(tenant=tenant, exercise_name=name, metric="est_1rm")
                .order_by("-value")
                .first()
            )
            prev_value = prev.value if prev else None

            if prev_value is None or top_decimal > prev_value:
                pr = PersonalRecord.objects.create(
                    tenant=tenant,
                    workout=workout,
                    exercise_name=name,
                    category="strength",
                    value=top_decimal,
                    previous_value=prev_value,
                    metric="est_1rm",
                    date=workout.date,
                )
                new_prs.append(
                    {"exercise": name, "value": float(pr.value), "previous": float(prev_value) if prev_value else None}
                )

    return new_prs
