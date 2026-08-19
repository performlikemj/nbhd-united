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
    rpe: int | None = None


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


def _effective_template_for_week(
    base_by_weekday: dict[int, dict],
    week_overrides: dict | None,
    week_index: int,
) -> dict[int, dict]:
    """Resolve the per-week ``{weekday_int: template}`` for ``week_index``.

    Mirrors the POST path (:func:`_expand_plan_workouts`): the override for
    this week (keyed by the stringified week index) is merged over the base
    template, with a weekday mapped to ``None`` dropped (rest day). When no
    override applies, the base template is returned unchanged.
    """
    override = (week_overrides or {}).get(str(week_index))
    if not isinstance(override, dict):
        return base_by_weekday
    effective = dict(base_by_weekday)
    for day_key, day_val in override.items():
        try:
            day_int = int(day_key)
        except (TypeError, ValueError):
            continue
        if not (0 <= day_int <= 6):
            continue
        if day_val is None:
            effective.pop(day_int, None)
        elif isinstance(day_val, dict):
            effective[day_int] = day_val
    return effective


def plan_progress_fields(plan, today: _date) -> dict:
    """Additive program-progress fields derived from a plan + tenant-local today.

    Pure — no DB. ``today`` MUST come from the tenant-tz front door
    (``apps.common.tenant_tz.tenant_today`` / ``today_in_tenant_tz``), never a
    bare ``date.today()``, so a tenant offset from UTC doesn't see the wrong day.

    Returns:
      * ``end_date``       — ISO string of the INCLUSIVE last program day
                             (``WorkoutPlan.end_date``).
      * ``days_remaining`` — ``max(0, (end_date - today).days)``; 0 on the final
                             day and once the program is over.
      * ``current_week``   — 1-based week index, clamped to ``[1, weeks]`` (week
                             1 before the plan starts, ``weeks`` after it ends).
    """
    end = plan.end_date
    days_remaining = max(0, (end - today).days)
    elapsed_days = (today - plan.start_date).days
    if elapsed_days < 0:
        current_week = 1
    else:
        current_week = min(plan.weeks, elapsed_days // 7 + 1)
    return {
        "end_date": end.isoformat(),
        "days_remaining": days_remaining,
        "current_week": current_week,
    }


def rest_dates_for_window(
    tenant,
    window_start: _date,
    window_end: _date,
    *,
    plans=None,
) -> set[_date]:
    """Dates in ``[window_start, window_end]`` that are programmed REST days.

    A date is REST iff at least one ACTIVE plan covers it within its
    ``[start_date, end_date]`` span AND no active plan's *effective* template
    (``_effective_template_for_week`` — which already drops ``week_overrides``
    null-days) trains that weekday for that date. Multi-plan union: a date is
    rest only when NO active plan trains it (one plan training it wins).

    Pure Python over the fetched plans — the ONLY DB hit is the active-plans
    fetch, and callers with the plans already in hand pass them via ``plans`` to
    avoid even that. Callers subtract dates carrying a real ``Workout`` row: an
    ad-hoc row always wins over a computed rest day.
    """
    from .models import PlanStatus, WorkoutPlan

    if plans is None:
        plans = list(WorkoutPlan.objects.filter(tenant=tenant, status=PlanStatus.ACTIVE))
    else:
        plans = [p for p in plans if p.status == PlanStatus.ACTIVE]
    if not plans:
        return set()

    # Precompute each plan's base template + Monday-of-week-0 anchor once.
    prepared = []
    for p in plans:
        base_by_weekday = _parse_schedule_template(p.schedule_json)
        plan_monday = p.start_date - timedelta(days=p.start_date.weekday())
        prepared.append((p, base_by_weekday, plan_monday))

    rest: set[_date] = set()
    d = window_start
    while d <= window_end:
        covered = False
        trained = False
        for p, base_by_weekday, plan_monday in prepared:
            if not (p.start_date <= d <= p.end_date):
                continue
            covered = True
            week_index = (d - plan_monday).days // 7
            effective = _effective_template_for_week(base_by_weekday, p.week_overrides, week_index)
            if d.weekday() in effective:
                trained = True
                break
        if covered and not trained:
            rest.add(d)
        d += timedelta(days=1)
    return rest


def reconcile_plan_state(
    plan,
    schedule_json: dict,
    weeks: int,
    *,
    today: _date | None = None,
    week_overrides: dict | None = None,
) -> PlanReconciliation:
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
    base_by_weekday = _parse_schedule_template(schedule_json)
    start_date = plan.start_date
    plan_monday = start_date - timedelta(days=start_date.weekday())

    # Resolve the effective per-week template once. With week_overrides a
    # given weekday's template (or its very presence, for rest-day drops)
    # varies by week, so the schedule has a week dimension here — matching
    # the POST/create path in _expand_plan_workouts.
    template_by_week: dict[int, dict[int, dict]] = {
        w: _effective_template_for_week(base_by_weekday, week_overrides, w) for w in range(weeks)
    }

    def template_for(week_idx: int, weekday: int) -> dict:
        return template_by_week.get(week_idx, base_by_weekday).get(weekday) or {}

    def slot_date(week_idx: int, weekday: int) -> _date:
        return plan_monday + timedelta(days=week_idx * 7 + weekday)

    def in_scope(week_idx: int, weekday: int) -> bool:
        d = slot_date(week_idx, weekday)
        return d >= today and d >= start_date

    desired_keys: set[SlotKey] = {
        SlotKey(week_index=w, weekday=d) for w in range(weeks) for d in template_by_week[w].keys() if in_scope(w, d)
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
        # The create path maps the template's ``target_rpe`` (or ``rpe``) onto
        # Workout.rpe; mirror that here so a per-week deload that lowers the
        # target RPE actually re-prescribes a kept workout.
        if "target_rpe" in template or "rpe" in template:
            raw_rpe = template.get("target_rpe", template.get("rpe"))
            try:
                new_rpe = max(1, min(10, int(raw_rpe))) if raw_rpe is not None else None
            except (TypeError, ValueError):
                new_rpe = workout.rpe
            if new_rpe != workout.rpe:
                patch["rpe"] = new_rpe
        return patch

    workouts_to_create: list[WorkoutSpec] = []
    workouts_to_adopt: list[tuple[Any, SlotKey, dict]] = []
    for key in new_keys:
        d = slot_date(key.week_index, key.weekday)
        template = template_for(key.week_index, key.weekday)
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
        raw_rpe = template.get("target_rpe", template.get("rpe"))
        try:
            rpe = max(1, min(10, int(raw_rpe))) if raw_rpe is not None else None
        except (TypeError, ValueError):
            rpe = None
        workouts_to_create.append(
            WorkoutSpec(
                slot_key=key,
                date=d,
                category=category,
                activity=activity,
                duration_minutes=duration,
                detail_json=detail,
                rpe=rpe,
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
            template = template_for(slot.week_index, slot.weekday)
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
    writer: str = "background",
    edit_lock_check: Callable[[Any], bool] | None = None,
) -> dict[str, int]:
    """Commit a :class:`PlanReconciliation` in a single transaction.

    Returns a telemetry dict keyed by action. ``writer`` is the provenance of
    the plan edit that produced the template diff. ``edit_lock_check`` is an
    optional callable taking a ``Workout`` and returning True if it's
    currently edit-locked; locked workouts are NOT deleted (they hang
    onto their existing FK pointing at the now-archived slot — the
    deliberate "orphan-for-audit" pattern).
    """
    from django.db import transaction
    from django.utils import timezone

    from apps.pii.store_authoring import author_store_fields

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

    registered_workout_fields = frozenset({"skip_reason", "activity", "notes", "notes_thread", "detail_json"})

    def _author_patch(patch: dict[str, Any]) -> tuple[dict[str, Any], dict | None]:
        if not registered_workout_fields.intersection(patch):
            return patch, None
        authored, receipts = author_store_fields(
            tenant,
            patch,
            model_label="fuel.Workout",
            seam=f"fuel.{writer}.plan.reconcile",
            writer=writer,
            defer_detection=writer == "runtime",
        )
        return authored, receipts

    authored_adoptions = [
        (workout, slot_key, *_author_patch(patch)) for workout, slot_key, patch in rec.workouts_to_adopt
    ]
    authored_retemplates = [(workout, *_author_patch(patch)) for workout, patch in rec.workouts_to_retemplate]
    authored_creations = []
    for spec in rec.workouts_to_create:
        authored, receipts = author_store_fields(
            tenant,
            {"activity": spec.activity, "detail_json": spec.detail_json},
            model_label="fuel.Workout",
            seam=f"fuel.{writer}.plan.reconcile",
            writer=writer,
            defer_detection=writer == "runtime",
        )
        authored_creations.append((spec, authored, receipts))

    # Every locked refetch below deliberately reasserts the diff-time state.
    # If that state changed, skip without counting a mutation or an edit-lock
    # skip: those counters describe work actually applied or explicitly locked.
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

        for stale_workout in rec.workouts_to_delete:
            workout = (
                Workout.objects.select_for_update()
                .filter(
                    pk=stale_workout.pk,
                    tenant=tenant,
                    plan=plan,
                    status=WorkoutStatus.PLANNED,
                )
                .first()
            )
            if workout is None:
                continue
            if edit_lock_check and edit_lock_check(workout):
                counts["workouts_locked_skip"] += 1
                continue
            workout.delete()
            counts["workouts_deleted"] += 1

        for stale_workout, slot_key, patch, field_receipts in authored_adoptions:
            workout = (
                Workout.objects.select_for_update()
                .filter(
                    pk=stale_workout.pk,
                    tenant=tenant,
                    plan=plan,
                    status=WorkoutStatus.PLANNED,
                    slot__isnull=True,
                )
                .first()
            )
            if workout is None:
                continue
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
            if field_receipts is not None:
                workout.pii_receipts = {**(workout.pii_receipts or {}), **field_receipts}
                update_fields.append("pii_receipts")
            update_fields.append("updated_at")
            workout.save(update_fields=update_fields)
            counts["workouts_adopted"] += 1

        for stale_workout, patch, field_receipts in authored_retemplates:
            workout = (
                Workout.objects.select_for_update()
                .filter(
                    pk=stale_workout.pk,
                    tenant=tenant,
                    plan=plan,
                    status=WorkoutStatus.PLANNED,
                    slot_id=stale_workout.slot_id,
                )
                .first()
            )
            if workout is None:
                continue
            if edit_lock_check and edit_lock_check(workout):
                counts["workouts_locked_skip"] += 1
                continue
            update_fields = []
            for k, v in patch.items():
                setattr(workout, k, v)
                update_fields.append(k)
            if field_receipts is not None:
                workout.pii_receipts = {**(workout.pii_receipts or {}), **field_receipts}
                update_fields.append("pii_receipts")
            if update_fields:
                update_fields.append("updated_at")
                workout.save(update_fields=update_fields)
                counts["workouts_retemplated"] += 1

        for spec, authored, receipts in authored_creations:
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
                activity=authored["activity"],
                duration_minutes=spec.duration_minutes,
                detail_json=authored["detail_json"],
                rpe=spec.rpe,
                pii_receipts=receipts,
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


def _fmt_pr_num(value) -> str:
    """Render a PR/set number without trailing zeroes."""
    return f"{float(value):g}"


def _est_1rm_source_set(pr) -> dict | None:
    """Return the workout set that produced an estimated-1RM PR, if retained."""
    workout = getattr(pr, "workout", None)
    detail = getattr(workout, "detail_json", None)
    if not isinstance(detail, dict):
        return None

    candidates = []
    for exercise in detail.get("exercises", []):
        if not isinstance(exercise, dict) or exercise.get("name", "").strip() != pr.exercise_name:
            continue
        for workout_set in exercise.get("sets", []):
            if not isinstance(workout_set, dict):
                continue
            weight = _safe_num(workout_set.get("weight"))
            reps = _safe_num(workout_set.get("reps"))
            estimate = est_1rm(weight, reps)
            if estimate > 0:
                candidates.append((estimate, weight, reps))

    if not candidates:
        return None
    estimate, weight, reps = max(candidates, key=lambda item: item[0])
    if abs(estimate - float(pr.value)) > 0.05:
        return None
    return {"weight_kg": _fmt_pr_num(weight), "reps": int(reps) if reps.is_integer() else reps}


def format_pr_display(pr) -> str:
    """Human-honest PR rendering for every assistant/user-facing surface."""
    value = _fmt_pr_num(pr.value)
    if pr.metric == "est_1rm":
        display = f"est. 1RM {value} kg"
        source_set = _est_1rm_source_set(pr)
        if source_set:
            display += f" (from {source_set['weight_kg']} kg × {source_set['reps']:g})"
        return display
    unit = _PR_UNIT.get(pr.metric, "")
    return f"{value}{unit}"


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


def aggregate_cardio_progress(workouts, *, tenant=None) -> dict:
    """Build pace and distance trends from cardio workouts.

    Malformed rows are SKIPPED and counted, never fatal. ``distance_km`` went
    unvalidated on the write paths for a long time, and the bare ``float()``
    this used to do meant one row carrying "5 miles" took the user's whole
    cardio Progress view down with a 500 — on every load, permanently, with no
    way back short of editing the row. The write paths now reject non-numeric
    values (``set_contract.validate_flat_detail``), but rows written before
    that still exist, so the read side has to survive them on its own.

    ``skipped_rows`` appears in the result only when something was stepped
    over, so the damage is visible in the response instead of silent. ``tenant``
    is optional and used only to attribute the telemetry event.
    """
    pace_points = []
    dist_points = []
    total_km = 0.0
    skipped = 0

    for w in sorted(workouts, key=lambda w: w.date):
        d = w.detail_json or {}
        if not isinstance(d, dict):
            skipped += 1
            continue
        if d.get("pace"):
            parts = str(d["pace"]).split(":")
            try:
                secs = int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 else int(parts[0]) * 60
                pace_points.append({"date": str(w.date), "value": secs})
            except (ValueError, IndexError):
                pass
        if d.get("distance_km"):
            try:
                km = float(d["distance_km"])
            except (TypeError, ValueError):
                skipped += 1
                continue
            total_km += km
            dist_points.append({"date": str(w.date), "value": km})

    result = {"pace": pace_points, "distance": dist_points, "total_km": round(total_km, 1)}
    if skipped:
        from apps.platform_logs.telemetry import emit_tool_event

        result["skipped_rows"] = skipped
        emit_tool_event(
            tool_name="fuel-cardio-progress",
            outcome="normalized",
            namespace="fuel",
            tenant_id=getattr(tenant, "id", None),
            reason_code="cardio_legacy_row_skipped",
            detail={"cardio_rows_skipped": skipped},
        )
    return result


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
                    {
                        "exercise": name,
                        "value": float(pr.value),
                        "previous": float(prev_value) if prev_value else None,
                        "metric": pr.metric,
                        "display": format_pr_display(pr),
                    }
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

    recent_pr_rows = list(
        PersonalRecord.objects.filter(tenant=tenant, date__gte=start_28)
        .select_related("workout")
        .order_by("-date", "-value")[:3]
    )
    recent_prs = [
        {
            "exercise_name": pr.exercise_name,
            "value": pr.value,
            "metric": pr.metric,
            "date": pr.date,
            "display": format_pr_display(pr),
        }
        for pr in recent_pr_rows
    ]

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
            return f"{pr['exercise_name']} — {pr['display']} ({pr['date'].strftime('%b %d')})"

        lines.append("- PRs: " + ", ".join(_fmt_pr(pr) for pr in t["recent_prs"]))
    return "\n".join(lines)


def _fmt_decimal(val) -> str:
    """Render a Decimal without trailing ``.00`` — ``75.00`` → ``"75"``."""
    return f"{val:.0f}" if val == val.to_integral_value() else f"{val}"


def all_time_prs(tenant, limit: int = 20) -> list[dict]:
    """Personal records newest-first, capped — the same lifetime list the human
    PR feed (:class:`~apps.fuel.views.PRFeedView`) shows.

    Unlike ``weekly_trends()['recent_prs']`` this is NOT windowed to 28 days, so
    the assistant can reference lifetime bests. Compact by design (it enters
    model context): one row per PR, four fields.
    """
    from .models import PersonalRecord

    rows = (
        PersonalRecord.objects.filter(tenant=tenant).select_related("workout").order_by("-date", "-created_at")[:limit]
    )
    return [
        {
            "exercise": pr.exercise_name,
            "value": _fmt_decimal(pr.value),
            "metric": pr.metric,
            "display": format_pr_display(pr),
            "date": str(pr.date),
        }
        for pr in rows
    ]


def monthly_volume_12mo(tenant) -> list[dict]:
    """12 monthly datapoints (oldest→newest) of session count + total minutes.

    Gives the assistant a *year* of load history instead of only the 4 weeks
    ``weekly_trends`` covers. Months with no completed workouts are emitted as
    zeros so the trend line is honest. Tenant-local month boundaries.
    """
    from dateutil.relativedelta import relativedelta
    from django.db.models import Count, Sum
    from django.db.models.functions import TruncMonth

    from apps.common.tenant_tz import tenant_today

    from .models import Workout, WorkoutStatus

    today = tenant_today(tenant)
    # First day of the month 11 months ago → 12 months inclusive of this one.
    first_month = today.replace(day=1) - relativedelta(months=11)

    rows = (
        Workout.objects.filter(
            tenant=tenant,
            status=WorkoutStatus.DONE,
            date__gte=first_month,
            date__lte=today,
        )
        .annotate(m=TruncMonth("date"))
        .values("m")
        .annotate(sessions=Count("id"), minutes=Sum("duration_minutes"))
    )
    by_month = {r["m"].strftime("%Y-%m"): (r["sessions"] or 0, r["minutes"] or 0) for r in rows}

    out = []
    for i in range(12):
        key = (first_month + relativedelta(months=i)).strftime("%Y-%m")
        sessions, minutes = by_month.get(key, (0, 0))
        out.append({"month": key, "sessions": sessions, "minutes": minutes})
    return out


def open_goals(tenant) -> list[dict]:
    """The user's not-yet-achieved fitness goals — exercise, target, deadline.

    Wires the human-typed ``FuelGoal`` rows to the assistant for the first time
    so it can program toward them. Compact: unachieved goals only, few fields.
    Dated goals sort first (soonest deadline), undated last.
    """
    from .models import FuelGoal

    rows = FuelGoal.objects.filter(tenant=tenant, achieved_at__isnull=True).order_by("target_date", "-created_at")
    return [
        {
            "exercise": g.exercise_name,
            "metric": g.metric,
            "target_value": _fmt_decimal(g.target_value),
            "target_date": str(g.target_date) if g.target_date else None,
        }
        for g in rows
    ]
