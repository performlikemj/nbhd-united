# Workout set actuals

`detail_json.exercises[].sets[]` and `detail_json.skills[].sets[]` may include
`logged`. The prescription stays in the set's top-level fields. Actuals never
replace prescription values, and neither validation nor normalization reformats
the supplied `logged` object. The strict Pydantic v2 models and validation live in
`set_contract.py`.

```json
{"type":"weighted_reps","reps":8,"weight":60,"logged":{"reps":8,"weight":62.5,"at":"2026-09-23T07:12:03Z"}}
{"type":"bodyweight_reps","reps":10,"logged":{"reps":12,"at":"2026-09-23T07:12:03Z"}}
{"type":"hold_time","hold_s":30,"logged":{"hold_s":45,"at":"2026-09-23T07:12:03Z"}}
{"type":"weighted_reps","reps":8,"weight":60,"logged":{"skipped":true,"at":"2026-09-23T07:12:03Z"}}
```

- `weighted_reps`: requires integer `reps >= 0` and finite numeric `weight >= 0`, in kg.
- `bodyweight_reps`: requires integer `reps >= 0`.
- `hold_time`: requires integer `hold_s >= 0`, in seconds.
- Any metric may instead use `skipped: true`; numeric actuals must then be absent.
- Every logged object requires `at`: a valid UTC calendar timestamp with seconds,
  uppercase `T`, and either `Z` or `+00:00`. Fractional seconds are accepted and
  preserved. Naive times, nonzero offsets, dates alone, and invalid calendar dates
  are rejected.
- No unknown keys, nulls, numeric strings, booleans as numbers, fractional reps or
  seconds, NaN, or infinities. `skipped: false` is invalid. Omit `logged` for a set
  that has not been performed; `logged: null` is invalid.
- Legacy untyped sets use the existing prescription/registry metric inference.
  Typed sets are recommended. Validation applies to sets in every workout category.

## Owner writes and completion

PATCH `/api/v1/fuel/workouts/<id>/` with the full desired detail and the existing
completion intent:

```json
{
  "status": "done",
  "detail_json": {
    "exercises": [{
      "name": "Bench press",
      "sets": [{
        "type": "weighted_reps", "reps": 8, "weight": 60,
        "logged": {"reps": 8, "weight": 62.5, "at": "2026-09-23T07:12:03Z"}
      }]
    }]
  }
}
```

The existing serializer stamps server time in read-only `completed_at`. It is not
copied from a set timestamp. Repeating `status: done` retains the first timestamp;
reversing completion clears it without removing actuals. Invalid detail rejects
the entire PATCH with HTTP 400 before completion or persistence. Validation errors
identify the `detail_json` exercise/set/field; invalid JSON numbers may instead
produce a JSON parse error. GET and PATCH responses preserve logged values.
Foreign-tenant workouts return 404. No new completion endpoint or model is added.

Owner detail writes remain replacements: the client sends every set it wants to
keep, and may remove `logged` by omitting it from a set in the replacement detail.
An unrelated PATCH omitting detail leaves it intact.

## Assistant rewrites and reads

Runtime workout PATCH and plan reconciliation recover omitted actuals from the
stored workout when an exercise name matches uniquely (case-insensitive, trimmed)
and the same set ordinal has unchanged metric/reps/weight/hold_s. Exercise list
reordering is supported. Runtime PATCH normalizes registry types before matching
stored actuals, then validates the merged detail. A swapped exercise or changed
prescription does not inherit another set's actuals; duplicate exercise names are deliberately not
matched. Explicit incoming `logged` wins and is validated by runtime PATCH.

When both exercise containers are omitted, containers containing logs survive.
Explicit exercise/skill lists remain replacements; an empty list explicitly
removes that container. Other top-level detail fields retain existing replacement
semantics. Runtime create and plan normalization use the same actuals validator.
HealthKit's existing merge already preserves exercise actuals on completion and
adoption.

Fuel workout/plan authoring and runtime response guards restore validated actuals
after PII text substitution so a known-value match cannot rewrite an ISO timestamp.
PII repair uses those same validated snapshots to shield the whole logged object
(timestamps and numbers) from registry traversal. Protected actuals do not consume
the repair text budget; old traversal cursors restart safely. Freeform strings and
invalid logged objects receive no exemption.

The existing runtime workout detail/summary card includes `logged_sets_summary`
when there are valid actuals, for example:

```json
{"logged_sets_summary":["Bench press: 3×8 @ 62.5 kg logged, 1 skipped"]}
```

Different doses are listed separately; skipped sets are counted separately;
unlogged sets do not contribute. Runtime detail also returns the raw detail JSON.
These changes do not alter existing PR/progress calculations, which still consume
prescription fields. Set IDs and concurrent owner/runtime write arbitration are
outside this contract; ambiguous rewrites cannot safely transfer actuals.
