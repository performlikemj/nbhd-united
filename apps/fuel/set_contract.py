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

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    "normalize_detail",
    "validate_detail",
]

# The three metrics a *set* can carry. (``distance_time`` / ``blocks``
# describe whole cardio/mobility workouts, not per-set data, and are
# intentionally out of scope here — see CONTINUITY_fuel-set-contract.md.)
SET_METRICS = frozenset({METRIC_WEIGHTED_REPS, METRIC_BODYWEIGHT_REPS, METRIC_HOLD_TIME})


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


def _normalized_sets(sets: Any, *, exercise_name: str, reg_metric: str) -> tuple[list, list[dict]]:
    """Stamp every set's ``type`` to the registry metric for a known
    exercise. Returns ``(new_sets, override_notes)``; only an actual
    change in effective metric is recorded as a note."""
    new_sets: list = []
    notes: list[dict] = []
    for s in sets:
        if not isinstance(s, dict):
            new_sets.append(s)
            continue
        prev = set_metric(s, exercise_name=exercise_name)
        desired = reg_metric if reg_metric in SET_METRICS else prev
        new_sets.append({**s, "type": desired})
        if desired != prev:
            notes.append({"exercise": exercise_name, "field": "set.type", "from": prev, "to": desired})
    return new_sets, notes


def normalize_detail(detail: Any, category: str, *, activity: str | None = None) -> tuple[Any, str, list[dict]]:
    """Deterministically correct set ``type`` and (only between
    ``strength`` and ``calisthenics``) the workout ``category`` from the
    exercise registry, *before* the LLM's guess is persisted.

    Returns ``(new_detail, new_category, overrides)``. Pure — never
    raises, never mutates the input (rebuilds dicts/lists). Only
    *registry-known* exercises are touched; unknowns are left untouched
    for the Phase 2 coercer/validator. Corrections are also recorded
    under ``new_detail["_normalized"]`` for debugging and the Phase 5 UI.
    """
    if not isinstance(detail, dict):
        return detail, category, []

    new = dict(detail)
    overrides: list[dict] = []
    reg_cats: list[str] = []

    for key in ("exercises", "skills"):
        container = new.get(key)
        if not isinstance(container, list):
            continue
        rebuilt: list = []
        for ex in container:
            if not isinstance(ex, dict):
                rebuilt.append(ex)
                continue
            name = str(ex.get("name") or "").strip() or str(activity or "").strip()
            norm = normalize_exercise(name) if name else None
            if not norm:
                rebuilt.append(ex)
                continue
            reg_cat, reg_metric = norm
            reg_cats.append(reg_cat)
            sets = ex.get("sets")
            if isinstance(sets, list):
                new_sets, notes = _normalized_sets(sets, exercise_name=name, reg_metric=reg_metric)
                rebuilt.append({**ex, "sets": new_sets})
                overrides.extend(notes)
            else:
                rebuilt.append(ex)
        new[key] = rebuilt

    if (
        reg_cats
        and all(c == reg_cats[0] for c in reg_cats)
        and reg_cats[0] in ("strength", "calisthenics")
        and category != reg_cats[0]
        and category in ("strength", "calisthenics", "other", "")
    ):
        overrides.append({"field": "category", "from": category, "to": reg_cats[0]})
        category = reg_cats[0]

    if overrides:
        new["_normalized"] = overrides
    return new, category, overrides


# ── Typed set contract (Phase 2 / #593) ───────────────────────────────


class _SetModel(BaseModel):
    """Base for typed shapes — tolerate extra keys (``est_1rm``, ``pr``,
    …) that consumers stamp onto stored sets."""

    model_config = ConfigDict(extra="ignore")


class WeightedRepsSet(_SetModel):
    # Constants (not string literals) so the lint-autofix can't strip the
    # quotes off `Literal["..."]`; resolves to the same canonical value.
    type: Literal[METRIC_WEIGHTED_REPS]
    reps: int = Field(ge=0)
    weight: float = Field(ge=0)


class BodyweightRepsSet(_SetModel):
    type: Literal[METRIC_BODYWEIGHT_REPS]
    reps: int = Field(ge=0)


class HoldTimeSet(_SetModel):
    type: Literal[METRIC_HOLD_TIME]
    hold_s: int = Field(ge=0)


TypedSet = Annotated[
    WeightedRepsSet | BodyweightRepsSet | HoldTimeSet,
    Field(discriminator="type"),
]


class _Exercise(_SetModel):
    name: str = ""
    sets: list[TypedSet] = Field(default_factory=list)


class _WorkoutDetail(_SetModel):
    exercises: list[_Exercise] = Field(default_factory=list)
    skills: list[_Exercise] = Field(default_factory=list)


# `from __future__ import annotations` defers schema construction for the
# discriminated union + nested models — rebuild them explicitly so
# validation works at import time.
WeightedRepsSet.model_rebuild()
BodyweightRepsSet.model_rebuild()
HoldTimeSet.model_rebuild()
_Exercise.model_rebuild()
_WorkoutDetail.model_rebuild()


def _coerce_container(detail: dict) -> dict:
    """Copy ``detail`` with every set in exercises/skills given a valid
    ``type`` (all other keys + extras preserved). Does not validate."""
    out = dict(detail)
    for key in ("exercises", "skills"):
        container = out.get(key)
        if not isinstance(container, list):
            continue
        rebuilt: list = []
        for ex in container:
            if isinstance(ex, dict) and isinstance(ex.get("sets"), list):
                name = str(ex.get("name") or "").strip()
                rebuilt.append({**ex, "sets": [coerce_set(s, exercise_name=name) for s in ex["sets"]]})
            else:
                rebuilt.append(ex)
        out[key] = rebuilt
    return out


def validate_detail(detail: Any, category: str) -> tuple[Any, Any]:
    """Coerce every set to a typed shape, then enforce the discriminated
    contract. Returns ``(coerced_detail, error_or_None)``; the error is
    an ``LLMValidationError`` the caller surfaces so the LLM
    self-corrects. Only strength/calisthenics are validated — cardio /
    HIIT / mobility keep their flat by-category shape untouched. The
    coerced detail preserves every original key (extras, ``_normalized``,
    cardio fields), so it is always safe to persist.
    """
    if not isinstance(detail, dict) or category not in ("strength", "calisthenics"):
        return detail, None

    coerced = _coerce_container(detail)

    # Local import keeps this module free of an import-time Django
    # dependency (llm_contracts pulls django.utils.timezone) and is
    # used immediately, so the lint-autofix can't reap it.
    from apps.common.llm_contracts import LLMValidationError

    try:
        _WorkoutDetail.model_validate(coerced)
    except ValidationError as exc:
        return coerced, LLMValidationError.from_pydantic(exc)
    return coerced, None
