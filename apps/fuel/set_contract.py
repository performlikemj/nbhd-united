"""Shape-agnostic accessors for workout set metrics.

Bridges the **legacy flat** set shape (``{reps?, weight?, hold_s?}`` — no
discriminator) and the **typed** discriminated shape (``{type, ...}``)
landing in Phase 2, so every consumer reads a set's metric the same way
regardless of when the row was written.

Inference order (most authoritative first):

  1. An explicit, *valid* ``type`` on the set.
  2. Field presence — ``hold_s`` ⇒ hold; positive ``weight`` ⇒ weighted;
     otherwise bodyweight. This reproduces the historical null-sniff
     exactly, so routing legacy data through here is behaviour-neutral.
  3. The exercise registry (only when 1 and 2 are inconclusive and a
     name is supplied) — e.g. an empty-field "plank" set ⇒ hold_time.

Pure functions, no Django imports — unit-testable in isolation and safe
to import from anywhere in ``apps.fuel``. The metric vocabulary is owned
by ``apps.common.llm_lookups``; this module never invents new values.
"""

from __future__ import annotations

from typing import Any

from apps.common.llm_lookups import (
    METRIC_BODYWEIGHT_REPS,
    METRIC_HOLD_TIME,
    METRIC_WEIGHTED_REPS,
    normalize_exercise,
)

__all__ = [
    "METRIC_BODYWEIGHT_REPS",
    "METRIC_HOLD_TIME",
    "METRIC_WEIGHTED_REPS",
    "SET_METRICS",
    "set_metric",
    "coerce_set",
]

# The three metrics a *set* can carry. (``distance_time`` / ``blocks``
# describe whole cardio/mobility workouts, not per-set data, and are
# intentionally out of scope here — see CONTINUITY_fuel-set-contract.md.)
SET_METRICS = frozenset(
    {METRIC_WEIGHTED_REPS, METRIC_BODYWEIGHT_REPS, METRIC_HOLD_TIME}
)


def _positive_weight(value: Any) -> bool:
    """True only for a strictly-positive numeric weight.

    ``weight: 0`` means bodyweight (per the tool schema's own guidance),
    so it must NOT classify as weighted.
    """
    if value is None:
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def set_metric(s: Any, *, exercise_name: str | None = None) -> str:
    """Return the canonical metric for a single set dict.

    Always returns one of :data:`SET_METRICS`; never raises. A non-dict
    input degrades to ``bodyweight_reps`` (the safest, lowest-information
    default) rather than blowing up a render or aggregate path.
    """
    if not isinstance(s, dict):
        return METRIC_BODYWEIGHT_REPS

    # 1. Explicit, valid type wins outright.
    declared = s.get("type")
    if declared in SET_METRICS:
        return declared

    # 2. Field presence — reproduces the historical inference exactly.
    if s.get("hold_s") is not None:
        return METRIC_HOLD_TIME
    if _positive_weight(s.get("weight")):
        return METRIC_WEIGHTED_REPS

    # 3. Registry refine — only when fields are inconclusive and we have a
    #    name (e.g. a bare "plank" set with neither hold_s nor weight).
    if exercise_name:
        norm = normalize_exercise(exercise_name)
        if norm and norm[1] in SET_METRICS:
            return norm[1]

    return METRIC_BODYWEIGHT_REPS


def coerce_set(raw: Any, *, exercise_name: str | None = None) -> dict[str, Any]:
    """Return a shallow copy of ``raw`` with a valid ``type`` stamped.

    Idempotent: a set that already has a valid ``type`` is returned with
    that type preserved. A non-dict yields a minimal bodyweight set so
    callers (the Phase 4 migration, the Phase 2 coercer) always get a
    well-formed dict back.
    """
    out: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    out["type"] = set_metric(out, exercise_name=exercise_name)
    return out
