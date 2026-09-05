# Cardio segments validation report

Branch: `feat/cardio-segments`. PR: https://github.com/performlikemj/nbhd-united/pull/1587

## Behavior and decisions

- R0: copied the v1 fixture verbatim. SHA-256: `2be5a5fd53a99a5c60cd6e32e3f578f5e6c95f3aea4fa83421507772ffdaa140`.
- R1: strict Pydantic models share backend vocabulary constants. The public validator accepts an optional category argument (default cardio), since detail alone cannot identify its parent category. Unknown extension fields survive. Planned echoes are recomputed; absent segments remove planned. Stored invalid segment fragments are grandfathered on unrelated consumer edits, but category changes revalidate them.
- R2: one materialisation helper preserves omitted fields, derives ceil(minutes) for timed prescriptions, and clears stale derived duration when prescriptions change. Initial expansion, WorkoutSpec/reconciliation creation, adoption, retemplating, consumer edits, and runtime edits use it. A plan-day segment write (including an echo) cannot inherit an old duration estimate; a duration supplied in the same write wins. Existing edit locks remain in place.
- Telemetry: reuse `emit_tool_event`, namespace `fuel`, tool name `fuel.cardio.prescription_shape`, with the bounded shape in `reason_code`. Each persisted materialisation and accepted runtime prescription observation contributes an event. Shape selection is segments, then structure, then exercises, otherwise flat; this identifies legacy exercise-shaped cardio even if it also carries scalar values. No user text or new telemetry allowlist keys.
- R3: retain duration plausibility, add half-distance plausibility, and decline segment prescriptions without a usable total. HealthKit completion explicitly excludes planned, segments, terrain, and structure from incoming detail updates.
- R4: registry exclusions apply to exact machine leaves across authoring, owner reads, repair, migration, and junk healing. Tool-response redaction, annotation, and the existing runtime known-value guard share path-aware protection. Segment extension notes remain authorable.
- R5: reusable open detail/day schemas cover log, update-workout, create-plan, update-plan, and both override maps. Partial day updates and null rest overrides survive. Runtime successful envelopes use `warnings: ["cardio days use segments, not exercises"]`; existing 201 create / 200 update statuses remain. Legacy exercise-shaped planned cardio receives this warning only when it already satisfies has_prescription (for example, through duration_minutes); exercises alone are rejected. No heart-rate-zone mapping or always-loaded agent-content edits.
- R6: reuse the prescription renderer in both workout-detail-readonly.tsx and the normal workout drawer; the specified read-only component alone only serves orphan recovery. Existing unit conversions handle km/mi, pace, and elevation. Show segments before legacy structure, terrain, planned versus actual totals, and retain scalar stats.
- R7: regression fixes cover null pace rejection, old plan duration inheritance, deduplicated plan warnings, runtime egress byte identity, template-name-only omission, preserving duration on unrelated legacy-invalid edits, parsed optional-duration handling, and test setup corrections. No migrations, historical workout rewriting, iOS changes, deployment, or merge.

## Gates

| Gate | Result |
|---|---|
| Fixture byte comparison | PASS |
| Ruff 0.15.21 check / format check | PASS; 1,600 Python files formatted |
| makemigrations --check --dry-run | PASS: No changes detected |
| Focused contract / write / PII / schema tests | PASS: 34 tests |
| Fuel + orchestrator + PII | PASS: 2,417 tests, 34 skipped, 64.030 seconds |
| Per-suite discovery | Fuel 635; orchestrator 1,194; PII 588 |
| Full apps/ suite | PASS: 8,708 tests, 34 skipped, 663.436 seconds |
| Node Fuel plugin | PASS: 28 tests |
| Frontend npm ci | PASS |
| Frontend lint | PASS: 0 errors, 4 existing warnings outside changed files |
| Frontend production build | PASS: 43 static pages |
| AGENTS rendered content | PASS: 23,505 characters / 23,678 UTF-8 bytes; content pin 23,505; runtime cap 26,000 |

The checked-out AGENTS render and its pin differ from the brief’s 22,758/22,759 figures. The template, rules, docs, renderer, and existing budget tests are unchanged from base 7a25e2db. The existing budget tests pass in the orchestrator suite.

One broader rerun hit an unchanged provisioning test’s blanket `dek` substring assertion against a randomly generated test token. This is outside the cardio paths; no unrelated source was changed. The single-test retry passed, followed by the complete 2,417-test suite passing. No tests were disabled to obtain that result. The repository’s real-model PII tests remain opt-in under the requested gate environment.

### Local test environment

The two virtualenv paths listed in the brief do not exist. Tests ran from this worktree using an isolated uv environment with Python 3.12, Django 6.1, and the repository’s pinned dependencies except CUDA/NVIDIA/triton packages that have no macOS wheels. Repository requirements were not changed. The default local PostgreSQL 14 is unsupported by Django 6.1, so tests used a separate temporary PostgreSQL 17.7 cluster with pgvector and a task-specific test database. Node is 22.23.2. No Docker gate was launched: another gate was running, and the literal process probe also matches the launcher carrying the brief. No source paths outside the allowlist were changed; Next.js regenerated next-env.d.ts was restored to its original bytes.

## Files and line counts by task

Counts are additions/deletions in each task commit, including tests; later R7 refinements are counted separately.

### R0 — `ae91f9a6`

| File | Added | Deleted |
|---|---:|---:|
| `contracts/fuel_cardio_segments.v1.json` | 73 | 0 |

### R1 — `d3d222ac`

| File | Added | Deleted |
|---|---:|---:|
| `apps/common/llm_lookups.py` | 23 | 0 |
| `apps/fuel/runtime_views.py` | 18 | 2 |
| `apps/fuel/serializers.py` | 34 | 3 |
| `apps/fuel/set_contract.py` | 186 | 4 |
| `apps/fuel/test_cardio_segments.py` | 155 | 0 |

### R2 — `8d077d7b`

| File | Added | Deleted |
|---|---:|---:|
| `apps/fuel/cardio.py` | 72 | 0 |
| `apps/fuel/runtime_views.py` | 33 | 1 |
| `apps/fuel/serializers.py` | 12 | 0 |
| `apps/fuel/services.py` | 23 | 0 |
| `apps/fuel/test_cardio_segments.py` | 87 | 0 |

### R3 — `40efaa2a`

| File | Added | Deleted |
|---|---:|---:|
| `apps/fuel/healthkit.py` | 21 | 3 |
| `apps/fuel/test_cardio_segments.py` | 61 | 0 |

### R4 — `6501a075`

| File | Added | Deleted |
|---|---:|---:|
| `apps/pii/authoring.py` | 8 | 1 |
| `apps/pii/historical_migration.py` | 6 | 4 |
| `apps/pii/junk_sweep.py` | 7 | 2 |
| `apps/pii/redactor.py` | 15 | 5 |
| `apps/pii/repair_sweep.py` | 8 | 1 |
| `apps/pii/store_authoring.py` | 1 | 0 |
| `apps/pii/store_registry.py` | 62 | 6 |
| `apps/pii/test_cardio_machine_fields.py` | 116 | 0 |

### R5 — `c090648f`

| File | Added | Deleted |
|---|---:|---:|
| `apps/fuel/cardio.py` | 32 | 0 |
| `apps/fuel/runtime_views.py` | 38 | 12 |
| `apps/fuel/test_cardio_segments.py` | 56 | 0 |
| `apps/orchestrator/test_fuel_tool_schema.py` | 7 | 0 |
| `runtime/openclaw/plugins/nbhd-fuel-tools/index.js` | 82 | 28 |
| `runtime/openclaw/plugins/nbhd-fuel-tools/index.test.mjs` | 61 | 0 |

### R6 — `8642aa58`

| File | Added | Deleted |
|---|---:|---:|
| `frontend/components/fuel/workout-detail-readonly.tsx` | 78 | 8 |
| `frontend/components/fuel/workout-detail.tsx` | 10 | 1 |

### R7 — final verification and regressions

| File | Added | Deleted |
|---|---:|---:|
| `apps/fuel/cardio.py` | 9 | 1 |
| `apps/fuel/runtime_views.py` | 21 | 1 |
| `apps/fuel/serializers.py` | 3 | 0 |
| `apps/fuel/set_contract.py` | 11 | 2 |
| `apps/fuel/test_cardio_segments.py` | 112 | 5 |
| `apps/pii/egress.py` | 9 | 3 |
| `apps/pii/test_cardio_machine_fields.py` | 22 | 2 |
| `contracts/fuel_cardio_segments.validation.md` | 121 | 0 |

## Review fix round 1

- Removed the exercises-only bypass from all three runtime prescription guards. Duration-carrying legacy cardio retains its warning and shape telemetry. Regressions cover rejection on log, workout PATCH, and plan writes, plus an accepted PATCH with telemetry.
- Removed `not`, `if`, and `then` from the cardio tool schema. Dose exclusivity stays in `oneOf`; interval-only repeat/recovery and repeat >= 2 for recovery are described in text and remain server-validated. Node coverage checks every nested cardio schema for unsupported keywords.
- Restored populated-only cardio stat rows. Prescription-only detail has no empty stats grid; EmptyDetails appears only without a prescription or populated stats.
- Gates: Ruff check and format pass (1,600 files); migration check reports no changes; focused cardio 25 tests pass; Fuel/orchestrator/PII 2,419 tests pass with 34 existing skips in 67.861 seconds; Node 28 tests pass; frontend lint passes with 0 errors and 4 existing warnings; build generates 43 static pages.
- Reused the isolated uv and temporary PostgreSQL 17 environment described above. No full apps/ rerun was requested for this round.
