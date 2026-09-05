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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from apps.common.llm_lookups import (
    CARDIO_EFFORTS,
    CARDIO_INTERVAL_KIND,
    CARDIO_LIMITS,
    CARDIO_PACE_REGEX,
    CARDIO_RECOVERY_EFFORTS,
    CARDIO_STEADY_KINDS,
    CARDIO_TERRAINS,
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
    "has_prescription",
    "normalize_detail",
    "validate_cardio_prescription",
    "expand_cardio_reps",
    "derive_planned",
    "validate_detail",
    "validate_flat_detail",
    "FLAT_NUMERIC_FIELDS",
    "split_detail_errors",
]

# The three metrics a *set* can carry. (``distance_time`` / ``blocks``
# describe whole cardio/mobility workouts, not per-set data, and are
# intentionally out of scope here — see CONTINUITY_fuel-set-contract.md.)
SET_METRICS = frozenset({METRIC_WEIGHTED_REPS, METRIC_BODYWEIGHT_REPS, METRIC_HOLD_TIME})


class _CardioDose(BaseModel):
    # Extension fields survive on the original dict; strict numbers reject bools/strings.
    model_config = ConfigDict(extra="allow", strict=True, allow_inf_nan=False)
    duration_s: int | None = Field(default=None, ge=CARDIO_LIMITS["duration_s_min"], le=CARDIO_LIMITS["duration_s_max"])
    distance_km: float | None = Field(
        default=None, ge=CARDIO_LIMITS["distance_km_min"], le=CARDIO_LIMITS["distance_km_max"]
    )

    @model_validator(mode="after")
    def one_dose(self):
        supplied = self.model_fields_set & {"duration_s", "distance_km"}
        if len(supplied) != 1 or getattr(self, next(iter(supplied))) is None:
            raise ValueError("exactly one dose is required: duration_s or distance_km")
        return self


class _CardioRecovery(_CardioDose):
    duration_s: int | None = Field(
        default=None, ge=CARDIO_LIMITS["recovery_duration_s_min"], le=CARDIO_LIMITS["recovery_duration_s_max"]
    )
    distance_km: float | None = Field(
        default=None, ge=CARDIO_LIMITS["recovery_distance_km_min"], le=CARDIO_LIMITS["recovery_distance_km_max"]
    )
    effort: Literal[*CARDIO_RECOVERY_EFFORTS]


class _CardioWork(_CardioDose):
    effort: Literal[*CARDIO_EFFORTS]
    target_pace: str | None = Field(default=None, pattern=CARDIO_PACE_REGEX)

    @model_validator(mode="after")
    def pace_if_supplied(self):
        if "target_pace" in self.model_fields_set and self.target_pace is None:
            raise ValueError("target_pace must be M:SS per km when supplied")
        return self


class _CardioSteady(_CardioWork):
    kind: Literal[*CARDIO_STEADY_KINDS]

    @model_validator(mode="after")
    def no_interval_fields(self):
        if {"repeat", "recovery"} & self.model_fields_set:
            raise ValueError("repeat and recovery are only valid on interval blocks")
        return self


class _CardioInterval(_CardioWork):
    kind: Literal[CARDIO_INTERVAL_KIND]
    repeat: int = Field(ge=CARDIO_LIMITS["repeat_min"], le=CARDIO_LIMITS["repeat_max"])
    recovery: _CardioRecovery | None = None

    @model_validator(mode="after")
    def between_reps(self):
        if "recovery" in self.model_fields_set and (self.recovery is None or self.repeat == 1):
            raise ValueError("recovery requires at least two work reps and a recovery dose")
        return self


_CardioBlock = Annotated[_CardioSteady | _CardioInterval, Field(discriminator="kind")]


class _CardioPrescription(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    segments: list[_CardioBlock] = Field(min_length=1, max_length=CARDIO_LIMITS["blocks_max"])
    terrain: Literal[*CARDIO_TERRAINS] | None = None

    @model_validator(mode="after")
    def expansion_limit(self):
        if (
            sum(block.repeat if isinstance(block, _CardioInterval) else 1 for block in self.segments)
            > CARDIO_LIMITS["expanded_reps_max"]
        ):
            raise ValueError("segments exceed 200 expanded work reps")
        return self


def _cardio_errors(detail: Any, category: str = "cardio") -> list[dict]:
    if not isinstance(detail, dict):
        return []
    if "segments" in detail and category != "cardio":
        return [
            {
                "loc": ["segments"],
                "msg": "segments are only valid for cardio category",
                "type": "cardio_category_invalid",
            }
        ]
    errors = []
    if (
        category == "cardio"
        and "segments" in detail
        and "terrain" in detail
        and detail["terrain"] not in CARDIO_TERRAINS
    ):
        errors.append(
            {
                "loc": ["terrain"],
                "msg": "terrain must be one of: " + ", ".join(CARDIO_TERRAINS),
                "type": "cardio_detail_invalid",
            }
        )
    if "segments" in detail:
        try:
            _CardioPrescription.model_validate(detail)
        except ValidationError as exc:
            for err in exc.errors(include_url=False, include_context=False, include_input=False):
                if not err["loc"]:
                    err["loc"] = ("segments",)
                errors.append(err)
    return errors


def validate_cardio_prescription(detail: Any, category: str = "cardio") -> list[str]:
    """Validate machine fields without coercing or rewriting the supplied detail."""
    return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in _cardio_errors(detail, category)]


def _cardio_error_envelope(detail: Any, category: str):
    from apps.common.llm_contracts import LLMValidationError

    errors = _cardio_errors(detail, category)
    if errors:
        return LLMValidationError(message="Invalid cardio prescription.", details=errors)
    return None


def expand_cardio_reps(segments) -> int:
    return sum(block.get("repeat", 1) if block["kind"] == CARDIO_INTERVAL_KIND else 1 for block in segments)


def derive_planned(segments, explicit_duration_minutes=None) -> dict:
    """Derive homogeneous totals, counting recovery only between work reps.

    Invalid legacy prescriptions have no derived totals; validation reports errors
    separately so unrelated edits can still grandfather stored invalid fragments.
    """
    if validate_cardio_prescription({"segments": segments}):
        return {}
    doses = []
    for block in segments:
        repeat = block.get("repeat", 1) if block["kind"] == CARDIO_INTERVAL_KIND else 1
        doses.append((block, repeat))
        if block.get("recovery"):
            doses.append((block["recovery"], repeat - 1))
    planned = {}
    for key in ("duration_s", "distance_km"):
        if all(key in dose for dose, _ in doses):
            planned[key] = round(sum(dose[key] * count for dose, count in doses), 8)
    if explicit_duration_minutes is not None:
        planned["duration_s"] = explicit_duration_minutes * 60
    return planned


def has_prescription(detail: Any, category: str, *, duration_minutes: Any = None) -> bool:
    """Return whether a planned workout has usable category-specific content.

    Categories outside the prescribed workout set intentionally pass: sport,
    rest-like values, and future categories do not have a detail contract here.
    ``duration_minutes`` is a valid standalone cardio prescription when it was
    supplied, including zero; callers own any numeric coercion or range rules.
    """
    if category not in ("strength", "calisthenics", "cardio", "hiit", "mobility"):
        return True
    if not isinstance(detail, dict):
        return False

    def _non_empty_list(key: str) -> bool:
        value = detail.get(key)
        return isinstance(value, list) and bool(value)

    if category in ("strength", "calisthenics"):
        return _non_empty_list("exercises") or _non_empty_list("skills")
    if category == "cardio":
        return (
            _non_empty_list("segments")
            or duration_minutes is not None
            or any(detail.get(key) for key in ("distance_km", "pace", "structure", "avg_hr", "elevation", "avg_power"))
        )
    if category == "hiit":
        return (
            bool(detail.get("rounds") and detail.get("work_s"))
            or bool(detail.get("structure"))
            or any(_non_empty_list(key) for key in ("exercises", "skills"))
        )
    return any(_non_empty_list(key) for key in ("blocks", "skills", "exercises"))


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


def normalize_detail(
    detail: Any, category: str, *, activity: str | None = None, explicit_duration_minutes=None
) -> tuple[Any, str, list[dict]]:
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
    if category == "cardio" and "segments" in new:
        new["planned"] = derive_planned(new["segments"], explicit_duration_minutes)
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

    key_move = None
    if category == "calisthenics":
        key_move = ("exercises", "skills")
    elif category == "strength":
        key_move = ("skills", "exercises")
    if key_move is not None:
        source_key, target_key = key_move
        source = new.get(source_key)
        target = new.get(target_key)
        if isinstance(source, list) and source and not (isinstance(target, list) and target):
            new[target_key] = source
            new.pop(source_key, None)
            overrides.append({"field": "exercise_key", "from": source_key, "to": target_key})

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
    role: Literal["primary", "accessory", "warmup", "mobility"] | None = None


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
    self-corrects. Cardio segments and terrain are checked before the
    strength/calisthenics set contract; legacy flat fields stay untouched. The
    coerced detail preserves every original key (extras, ``_normalized``,
    cardio fields), so it is always safe to persist.
    """
    if not isinstance(detail, dict):
        return detail, None

    # Local import keeps this module free of an import-time Django
    # dependency (llm_contracts pulls django.utils.timezone) and is
    # used immediately, so the lint-autofix can't reap it.
    from apps.common.llm_contracts import LLMValidationError

    errors = _cardio_errors(detail, category)
    cardio_error_count = len(errors)
    allowed_roles = {"primary", "accessory", "warmup", "mobility"}
    for container in ("exercises", "skills"):
        items = detail.get(container)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if isinstance(item, dict) and "role" in item and item.get("role") not in allowed_roles:
                errors.append(
                    {
                        "loc": [container, index, "role"],
                        "msg": "role must be one of: primary, accessory, warmup, mobility",
                        "type": "invalid_role",
                        "allowed": sorted(allowed_roles),
                    }
                )
    if not cardio_error_count and errors:
        return detail, LLMValidationError(
            message="Exercise roles must use the documented plan-programming vocabulary.", details=[errors[0]]
        )
    coerced = detail
    if category in ("strength", "calisthenics"):
        coerced = _coerce_container(detail)
        try:
            _WorkoutDetail.model_validate(coerced)
        except ValidationError as exc:
            set_error = LLMValidationError.from_pydantic(exc)
            if not errors:
                return coerced, set_error
            errors.extend(set_error.details)
    if errors:
        return coerced, LLMValidationError(message="Workout detail validation failed.", details=errors)
    return coerced, None


# ── Flat (cardio / HIIT / mobility) detail contract ───────────────────

# Fields in the FLAT by-category detail shape that MUST hold numbers. The read
# side treats them as such — ``aggregate_cardio_progress`` calls a bare
# ``float()`` on ``distance_km`` — so a value like "5 miles" is not a display
# nit: it raises on every load of that user's cardio Progress view, forever,
# because nothing downstream can recover from it. Validate at the faucet.
FLAT_NUMERIC_FIELDS = (
    "distance_km",
    "avg_hr",
    "peak_hr",
    "calories",
    "elevation",
    "rounds",
    "work_s",
    "rest_s",
    "avg_power",
)

# Categories carrying the flat shape. strength/calisthenics go through the
# discriminated set contract above instead.
FLAT_DETAIL_CATEGORIES = ("cardio", "hiit", "mobility")


def _coerce_number(value: Any) -> tuple[Any, bool]:
    """Return ``(number, ok)``.

    Accepts ints/floats and strings that parse cleanly as either. Rejects
    bools (``float(True)`` is 1.0 — a true distance is nonsense), containers,
    and text carrying units. Integral strings stay ints so a round count does
    not become 8.0.
    """
    if isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float)):
        return value, True
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    if not text:
        return None, False
    try:
        return int(text), True
    except ValueError:
        pass
    try:
        return float(text), True
    except ValueError:
        return None, False


def validate_flat_detail(detail: Any, category: str) -> tuple[Any, Any]:
    """Enforce numeric types on a cardio / HIIT / mobility ``detail_json``.

    Returns ``(detail, error_or_None)``. A numeric string is coerced in place
    (the caller persists the returned dict); anything that cannot parse as a
    number yields an ``LLMValidationError`` naming the field, so the assistant
    resends "5" instead of "5 miles" in the same loop. Absent and ``None``
    fields are left alone — silence means "not measured", which every consumer
    already handles.

    Pure: never raises, never mutates the input.
    """
    cardio_error = _cardio_error_envelope(detail, category)
    if cardio_error is not None:
        return detail, cardio_error
    if not isinstance(detail, dict) or category not in FLAT_DETAIL_CATEGORIES:
        return detail, None

    out: dict | None = None
    bad: list[dict] = []
    for field in FLAT_NUMERIC_FIELDS:
        if field not in detail:
            continue
        raw = detail[field]
        if raw is None:
            continue
        num, ok = _coerce_number(raw)
        if not ok:
            bad.append(
                {
                    "loc": ["detail_json", field],
                    "msg": f"{field} must be a number (e.g. 5 or 5.2) — no units, no words",
                    "type": "cardio_detail_invalid",
                }
            )
            continue
        if isinstance(raw, str):
            if out is None:
                out = dict(detail)
            out[field] = num

    if bad:
        from apps.common.llm_contracts import LLMValidationError

        fields = ", ".join(str(err["loc"][-1]) for err in bad)
        return detail, LLMValidationError(
            message=(
                f"These detail_json fields must be plain numbers: {fields}. "
                "Send the value only — the unit is fixed by the field name "
                "(distance_km is kilometres, work_s/rest_s are seconds). Convert "
                "first if the user gave you miles or minutes, then retry."
            ),
            details=bad,
        )
    return (out if out is not None else detail), None


def _error_offender(detail: Any, loc: list) -> tuple[Any, str | None]:
    """Resolve the raw fragment a validation-error ``loc`` points into.

    Returns ``(fragment, kind)`` with ``kind`` one of ``"set"`` /
    ``"exercise"`` / ``"container"``, or ``(None, None)`` when the loc
    doesn't resolve. Error locs are computed against the coerced detail,
    but ``normalize_detail`` and ``_coerce_container`` both rebuild
    exercises/sets lists 1:1 in order, so the indices map safely back
    onto the ORIGINAL (pre-coercion) detail passed here.
    """
    if not isinstance(detail, dict) or not loc or loc[0] not in ("exercises", "skills"):
        return None, None
    container_key = loc[0]
    container = detail.get(container_key)
    if not isinstance(container, list):
        # ``normalize_detail`` may have moved exercises↔skills to match a
        # corrected strength/calisthenics category before validation produced
        # this loc. The move is 1:1, so the opposite source key maps safely back
        # to the caller's pre-normalized fragment for legacy-error comparison.
        alternate_key = "skills" if container_key == "exercises" else "exercises"
        alternate = detail.get(alternate_key)
        if isinstance(alternate, list):
            container_key = alternate_key
            container = alternate
    if len(loc) < 2 or not isinstance(loc[1], int):
        return container, "container"
    if not isinstance(container, list) or not (0 <= loc[1] < len(container)):
        return None, None
    ex = container[loc[1]]
    if len(loc) >= 4 and loc[2] == "sets" and isinstance(loc[3], int):
        sets = ex.get("sets") if isinstance(ex, dict) else None
        if isinstance(sets, list) and 0 <= loc[3] < len(sets):
            return sets[loc[3]], "set"
    return ex, "exercise"


def split_detail_errors(details: list[dict], incoming: Any, stored: Any) -> tuple[list[dict], list[dict]]:
    """Partition ``validate_detail`` error details into
    ``(new_details, preexisting_details)``.

    An error is *pre-existing* when the raw fragment its ``loc`` points at
    already exists verbatim in ``stored`` — i.e. invalid state the row
    already carried and the client merely round-tripped (the web editor
    re-sends stored ``detail_json`` on every save), NOT values the user
    just authored. Callers may persist round-tripped legacy fragments
    instead of wedging every subsequent save on them, while still
    rejecting genuinely new invalid input. Membership (not positional)
    comparison, so inserting a set above a legacy-invalid one doesn't
    reclassify it as new. Pure; ``stored=None`` ⇒ every error is new.
    """
    stored_sets: list = []
    stored_exercises: list = []
    if isinstance(stored, dict):
        for key in ("exercises", "skills"):
            container = stored.get(key)
            if not isinstance(container, list):
                continue
            stored_exercises.extend(container)
            for ex in container:
                if isinstance(ex, dict) and isinstance(ex.get("sets"), list):
                    stored_sets.extend(ex["sets"])

    new_details: list[dict] = []
    preexisting: list[dict] = []
    for err in details:
        loc = list(err.get("loc") or [])
        if loc and loc[0] in ("segments", "terrain"):
            known = False
            if isinstance(stored, dict) and isinstance(incoming, dict) and err.get("type") != "cardio_category_invalid":
                key = loc[0]
                if key == "segments" and len(loc) > 1 and isinstance(loc[1], int):
                    blocks = incoming.get(key)
                    old_blocks = stored.get(key)
                    known = (
                        isinstance(blocks, list)
                        and isinstance(old_blocks, list)
                        and loc[1] < len(blocks)
                        and blocks[loc[1]] in old_blocks
                    )
                else:
                    known = key in stored and incoming.get(key) == stored[key]
            (preexisting if known else new_details).append(err)
            continue
        frag, kind = _error_offender(incoming, loc)
        if kind == "set":
            # Deliberately count-agnostic: a fragment byte-identical to ANY
            # stored set is grandfathered even if duplicated — that can only
            # multiply/relocate an already-tolerated invalid shape (never
            # introduce a new invalid class), and it keeps unchanged legacy
            # sets grandfathered when an edit shifts their indices.
            known = any(frag == s for s in stored_sets)
        elif kind == "exercise":
            known = any(frag == e for e in stored_exercises)
        elif kind == "container":
            known = isinstance(stored, dict) and frag in (
                stored.get(loc[0]),
                stored.get("skills" if loc[0] == "exercises" else "exercises"),
            )
        else:
            known = False
        (preexisting if known else new_details).append(err)
    return new_details, preexisting
