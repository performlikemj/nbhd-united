"""Closed-list lookups + unit conversions used by LLM tool runtime views.

These are the *deterministic* operations the LLM used to do in-prompt.
The LLM still proposes a classification or unit, but the server normalises
to the canonical answer before persistence so a mis-classification can't
turn a plank into "30 reps at 0 kg".

Add entries here rather than teaching the LLM more exercise names — the
list is the source of truth.
"""

from __future__ import annotations

# Metric type values match the discriminated `Set.type` enum landing in
# apps.fuel.serializers in Phase 3. They are referenced as plain strings
# here to keep apps.common dependency-free of apps.fuel.
METRIC_WEIGHTED_REPS = "weighted_reps"
METRIC_BODYWEIGHT_REPS = "bodyweight_reps"
METRIC_HOLD_TIME = "hold_time"
METRIC_DISTANCE_TIME = "distance_time"
METRIC_BLOCKS = "blocks"  # mobility — no per-set metric

# Lowercase canonical name → (workout category, default per-set metric).
# Order doesn't matter for exact lookup; longer keys win in substring fallback.
_EXERCISE_REGISTRY: dict[str, tuple[str, str]] = {
    # ── Strength — weighted reps ───────────────────────────────────────
    "bench press": ("strength", METRIC_WEIGHTED_REPS),
    "incline bench": ("strength", METRIC_WEIGHTED_REPS),
    "incline press": ("strength", METRIC_WEIGHTED_REPS),
    "decline bench": ("strength", METRIC_WEIGHTED_REPS),
    "dumbbell bench": ("strength", METRIC_WEIGHTED_REPS),
    "deadlift": ("strength", METRIC_WEIGHTED_REPS),
    "romanian deadlift": ("strength", METRIC_WEIGHTED_REPS),
    "rdl": ("strength", METRIC_WEIGHTED_REPS),
    "sumo deadlift": ("strength", METRIC_WEIGHTED_REPS),
    "squat": ("strength", METRIC_WEIGHTED_REPS),
    "back squat": ("strength", METRIC_WEIGHTED_REPS),
    "front squat": ("strength", METRIC_WEIGHTED_REPS),
    "goblet squat": ("strength", METRIC_WEIGHTED_REPS),
    "overhead press": ("strength", METRIC_WEIGHTED_REPS),
    "ohp": ("strength", METRIC_WEIGHTED_REPS),
    "military press": ("strength", METRIC_WEIGHTED_REPS),
    "barbell row": ("strength", METRIC_WEIGHTED_REPS),
    "bent over row": ("strength", METRIC_WEIGHTED_REPS),
    "barbell bent over row": ("strength", METRIC_WEIGHTED_REPS),
    "pendlay row": ("strength", METRIC_WEIGHTED_REPS),
    "t-bar row": ("strength", METRIC_WEIGHTED_REPS),
    "dumbbell row": ("strength", METRIC_WEIGHTED_REPS),
    "seal row": ("strength", METRIC_WEIGHTED_REPS),
    "barbell curl": ("strength", METRIC_WEIGHTED_REPS),
    "dumbbell curl": ("strength", METRIC_WEIGHTED_REPS),
    "hammer curl": ("strength", METRIC_WEIGHTED_REPS),
    "preacher curl": ("strength", METRIC_WEIGHTED_REPS),
    "face pull": ("strength", METRIC_WEIGHTED_REPS),
    "lateral raise": ("strength", METRIC_WEIGHTED_REPS),
    "front raise": ("strength", METRIC_WEIGHTED_REPS),
    "rear delt fly": ("strength", METRIC_WEIGHTED_REPS),
    "tricep extension": ("strength", METRIC_WEIGHTED_REPS),
    "skull crusher": ("strength", METRIC_WEIGHTED_REPS),
    "tricep pushdown": ("strength", METRIC_WEIGHTED_REPS),
    "shrug": ("strength", METRIC_WEIGHTED_REPS),
    "hip thrust": ("strength", METRIC_WEIGHTED_REPS),
    "leg press": ("strength", METRIC_WEIGHTED_REPS),
    "leg curl": ("strength", METRIC_WEIGHTED_REPS),
    "leg extension": ("strength", METRIC_WEIGHTED_REPS),
    "calf raise": ("strength", METRIC_WEIGHTED_REPS),
    "lat pulldown": ("strength", METRIC_WEIGHTED_REPS),
    "pulldown": ("strength", METRIC_WEIGHTED_REPS),
    "cable row": ("strength", METRIC_WEIGHTED_REPS),
    "good morning": ("strength", METRIC_WEIGHTED_REPS),
    "clean": ("strength", METRIC_WEIGHTED_REPS),
    "power clean": ("strength", METRIC_WEIGHTED_REPS),
    "snatch": ("strength", METRIC_WEIGHTED_REPS),
    "thruster": ("strength", METRIC_WEIGHTED_REPS),
    "lunge": ("strength", METRIC_WEIGHTED_REPS),
    "split squat": ("strength", METRIC_WEIGHTED_REPS),
    "bulgarian split squat": ("strength", METRIC_WEIGHTED_REPS),
    "step up": ("strength", METRIC_WEIGHTED_REPS),
    # ── Calisthenics — bodyweight reps ─────────────────────────────────
    "pull-up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "pull up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "pullup": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "chin-up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "chin up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "chinup": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "push-up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "push up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "pushup": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "dip": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "muscle-up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "muscle up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "sit-up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "sit up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "crunch": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "burpee": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "jumping jack": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "mountain climber": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "pistol squat": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "air squat": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "box jump": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "broad jump": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "v-up": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "leg raise": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "toes to bar": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "knees to chest": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    "inverted row": ("calisthenics", METRIC_BODYWEIGHT_REPS),
    # ── Calisthenics — hold time (isometric) ───────────────────────────
    "plank": ("calisthenics", METRIC_HOLD_TIME),
    "side plank": ("calisthenics", METRIC_HOLD_TIME),
    "forearm plank": ("calisthenics", METRIC_HOLD_TIME),
    "high plank": ("calisthenics", METRIC_HOLD_TIME),
    "wall sit": ("calisthenics", METRIC_HOLD_TIME),
    "hollow hold": ("calisthenics", METRIC_HOLD_TIME),
    "superman hold": ("calisthenics", METRIC_HOLD_TIME),
    "l-sit": ("calisthenics", METRIC_HOLD_TIME),
    "l sit": ("calisthenics", METRIC_HOLD_TIME),
    "handstand hold": ("calisthenics", METRIC_HOLD_TIME),
    "headstand": ("calisthenics", METRIC_HOLD_TIME),
    "dead hang": ("calisthenics", METRIC_HOLD_TIME),
    "hang": ("calisthenics", METRIC_HOLD_TIME),
    "front lever": ("calisthenics", METRIC_HOLD_TIME),
    "back lever": ("calisthenics", METRIC_HOLD_TIME),
    "human flag": ("calisthenics", METRIC_HOLD_TIME),
    "bridge hold": ("calisthenics", METRIC_HOLD_TIME),
    # ── Cardio — distance + time ───────────────────────────────────────
    "running": ("cardio", METRIC_DISTANCE_TIME),
    "run": ("cardio", METRIC_DISTANCE_TIME),
    "jog": ("cardio", METRIC_DISTANCE_TIME),
    "jogging": ("cardio", METRIC_DISTANCE_TIME),
    "sprint": ("cardio", METRIC_DISTANCE_TIME),
    "walk": ("cardio", METRIC_DISTANCE_TIME),
    "hike": ("cardio", METRIC_DISTANCE_TIME),
    "ruck": ("cardio", METRIC_DISTANCE_TIME),
    "cycling": ("cardio", METRIC_DISTANCE_TIME),
    "cycle": ("cardio", METRIC_DISTANCE_TIME),
    "bike": ("cardio", METRIC_DISTANCE_TIME),
    "biking": ("cardio", METRIC_DISTANCE_TIME),
    "swimming": ("cardio", METRIC_DISTANCE_TIME),
    "swim": ("cardio", METRIC_DISTANCE_TIME),
    "row": ("cardio", METRIC_DISTANCE_TIME),
    "rowing": ("cardio", METRIC_DISTANCE_TIME),
    "erg": ("cardio", METRIC_DISTANCE_TIME),
    "ski erg": ("cardio", METRIC_DISTANCE_TIME),
    "elliptical": ("cardio", METRIC_DISTANCE_TIME),
    # ── Mobility — block-based ─────────────────────────────────────────
    "yoga": ("mobility", METRIC_BLOCKS),
    "stretching": ("mobility", METRIC_BLOCKS),
    "stretch": ("mobility", METRIC_BLOCKS),
    "foam roll": ("mobility", METRIC_BLOCKS),
    "mobility": ("mobility", METRIC_BLOCKS),
}


def normalize_exercise(name: str) -> tuple[str, str] | None:
    """Resolve a free-text exercise name to (category, default_metric).

    Resolution order:
      1. ``weighted <X>`` prefix promotes a bodyweight movement to
         ``strength`` + ``weighted_reps`` (so "weighted pull-ups" stores
         weight and reps, not pure bodyweight reps).
      2. Exact match against the canonical name.
      3. Longest substring match — "side plank with rotation" still
         resolves to "side plank".

    Returns ``None`` when nothing matches; callers should fall back to
    whatever the LLM proposed and surface a warning so the registry can
    be extended.
    """
    if not name:
        return None
    p = name.strip().lower()
    if not p:
        return None

    # "weighted <bodyweight movement>" → strength + weighted_reps
    if p.startswith("weighted "):
        rest = p[len("weighted ") :].strip()
        if rest in _EXERCISE_REGISTRY:
            _, _metric = _EXERCISE_REGISTRY[rest]
            return ("strength", METRIC_WEIGHTED_REPS)
        # Longest substring among known names — still upgrades to weighted_reps.
        match = _longest_substring_match(rest)
        if match is not None:
            return ("strength", METRIC_WEIGHTED_REPS)

    if p in _EXERCISE_REGISTRY:
        return _EXERCISE_REGISTRY[p]

    match = _longest_substring_match(p)
    return _EXERCISE_REGISTRY[match] if match else None


def _longest_substring_match(haystack: str) -> str | None:
    """Return the longest canonical name that appears in ``haystack``."""
    best: str | None = None
    for canonical in _EXERCISE_REGISTRY:
        if canonical in haystack and (best is None or len(canonical) > len(best)):
            best = canonical
    return best


# ── Unit conversions — exact constants, never let the LLM guess. ──────

_LBS_PER_KG = 2.20462262185
_KM_PER_MI = 1.609344


def kg_to_lbs(kg: float) -> float:
    return kg * _LBS_PER_KG


def lbs_to_kg(lbs: float) -> float:
    return lbs / _LBS_PER_KG


def km_to_mi(km: float) -> float:
    return km / _KM_PER_MI


def mi_to_km(mi: float) -> float:
    return mi * _KM_PER_MI


def convert_weight(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a weight value between ``kg`` and ``lbs``."""
    f = from_unit.strip().lower()
    t = to_unit.strip().lower()
    if f == t:
        return value
    if f == "kg" and t == "lbs":
        return kg_to_lbs(value)
    if f == "lbs" and t == "kg":
        return lbs_to_kg(value)
    raise ValueError(f"convert_weight: unsupported unit pair {from_unit!r} → {to_unit!r}")


def convert_distance(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a distance value between ``km`` and ``mi``."""
    f = from_unit.strip().lower()
    t = to_unit.strip().lower()
    if f == t:
        return value
    if f == "km" and t == "mi":
        return km_to_mi(value)
    if f == "mi" and t == "km":
        return mi_to_km(value)
    raise ValueError(f"convert_distance: unsupported unit pair {from_unit!r} → {to_unit!r}")
