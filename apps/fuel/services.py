"""Fuel business logic — est1RM calculation and progress aggregation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import timedelta
from typing import Any

from .set_contract import METRIC_HOLD_TIME, set_metric

# --------------------------------------------------------------------------
# Plan reconciler (phase 3 of the plan-update durable fix).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotKey:
    """The natural-key half of a PlanSlot: ``(week_index, weekday)``.

    Frozen so it's hashable for set diffs.
    """

    week_index: int
    weekday: int


@dataclass
class WorkoutSpec:
    """Template-derived data for a workout the reconciler wants to create."""

    slot_key: SlotKey
    date: _date
    category: str
    activity: str
    duration_minutes: int | None
    detail_json: dict


@dataclass
class PlanReconciliation:
    """Result of :func:`reconcile_plan_state`. Describes the diff between the
    plan's current slot+workout state and the desired schedule without
    committing anything. :func:`apply_reconciliation` is what writes.
    """

    plan_id: str
    new_slot_keys: list[SlotKey] = field(default_factory=list)
    slots_to_archive: list[Any] = field(default_factory=list)
    slots_kept: list[Any] = field(default_factory=list)
    workouts_to_delete: list[Any] = field(default_factory=list)
    workouts_to_create: list[WorkoutSpec] = field(default_factory=list)
    # (workout, target_slot_key, template_patch) — existing rows whose
    # date matches a newly-created slot. They get adopted by that slot
    # AND inherit the slot's template-driven fields (matches the old
    # DELETE+INSERT "template wins" behavior without changing the uuid).
    workouts_to_adopt: list[tuple[Any, SlotKey, dict]] = field(default_factory=list)
    # (workout, template) — for slots that are kept, apply template fields
    # the assistant just changed. Workout uuids stay; only the template-
    # driven fields move.
    workouts_to_retemplate: list[tuple[Any, dict]] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return (
            not self.new_slot_keys
            and not self.slots_to_archive
            and not self.workouts_to_delete
            and not self.workouts_to_create
            and not self.workouts_to_adopt
            and not self.workouts_to_retemplate
        )


def _parse_schedule_template(schedule_json: dict | None) -> dict[int, dict]:
    """Return ``{weekday_int: template_dict}`` for valid entries only.

    Invalid weekday strings, out-of-range ints, and non-dict entries are
    dropped silently — same forgiving shape as ``_expand_plan_workouts``.
    """
    out: dict[int, dict] = {}
    for day_str, workout_def in (schedule_json or {}).items():
        try:
            day_int = int(day_str)
        except (TypeError, ValueError):
            continue
        if not (0 <= day_int <= 6):
            continue
        if not isinstance(workout_def, dict):
            continue
        out[day_int] = workout_def
    return out


def reconcile_plan_state(plan, schedule_json: dict, weeks: int, *, today: _date | None = None) -> PlanReconciliation:
    """Diff a plan's current slot/workout state against the desired schedule.

    Pure relative to the DB *in the sense that* it does NOT issue any
    writes. It does read from the ORM (active slots, future planned
    workouts) — keeping the read here means callers don't have to thread
    snapshot arguments through, and the queries are tightly scoped.

    Behavior rules:

    * Only ``(week_index, weekday)`` pairs whose computed date is on/after
      ``today`` AND on/after the plan's ``start_date`` count toward the
      diff. Past slots are out of scope — historical workouts stay put.
    * Slots in the desired schedule but missing from the active set go in
      ``new_slot_keys`` (plus a matching :class:`WorkoutSpec` in
      ``workouts_to_create``).
    * Slots in the active set whose key is missing from the desired
      schedule go in ``slots_to_archive`` (soft-archive). Future planned
      workouts on those slots are listed in ``workouts_to_delete``;
      done / in-progress / past workouts are deliberately left alone.
    * Slots whose key matches in both lists go in ``slots_kept`` — no
      action required, and crucially their existing workouts stay put
      with their UUIDs intact. That's the property that fixes the
      browser-mid-edit race.

    ``today`` is an injection seam for tests.
    """
    from .models import PlanSlot, Workout, WorkoutCategory, WorkoutStatus

    today = today or _date.today()
    weeks = max(1, min(52, int(weeks or 1)))
    template_by_weekday = _parse_schedule_template(schedule_json)
    start_date = plan.start_date
    plan_monday = start_date - timedelta(days=start_date.weekday())

    def slot_date(week_idx: int, weekday: int) -> _date:
        return plan_monday + timedelta(days=week_idx * 7 + weekday)

    def in_scope(week_idx: int, weekday: int) -> bool:
        d = slot_date(week_idx, weekday)
        return d >= today and d >= start_date

    desired_keys: set[SlotKey] = {
        SlotKey(week_index=w, weekday=d) for w in range(weeks) for d in template_by_weekday.keys() if in_scope(w, d)
    }

    current_slots = list(PlanSlot.objects.filter(plan=plan, archived_at__isnull=True))
    current_by_key: dict[SlotKey, Any] = {SlotKey(week_index=s.week_index, weekday=s.weekday): s for s in current_slots}
    current_keys_in_scope = {k for k in current_by_key.keys() if in_scope(k.week_index, k.weekday)}

    new_keys = sorted(
        desired_keys - current_keys_in_scope,
        key=lambda k: (k.week_index, k.weekday),
    )
    archive_keys = current_keys_in_scope - desired_keys
    kept_keys = current_keys_in_scope & desired_keys

    slots_to_archive = [current_by_key[k] for k in archive_keys]
    slots_kept = [current_by_key[k] for k in kept_keys]

    # Existing slot-less planned workouts for this plan, keyed by date so
    # we can offer to adopt them to a new slot instead of creating a
    # duplicate. Covers the case where a plan was created before slots
    # existed (or via a code path that didn't link them).
    slotless_by_date: dict[_date, Any] = {}
    for w in Workout.objects.filter(plan=plan, slot__isnull=True, status=WorkoutStatus.PLANNED, date__gte=today):
        # First slot-less workout per date wins; subsequent get duplicates handled separately.
        slotless_by_date.setdefault(w.date, w)

    def _template_patch_for(workout, template: dict) -> dict:
        """Compute the field-level patch to bring ``workout`` in line with
        the new ``template``. Only fields the template EXPLICITLY sets are
        applied — silence is treated as "leave alone." Matches the safe
        intersection of the old DELETE+INSERT "template wins" behavior and
        the new "preserve user customization where the assistant didn't
        speak" guarantee.

        User edits during the lock window are protected separately by
        ``apply_reconciliation`` calling ``edit_lock_check`` and skipping
        the retemplate.
        """
        patch: dict[str, Any] = {}
        if "category" in template:
            cat = template["category"]
            if cat in WorkoutCategory.values and workout.category != cat:
                patch["category"] = cat
        if "activity" in template:
            new_activity = str(template["activity"]).strip()
            if new_activity and workout.activity.strip() != new_activity:
                patch["activity"] = new_activity
        if "duration_minutes" in template:
            raw = template["duration_minutes"]
            try:
                new_dur = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                new_dur = workout.duration_minutes
            if new_dur != workout.duration_minutes:
                patch["duration_minutes"] = new_dur
        if "detail_json" in template and isinstance(template["detail_json"], dict):
            if workout.detail_json != template["detail_json"]:
                patch["detail_json"] = template["detail_json"]
        return patch

    workouts_to_create: list[WorkoutSpec] = []
    workouts_to_adopt: list[tuple[Any, SlotKey, dict]] = []
    for key in new_keys:
        d = slot_date(key.week_index, key.weekday)
        template = template_by_weekday.get(key.weekday) or {}
        existing = slotless_by_date.pop(d, None)
        if existing is not None:
            workouts_to_adopt.append((existing, key, _template_patch_for(existing, template)))
            continue
        category = template.get("category", "other")
        if category not in WorkoutCategory.values:
            category = "other"
        activity = str(template.get("activity", WorkoutCategory(category).label)).strip()
        duration = template.get("duration_minutes")
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        detail = template.get("detail_json")
        if not isinstance(detail, dict):
            detail = {}
        workouts_to_create.append(
            WorkoutSpec(
                slot_key=key,
                date=d,
                category=category,
                activity=activity,
                duration_minutes=duration,
                detail_json=detail,
            )
        )

    # For kept slots: propagate template-driven fields to existing workouts.
    # Matches the old DELETE+INSERT behavior of "template wins on the fields
    # it specifies." The lock check in apply_reconciliation gates the write,
    # so a mid-edit user still wins. Activity is intentionally NOT retemplated:
    # the slot identity preserves user-rename semantics (different from the
    # old behavior where (date, activity) match would discard a renamed row).
    workouts_to_retemplate: list[tuple[Any, dict]] = []
    if slots_kept:
        kept_workouts = Workout.objects.filter(
            plan=plan,
            slot__in=slots_kept,
            status=WorkoutStatus.PLANNED,
            date__gte=today,
        )
        kept_by_slot_id = {w.slot_id: w for w in kept_workouts}
        for slot in slots_kept:
            w = kept_by_slot_id.get(slot.id)
            if w is None:
                continue
            template = template_by_weekday.get(slot.weekday) or {}
            patch = _template_patch_for(w, template)
            if patch:
                workouts_to_retemplate.append((w, patch))

    workouts_to_delete: list[Any] = []
    if slots_to_archive:
        workouts_to_delete = list(
            Workout.objects.filter(
                plan=plan,
                slot__in=slots_to_archive,
                status=WorkoutStatus.PLANNED,
                date__gte=today,
            )
        )

    return PlanReconciliation(
        plan_id=str(plan.id),
        new_slot_keys=new_keys,
        slots_to_archive=slots_to_archive,
        slots_kept=slots_kept,
        workouts_to_delete=workouts_to_delete,
        workouts_to_create=workouts_to_create,
        workouts_to_adopt=workouts_to_adopt,
        workouts_to_retemplate=workouts_to_retemplate,
    )


def apply_reconciliation(
    rec: PlanReconciliation,
    *,
    plan,
    tenant,
    edit_lock_check: Callable[[Any], bool] | None = None,
) -> dict[str, int]:
    """Commit a :class:`PlanReconciliation` in a single transaction.

    Returns a telemetry dict keyed by action. ``edit_lock_check`` is an
    optional callable taking a ``Workout`` and returning True if it's
    currently edit-locked; locked workouts are NOT deleted (they hang
    onto their existing FK pointing at the now-archived slot — the
    deliberate "orphan-for-audit" pattern).
    """
    from django.db import transaction
    from django.utils import timezone

    from .models import PlanSlot, Workout, WorkoutSource, WorkoutStatus

    now = timezone.now()
    counts = {
        "slots_created": 0,
        "slots_archived": 0,
        "workouts_created": 0,
        "workouts_adopted": 0,
        "workouts_retemplated": 0,
        "workouts_deleted": 0,
        "workouts_locked_skip": 0,
    }

    with transaction.atomic():
        new_slot_by_key: dict[SlotKey, Any] = {}
        for key in rec.new_slot_keys:
            slot = PlanSlot.objects.create(
                tenant=tenant,
                plan=plan,
                week_index=key.week_index,
                weekday=key.weekday,
            )
            new_slot_by_key[key] = slot
            counts["slots_created"] += 1

        for slot in rec.slots_to_archive:
            slot.archived_at = now
            slot.save(update_fields=["archived_at"])
            counts["slots_archived"] += 1

        for w in rec.workouts_to_delete:
            if edit_lock_check and edit_lock_check(w):
                counts["workouts_locked_skip"] += 1
                continue
            w.delete()
            counts["workouts_deleted"] += 1

        for workout, slot_key, patch in rec.workouts_to_adopt:
            if edit_lock_check and edit_lock_check(workout):
                counts["workouts_locked_skip"] += 1
                continue
            slot = new_slot_by_key.get(slot_key)
            if slot is None:
                continue
            workout.slot = slot
            update_fields = ["slot"]
            for k, v in patch.items():
                setattr(workout, k, v)
                update_fields.append(k)
            update_fields.append("updated_at")
            workout.save(update_fields=update_fields)
            counts["workouts_adopted"] += 1

        for workout, patch in rec.workouts_to_retemplate:
            if edit_lock_check and edit_lock_check(workout):
                counts["workouts_locked_skip"] += 1
                continue
            update_fields = []
            for k, v in patch.items():
                setattr(workout, k, v)
                update_fields.append(k)
            if update_fields:
                update_fields.append("updated_at")
                workout.save(update_fields=update_fields)
                counts["workouts_retemplated"] += 1

        for spec in rec.workouts_to_create:
            slot = new_slot_by_key.get(spec.slot_key)
            if slot is None:
                continue
            Workout.objects.create(
                tenant=tenant,
                plan=plan,
                slot=slot,
                date=spec.date,
                status=WorkoutStatus.PLANNED,
                source=WorkoutSource.ASSISTANT,
                category=spec.category,
                activity=spec.activity,
                duration_minutes=spec.duration_minutes,
                detail_json=spec.detail_json,
            )
            counts["workouts_created"] += 1

    return counts


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


# --------------------------------------------------------------------------
# Trends digest — computed workout aggregates the assistant reasons from.
# --------------------------------------------------------------------------
#
# A coach reasons from *trends* (weekly volume, what you train, how recently,
# whether load is climbing), not a raw list of the last few sessions. These
# feed two surfaces: the always-on USER.md ``fuel`` section
# (``render_fuel`` → ``weekly_trends_digest``) and the on-demand
# ``nbhd_fuel_summary`` tool (``RuntimeFuelSummaryView`` → ``weekly_trends``).
# Source-agnostic by design: a session counts toward volume whether it came
# from Apple Health, the app, or a chat log — provenance is carried per-row
# in ``Workout.source`` and surfaced separately.

_TRENDS_WINDOW_DAYS = 28
_PR_UNIT = {"est_1rm": " kg", "distance": " km", "hold_s": " s", "reps": " reps"}


def weekly_trends(tenant) -> dict:
    """Structured workout aggregates over the last 4 weeks, or ``{}`` if none.

    Returns volume (7d + 28d sessions/minutes), frequency-by-category,
    days-since-last per category (recency), recent personal records (load
    progression), and a coarse 7d-vs-prior-7d volume trend. Tenant-local
    day boundaries so a JST tenant's "this week" doesn't flip at 09:00.
    """
    from datetime import timedelta

    from django.db.models import Count, Max, Sum

    from apps.common.tenant_tz import tenant_today

    from .models import PersonalRecord, Workout, WorkoutStatus

    today = tenant_today(tenant)
    start_28 = today - timedelta(days=_TRENDS_WINDOW_DAYS - 1)
    start_7 = today - timedelta(days=6)
    prior_7_start = today - timedelta(days=13)
    prior_7_end = today - timedelta(days=7)

    done = Workout.objects.filter(tenant=tenant, status=WorkoutStatus.DONE, date__gte=start_28, date__lte=today)

    def _vol(qs) -> tuple[int, int]:
        agg = qs.aggregate(n=Count("id"), mins=Sum("duration_minutes"))
        return agg["n"] or 0, agg["mins"] or 0

    sessions_28, minutes_28 = _vol(done)
    if sessions_28 == 0:
        return {}

    sessions_7, minutes_7 = _vol(done.filter(date__gte=start_7))
    minutes_prior_7 = (
        done.filter(date__gte=prior_7_start, date__lte=prior_7_end).aggregate(mins=Sum("duration_minutes"))["mins"] or 0
    )

    by_category = list(
        done.values("category")
        .annotate(count=Count("id"), minutes=Sum("duration_minutes"))
        .order_by("-count", "-minutes")
    )

    recency_days = {
        row["category"]: (today - row["last"]).days for row in done.values("category").annotate(last=Max("date"))
    }

    recent_prs = list(
        PersonalRecord.objects.filter(tenant=tenant, date__gte=start_28)
        .order_by("-date", "-value")[:3]
        .values("exercise_name", "value", "metric", "date")
    )

    if minutes_prior_7 == 0:
        trend = "up" if minutes_7 > 0 else "flat"
    elif minutes_7 > minutes_prior_7 * 1.1:
        trend = "up"
    elif minutes_7 < minutes_prior_7 * 0.9:
        trend = "down"
    else:
        trend = "flat"

    return {
        "sessions_7d": sessions_7,
        "minutes_7d": minutes_7,
        "sessions_28d": sessions_28,
        "minutes_28d": minutes_28,
        "by_category": by_category,
        "recency_days": recency_days,
        "recent_prs": recent_prs,
        "volume_trend": trend,
    }


def weekly_trends_digest(tenant) -> str:
    """Terse markdown rendering of :func:`weekly_trends` for USER.md, or "".

    Kept to ~4 lines — this rides inside the char-capped ``fuel`` envelope
    section, so it must out-earn the raw session rows it replaces.
    """
    t = weekly_trends(tenant)
    if not t:
        return ""

    arrow = {"up": "↑", "down": "↓", "flat": "→"}[t["volume_trend"]]
    lines = ["**Trends** (last 4 wks):"]
    lines.append(
        f"- {t['sessions_28d']} sessions · {t['minutes_28d']} min — "
        f"this wk {t['sessions_7d']} · {t['minutes_7d']} min {arrow}"
    )
    if t["by_category"]:
        freq = ", ".join(f"{c['category']} ×{c['count']}" for c in t["by_category"][:4])
        lines.append(f"- By activity: {freq}")
    if t["recency_days"]:
        rec = sorted(t["recency_days"].items(), key=lambda kv: kv[1])[:3]
        recs = ", ".join(f"{cat} today" if days <= 0 else f"{cat} {days}d ago" for cat, days in rec)
        lines.append(f"- Last: {recs}")
    if t["recent_prs"]:

        def _fmt_pr(pr: dict) -> str:
            val = pr["value"]
            shown = f"{val:.0f}" if val == val.to_integral_value() else f"{val}"
            unit = _PR_UNIT.get(pr["metric"], "")
            return f"{pr['exercise_name']} {shown}{unit} ({pr['date'].strftime('%b %d')})"

        lines.append("- PRs: " + ", ".join(_fmt_pr(pr) for pr in t["recent_prs"]))
    return "\n".join(lines)
