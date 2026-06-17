"""HealthKit sync ingestion — idempotent import of Apple Health workouts
and daily health metrics into the fuel pillar.

Serves ``POST /api/v1/fuel/healthkit/sync/`` (consumer JWT, iOS app).
Full design + red-team record: ``CONTINUITY_healthkit_sync.md``.

Invariants this module owns:

- ``external_id`` (the HealthKit sample UUID) is the idempotency anchor —
  backed by the partial unique constraint on ``(tenant, external_id)``.
  Existing rows are NEVER updated by a re-delivery (user edits survive),
  and deleted imports are tombstoned on the FuelProfile so a client
  anchor reset (app reinstall, re-backfill) cannot resurrect them.
- NO batch-wide transaction: every item commits in its own atomic block,
  so one IntegrityError cannot poison the rest of the batch and the
  tenants-row lock taken by the per-save ``fuel_version`` bump is held
  per item, never for the whole batch.
- Planned-workout auto-complete locks the candidate with
  ``select_for_update`` and re-checks ``status=planned`` after the lock
  (the finance ``record_transaction`` shape) — concurrent device syncs
  or an assistant complete racing the flip degrade to a standalone
  create instead of a double-complete.
- The caller (view) wraps the loop in ``suppress_refresh()`` +
  ``suppress_cron_regen()`` and restores visibility with ONE
  ``push_visibility_refresh`` after the loop. That push uses
  ``debounce_seconds=0`` deliberately: the leading-edge debounce key is
  shared with conversation capture, so a windowed push fired within two
  minutes of any chat turn would be silently dropped and the synced
  data would never reach USER.md.
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import UTC, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone as dj_timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.common.tenant_tz import tenant_tz

from .models import (
    BodyWeightLog,
    FuelProfile,
    RestingHeartRateLog,
    SleepLog,
    Workout,
    WorkoutCategory,
    WorkoutSource,
    WorkoutStatus,
)

logger = logging.getLogger(__name__)

MAX_WORKOUTS = 50
MAX_DAILY = 31
MAX_DELETED = 100

# HK activity types that must not auto-complete a planned session unless
# the plan itself is a walk/hike — a dog walk must not complete a 10K.
_LOW_SIGNAL_TYPES = {"walking", "hiking"}

# Categories whose planned sessions may be completed by each other. HK
# files most calisthenics under functionalStrengthTraining, so strength
# and calisthenics interchange; everything else must match exactly.
_COMPAT = {
    WorkoutCategory.STRENGTH: {WorkoutCategory.STRENGTH, WorkoutCategory.CALISTHENICS},
    WorkoutCategory.CALISTHENICS: {WorkoutCategory.STRENGTH, WorkoutCategory.CALISTHENICS},
}

# Fallback raw HK activity type → fuel category, used when the client
# sends an unknown/missing category. Mirrors the iOS-side map.
_RAW_TYPE_CATEGORY = {
    "running": WorkoutCategory.CARDIO,
    "walking": WorkoutCategory.CARDIO,
    "cycling": WorkoutCategory.CARDIO,
    "swimming": WorkoutCategory.CARDIO,
    "hiking": WorkoutCategory.CARDIO,
    "rowing": WorkoutCategory.CARDIO,
    "elliptical": WorkoutCategory.CARDIO,
    "stairs": WorkoutCategory.CARDIO,
    "paddleSports": WorkoutCategory.CARDIO,
    "functionalStrengthTraining": WorkoutCategory.STRENGTH,
    "traditionalStrengthTraining": WorkoutCategory.STRENGTH,
    "highIntensityIntervalTraining": WorkoutCategory.HIIT,
    "crossTraining": WorkoutCategory.HIIT,
    "coreTraining": WorkoutCategory.CALISTHENICS,
    "yoga": WorkoutCategory.MOBILITY,
    "pilates": WorkoutCategory.MOBILITY,
    "flexibility": WorkoutCategory.MOBILITY,
    "cooldown": WorkoutCategory.MOBILITY,
    "mindAndBody": WorkoutCategory.MOBILITY,
}

# Plausibility: an HK workout shorter than this fraction of the planned
# duration does not complete the plan (20-min stroll vs 60-min session).
_MIN_DURATION_RATIO = 0.5


def _safe_float(value) -> float | None:
    # OverflowError: float(10**400). Non-finite must be rejected here so
    # _safe_int's round() can never see inf/nan ("inf"/"1e400" arrive as
    # JSON strings and pass DRF's STRICT_JSON, which only rejects bare
    # NaN/Infinity literals).
    try:
        if value is None:
            return None
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _safe_int(value) -> int | None:
    f = _safe_float(value)
    return None if f is None else round(f)


def _aware(dt):
    return dj_timezone.make_aware(dt, UTC) if dj_timezone.is_naive(dt) else dt


def _safe_parse_datetime(value: str):
    # parse_datetime returns None for malformed input but RAISES ValueError
    # for well-formed-but-invalid values ("2026-02-30T10:00:00Z") — which
    # would 500 the whole request and wedge the client's anchor-retry loop.
    try:
        return parse_datetime(value)
    except ValueError:
        return None


def _safe_parse_date(value: str):
    try:
        return parse_date(value)
    except ValueError:
        return None


def _clean_workout(item) -> tuple[dict | None, str | None]:
    """Validate + normalize one workout item. Returns (clean, error).

    Model validators do not run through ``create()``, so every bound is
    enforced here (the same posture as the manual checks in
    runtime_views/views for sleep/weight/RHR).
    """
    if not isinstance(item, dict):
        return None, "item must be an object"

    external_id = str(item.get("external_id") or "").strip()
    if not external_id or len(external_id) > 64:
        return None, "external_id is required (max 64 chars)"

    started_at = _safe_parse_datetime(str(item.get("started_at") or "").strip())
    if started_at is None:
        return None, "started_at must be an ISO-8601 datetime"
    started_at = _aware(started_at)
    # Plausibility window — also prevents OverflowError at the astimezone
    # sites (year 1 with +14:00 offset / year 9999 east of UTC) and keeps
    # far-future rows out of the planned-matching date queries.
    now = dj_timezone.now()
    if not (now - timedelta(days=1825) <= started_at <= now + timedelta(days=2)):
        return None, "started_at out of range"

    ended_at = None
    ended_raw = str(item.get("ended_at") or "").strip()
    if ended_raw:
        ended_at = _safe_parse_datetime(ended_raw)
        if ended_at is None:
            return None, "ended_at must be an ISO-8601 datetime"
        ended_at = _aware(ended_at)
        if ended_at < started_at:
            return None, "ended_at precedes started_at"
        if ended_at > now + timedelta(days=3):
            return None, "ended_at out of range"

    duration = _safe_int(item.get("duration_minutes"))
    if duration is None:
        return None, "duration_minutes must be a number"
    if not 1 <= duration <= 1440:
        return None, "duration_minutes out of range (1-1440)"

    raw_type = str(item.get("raw_type") or "").strip()[:64]
    category = str(item.get("category") or "").strip()
    if category not in WorkoutCategory.values:
        category = _RAW_TYPE_CATEGORY.get(raw_type, WorkoutCategory.OTHER)

    activity = str(item.get("activity") or "").strip()[:128] or WorkoutCategory(category).label
    source_bundle = str(item.get("source_bundle") or "").strip()[:128]

    raw_metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    metrics: dict = {}
    distance = _safe_float(raw_metrics.get("distance_km"))
    if distance is not None and 0 < distance <= 500:
        metrics["distance_km"] = round(distance, 2)
    for key in ("avg_hr", "peak_hr"):
        bpm = _safe_int(raw_metrics.get(key))
        if bpm is not None and 20 <= bpm <= 250:
            metrics[key] = bpm
    calories = _safe_int(raw_metrics.get("calories"))
    if calories is not None and 0 < calories <= 20000:
        metrics["calories"] = calories
    # Client sends elevation_m; the canonical detail_json key every
    # existing consumer reads is `elevation` (int meters).
    elevation = _safe_int(raw_metrics.get("elevation_m"))
    if elevation is not None and 0 <= elevation <= 20000:
        metrics["elevation"] = elevation
    # Derived pace feeds the existing cardio pace trend, which only
    # reads the 'M:SS'-per-km string convention.
    if category == WorkoutCategory.CARDIO and metrics.get("distance_km"):
        pace_s = duration * 60 / metrics["distance_km"]
        if pace_s < 6000:
            metrics["pace"] = f"{int(pace_s // 60)}:{int(pace_s % 60):02d}"

    return {
        "external_id": external_id,
        "activity": activity,
        "category": category,
        "raw_type": raw_type,
        "source_bundle": source_bundle,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_minutes": duration,
        "metrics": metrics,
    }, None


def _hk_detail(clean: dict, *, matched: bool) -> dict:
    detail = dict(clean["metrics"])
    detail["started_at"] = clean["started_at"].isoformat()
    if clean["ended_at"]:
        detail["ended_at"] = clean["ended_at"].isoformat()
    hk = {"matched": matched}
    if clean["raw_type"]:
        hk["raw_type"] = clean["raw_type"]
    if clean["source_bundle"]:
        hk["source_bundle"] = clean["source_bundle"]
    detail["_healthkit"] = hk
    return detail


def _find_candidate(tenant, clean: dict, tz, consumed: set) -> Workout | None:
    """Pick the planned session this HK workout completes, or None.

    Gates (applied to the single-candidate case too): tenant-local same
    day, compatible category, duration plausibility, time window when the
    plan carries a scheduled time, no active edit lock, and the low-signal
    walk/hike exclusion. Ambiguity (several day-only candidates) means no
    guess — standalone create.
    """
    local_date = clean["started_at"].astimezone(tz).date()
    compat = _COMPAT.get(clean["category"], {clean["category"]})
    low_signal = clean["raw_type"] in _LOW_SIGNAL_TYPES
    now = dj_timezone.now()

    viable = []
    for c in Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.PLANNED,
        date=local_date,
        category__in=compat,
    ).exclude(id__in=consumed):
        if c.edit_lock_until and c.edit_lock_until > now:
            continue
        if low_signal and not any(k in (c.activity or "").lower() for k in ("walk", "hike")):
            continue
        if c.duration_minutes and clean["duration_minutes"] < _MIN_DURATION_RATIO * c.duration_minutes:
            continue
        if c.scheduled_at:
            window_start = c.window_start_at or (c.scheduled_at - timedelta(hours=2))
            window_end = c.window_end_at or (c.scheduled_at + timedelta(hours=2))
            if not (window_start <= clean["started_at"] <= window_end):
                continue
        viable.append(c)

    if not viable:
        return None
    if len(viable) == 1:
        return viable[0]
    timed = [c for c in viable if c.scheduled_at]
    if not timed:
        return None
    return min(timed, key=lambda c: abs(c.scheduled_at - clean["started_at"]))


def _complete_planned(locked: Workout, clean: dict) -> dict:
    """Flip a locked planned row to done with the HK actuals.

    Caller holds ``select_for_update`` on the row inside an atomic block.
    Planned exercises/detail are preserved; measured metric keys win.
    """
    merged = dict(locked.detail_json or {})
    merged.update(_hk_detail(clean, matched=True))

    thread = list(locked.notes_thread or [])
    thread.append(
        {
            "at": dj_timezone.now().isoformat(),
            "who": "system",
            "text": f"Marked done from Apple Health ({clean['activity']}, {clean['duration_minutes']}m)",
        }
    )

    locked.status = WorkoutStatus.DONE
    locked.duration_minutes = clean["duration_minutes"]
    locked.detail_json = merged
    locked.external_id = clean["external_id"]
    locked.notes_thread = thread
    locked.version += 1
    locked.save(
        update_fields=[
            "status",
            "duration_minutes",
            "detail_json",
            "external_id",
            "notes_thread",
            "version",
            "updated_at",
        ]
    )

    return {
        "external_id": clean["external_id"],
        "status": "matched_planned",
        "workout_id": str(locked.id),
    }


def _find_adoptable_log(tenant, clean: dict, tz, consumed: set) -> Workout | None:
    """Pick an existing manually-logged DONE session this HK sample is a
    duplicate of, or None.

    The same physical session logged in-app / in chat (``external_id=""``)
    and then synced from Apple Health would otherwise become two rows — the
    manual one escapes the partial unique constraint (empty external_id)
    and double-counts in every volume/frequency aggregate. When a confident
    single match exists the caller ADOPTS it (stamps the HK external_id +
    fills measured metrics) instead of creating a duplicate — symmetric
    with how a PLANNED row is completed.

    Conservative by construction (a false merge silently undercounts, which
    is worse than a duplicate the user can delete): same tenant-local day,
    compatible category, duration plausibility BOTH directions, time window
    when the row carries a scheduled time, edit-lock-aware, low-signal
    walk/hike exclusion, and ambiguity (several day-only candidates) means
    no guess.
    """
    local_date = clean["started_at"].astimezone(tz).date()
    compat = _COMPAT.get(clean["category"], {clean["category"]})
    low_signal = clean["raw_type"] in _LOW_SIGNAL_TYPES
    now = dj_timezone.now()
    dur = clean["duration_minutes"]

    viable = []
    for c in Workout.objects.filter(
        tenant=tenant,
        status=WorkoutStatus.DONE,
        external_id="",
        date=local_date,
        category__in=compat,
    ).exclude(id__in=consumed):
        if c.edit_lock_until and c.edit_lock_until > now:
            continue
        if low_signal and not any(k in (c.activity or "").lower() for k in ("walk", "hike")):
            continue
        # Same session ⇒ similar duration. Reject both a too-short stroll
        # standing in for a long run and a too-long lift for a short flow.
        if c.duration_minutes:
            lo = _MIN_DURATION_RATIO * c.duration_minutes
            hi = c.duration_minutes / _MIN_DURATION_RATIO
            if not (lo <= dur <= hi):
                continue
        elif not c.scheduled_at:
            # No duration AND no scheduled time ⇒ no signal to tell a genuinely
            # separate same-day same-category session apart (a chat log like
            # "did a lift this morning" carries neither). Adopting here would
            # risk a silent false-merge — an undercount, which this module is
            # deliberately more conservative against than a duplicate the user
            # can delete — so fall through to a standalone create.
            continue
        if c.scheduled_at:
            window_start = c.window_start_at or (c.scheduled_at - timedelta(hours=2))
            window_end = c.window_end_at or (c.scheduled_at + timedelta(hours=2))
            if not (window_start <= clean["started_at"] <= window_end):
                continue
        viable.append(c)

    if not viable:
        return None
    if len(viable) == 1:
        return viable[0]
    # Multiple same-day same-category manual logs: only resolve when a
    # scheduled time lets us pick the closest; otherwise no guess.
    timed = [c for c in viable if c.scheduled_at]
    if not timed:
        return None
    return min(timed, key=lambda c: abs(c.scheduled_at - clean["started_at"]))


def _adopt_existing_log(locked: Workout, clean: dict) -> dict:
    """Stamp the HK external_id onto a pre-existing manual DONE log and
    enrich it with measured metrics, instead of creating a duplicate row.

    Non-destructive: the user's activity / duration / category and any
    detail values they entered are preserved (HK metrics only fill gaps);
    the stamped external_id makes future re-syncs idempotent and a later HK
    deletion tombstone-safe. Caller holds ``select_for_update`` on the row.
    """
    existing_detail = dict(locked.detail_json or {})
    hk_detail = _hk_detail(clean, matched=True)
    # User-entered values win on conflict; HK measured keys fill the gaps.
    merged = {**hk_detail, **existing_detail}
    hk_block = dict(hk_detail.get("_healthkit") or {})
    hk_block["adopted"] = True
    merged["_healthkit"] = hk_block

    thread = list(locked.notes_thread or [])
    thread.append(
        {
            "at": dj_timezone.now().isoformat(),
            "who": "system",
            "text": f"Linked to Apple Health ({clean['activity']}, {clean['duration_minutes']}m)",
        }
    )

    locked.external_id = clean["external_id"]
    locked.detail_json = merged
    locked.notes_thread = thread
    locked.version += 1
    locked.save(update_fields=["external_id", "detail_json", "notes_thread", "version", "updated_at"])

    return {
        "external_id": clean["external_id"],
        "status": "matched_log",
        "workout_id": str(locked.id),
    }


def _ingest_workout(tenant, clean: dict, tz, consumed: set) -> dict:
    eid = clean["external_id"]
    candidate = _find_candidate(tenant, clean, tz, consumed)
    completed_planned: Workout | None = None  # only this path runs detect_prs
    try:
        with transaction.atomic():
            if candidate is not None:
                locked = (
                    Workout.objects.select_for_update()
                    .filter(id=candidate.id, tenant=tenant, status=WorkoutStatus.PLANNED)
                    .first()
                )
                if locked is not None:
                    result = _complete_planned(locked, clean)
                    completed_planned = locked
                # Lost the race (assistant or another device flipped it
                # first) — fall through to adopt-or-create.
            if completed_planned is None:
                # No planned session to complete — does an existing manual
                # DONE log already record this same session? Adopt it rather
                # than creating a duplicate that inflates every aggregate.
                adoptable = _find_adoptable_log(tenant, clean, tz, consumed)
                locked_log = None
                if adoptable is not None:
                    locked_log = (
                        Workout.objects.select_for_update()
                        .filter(id=adoptable.id, tenant=tenant, status=WorkoutStatus.DONE, external_id="")
                        .first()
                    )
                if locked_log is not None:
                    result = _adopt_existing_log(locked_log, clean)
                else:
                    workout = Workout.objects.create(
                        tenant=tenant,
                        date=clean["started_at"].astimezone(tz).date(),
                        status=WorkoutStatus.DONE,
                        source=WorkoutSource.HEALTHKIT,
                        external_id=eid,
                        category=clean["category"],
                        activity=clean["activity"],
                        duration_minutes=clean["duration_minutes"],
                        detail_json=_hk_detail(clean, matched=False),
                    )
                    result = {"external_id": eid, "status": "created", "workout_id": str(workout.id)}
    except IntegrityError:
        # The per-item savepoint already rolled back. Classify instead of
        # blanket-reporting duplicate — an FK violation must not masquerade
        # as a successful dedup.
        if Workout.objects.filter(tenant=tenant, external_id=eid).exists():
            return {"external_id": eid, "status": "duplicate"}
        logger.warning("HealthKit ingest IntegrityError (non-duplicate) for tenant %s", tenant.id, exc_info=True)
        return {"external_id": eid, "status": "error", "error": "integrity_error"}

    # detect_prs runs OUTSIDE the atomic block: a swallowed DB error inside
    # it would mark the transaction needs_rollback and silently undo the
    # flip while the response still reported matched_planned. Adoption does
    # NOT re-run it — the row was already DONE (detect_prs ran at log time)
    # and HK imports carry no strength sets for it to act on.
    if completed_planned is not None:
        try:
            from .services import detect_prs

            detect_prs(tenant, completed_planned)
        except Exception:
            logger.warning("detect_prs failed for HK-completed workout %s", completed_planned.id, exc_info=True)
    return result


def _ingest_daily(tenant, item, tz) -> dict:
    """Upsert one day's metrics. ``update_or_create`` defaults only touch
    the provided keys, so user-entered sleep quality/notes survive."""
    if not isinstance(item, dict):
        return {"date": None, "status": "error", "error": "item must be an object"}
    day = _safe_parse_date(str(item.get("date") or "").strip())
    if day is None:
        return {
            "date": item.get("date") if isinstance(item.get("date"), str) else None,
            "status": "error",
            "error": "date must be YYYY-MM-DD",
        }
    # Sanity window keyed to the tenant-local day — turns a client
    # calendar/clock bug (Buddhist-calendar year 2569, year 0008) into a
    # visible per-item error instead of silent rows on garbage dates.
    today_local = dj_timezone.now().astimezone(tz).date()
    if not (today_local - timedelta(days=400) <= day <= today_local + timedelta(days=2)):
        return {"date": day.isoformat(), "status": "error", "error": "date out of range"}

    updates: list[tuple] = []
    if item.get("resting_hr") is not None:
        bpm = _safe_int(item.get("resting_hr"))
        if bpm is None or not 20 <= bpm <= 250:
            return {"date": day.isoformat(), "status": "error", "error": "resting_hr out of range (20-250)"}
        updates.append((RestingHeartRateLog, {"bpm": bpm}))
    if item.get("sleep_hours") is not None:
        hours = _safe_float(item.get("sleep_hours"))
        if hours is None or not 0 < hours <= 24:
            return {"date": day.isoformat(), "status": "error", "error": "sleep_hours out of range (0-24]"}
        updates.append((SleepLog, {"duration_hours": Decimal(str(round(hours, 2)))}))
    if item.get("body_weight_kg") is not None:
        weight = _safe_float(item.get("body_weight_kg"))
        if weight is None or not 0 < weight <= 500:
            return {"date": day.isoformat(), "status": "error", "error": "body_weight_kg out of range (0-500]"}
        updates.append((BodyWeightLog, {"weight_kg": Decimal(str(round(weight, 2)))}))

    if not updates:
        return {"date": day.isoformat(), "status": "error", "error": "no metric values provided"}

    for model, defaults in updates:
        try:
            with transaction.atomic():
                model.objects.update_or_create(tenant=tenant, date=day, defaults=defaults)
        except IntegrityError:
            # update_or_create's get→create race under (tenant, date)
            # uniqueness — the row exists now, so a plain update wins.
            model.objects.filter(tenant=tenant, date=day).update(**defaults)
    return {"date": day.isoformat(), "status": "upserted"}


def _maybe_self_heal_timezone(tenant, device_tz: str) -> None:
    """Adopt the device's IANA zone when the user still has the unset
    'UTC' default — iOS is the only channel that knows ground truth, and
    the auto-complete matcher buckets days by tenant tz, so an evening
    workout under a wrong UTC default would land on tomorrow's date and
    never match (the known 17/27-users-on-UTC latent bug)."""
    if not device_tz or device_tz == "UTC":
        return
    user = getattr(tenant, "user", None)
    if user is None or user.timezone != "UTC":
        return
    try:
        ZoneInfo(device_tz)
    except Exception:
        return
    user.timezone = device_tz
    user.save(update_fields=["timezone"])
    logger.info("Self-healed timezone for tenant %s from device_tz=%s", str(tenant.id)[:8], device_tz)


def ingest_healthkit_payload(tenant, payload: dict) -> dict:
    """Process one sync request. The view enforces caps/auth/gates and the
    signal-suppression contexts; this function owns the data work."""
    _maybe_self_heal_timezone(tenant, str(payload.get("device_tz") or "").strip())
    tz = tenant_tz(tenant)

    profile = FuelProfile.objects.filter(tenant=tenant).first()
    tombstones = set(profile.healthkit_tombstones or []) if profile else set()

    # Deletions first: per-instance deletes so post_delete receivers fire
    # (tombstone capture + fuel_version). Bounded by MAX_DELETED.
    # source=HEALTHKIT is deliberate asymmetry vs the tombstone receiver:
    # deleting the HK sample must not delete a matched planned session the
    # user/assistant authored — the row survives, only standalone imports go.
    deleted_count = 0
    deleted_ids = [str(x).strip()[:64] for x in (payload.get("deleted_external_ids") or []) if str(x).strip()]
    if deleted_ids:
        for row in Workout.objects.filter(tenant=tenant, source=WorkoutSource.HEALTHKIT, external_id__in=deleted_ids):
            row.delete()
            deleted_count += 1
        tombstones.update(deleted_ids)

    raw_workouts = payload.get("workouts") or []
    cleans: list[tuple[dict | None, str | None, object]] = []
    for item in raw_workouts:
        clean, err = _clean_workout(item)
        cleans.append((clean, err, item))

    existing = set(
        Workout.objects.filter(
            tenant=tenant,
            external_id__in=[c["external_id"] for c, e, _ in cleans if c],
        ).values_list("external_id", flat=True)
    )

    results = []
    consumed: set = set()
    counts = {"created": 0, "matched_planned": 0, "matched_log": 0, "duplicates": 0, "errors": 0}
    for clean, err, item in cleans:
        if err:
            raw_id = item.get("external_id") if isinstance(item, dict) else None
            results.append({"external_id": raw_id, "status": "error", "error": err})
            counts["errors"] += 1
            continue
        eid = clean["external_id"]
        if eid in tombstones or eid in existing:
            results.append({"external_id": eid, "status": "duplicate"})
            counts["duplicates"] += 1
            continue
        res = _ingest_workout(tenant, clean, tz, consumed)
        results.append(res)
        if res["status"] == "matched_planned":
            consumed.add(res["workout_id"])
            counts["matched_planned"] += 1
        elif res["status"] == "matched_log":
            # Adopted an existing manual log — reserve it so a second HK
            # sample in this batch can't adopt the same row.
            consumed.add(res["workout_id"])
            counts["matched_log"] += 1
        elif res["status"] == "created":
            counts["created"] += 1
        elif res["status"] == "duplicate":
            counts["duplicates"] += 1
        else:
            counts["errors"] += 1
        if res["status"] in ("created", "matched_planned", "matched_log", "duplicate"):
            existing.add(eid)

    daily_results = [_ingest_daily(tenant, item, tz) for item in (payload.get("daily_metrics") or [])]
    daily_upserted = sum(1 for r in daily_results if r["status"] == "upserted")

    return {
        "results": results,
        "daily_results": daily_results,
        "summary": {
            **counts,
            "deleted": deleted_count,
            "daily_upserted": daily_upserted,
            "daily_errors": len(daily_results) - daily_upserted,
        },
        "wrote_any": bool(
            counts["created"] or counts["matched_planned"] or counts["matched_log"] or deleted_count or daily_upserted
        ),
        "regen_needed": bool(counts["matched_planned"] and profile is not None and profile.use_session_scheduling),
    }


def push_visibility_refresh(tenant_id: str) -> None:
    """One post-batch USER.md push, off the request thread in prod.

    debounce_seconds=0 is load-bearing — see module docstring."""

    def _run() -> None:
        try:
            from apps.orchestrator.workspace_envelope import push_user_md

            push_user_md(tenant_id, debounce_seconds=0)
        except Exception:
            logger.warning("Post-sync USER.md push failed for tenant %s", str(tenant_id)[:8], exc_info=True)

    from django.conf import settings

    if getattr(settings, "NBHD_DISABLE_BACKGROUND_THREADS", False):
        _run()
        return
    threading.Thread(target=_run, daemon=True).start()
