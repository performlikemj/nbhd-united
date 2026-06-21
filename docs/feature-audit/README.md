# NBHD United — Feature Audit

**Canonical spreadsheet:** [`FEATURE_AUDIT.csv`](./FEATURE_AUDIT.csv) — the single source of truth for this audit.

Every feature across the Django control plane (21 apps) and the Next.js subscriber
console (36 pages / 90 components) is inventoried as a user story with a
code-grounded expected behaviour, then tested, fixed, and re-tested.

## Columns

| column | meaning |
|---|---|
| `id` | stable `FA-NNNN` identifier |
| `area` | subsystem (e.g. `billing`, `fuel`, `fe-journal`) |
| `layer` | `backend` / `frontend` / `fullstack` |
| `feature` | short name |
| `user_role` | who triggers it |
| `user_story` | As a `<role>`, I want `<capability>` so that `<benefit>` |
| `expected_behaviour` | precise behaviour grounded in the code |
| `entry_points` | `file:Symbol` + `METHOD /api/path` references |
| `dependencies` | external systems / other features relied on |
| `inventory_notes` | edge cases / gotchas captured during inventory |
| `status` | lifecycle — see below |
| `test_result` | phase-2 verification notes |
| `error_detail` | the bug / UX issue found |
| `severity` | `low` / `medium` / `high` / `critical` |
| `fix_ref` | phase-3 fix reference (commit / file) |
| `retest_result` | phase-4 re-verification notes |

## Status lifecycle

```
Inventoried → Tested-Pass
            → Tested-Fail → Fixed → Retest-Pass
                                  → Retest-Fail (loops back to Fixed)
```

## Rebuilding

`FEATURE_AUDIT.csv` is generated from `features_raw.json` by `build_csv.py`.
Re-running the builder preserves test/fix columns by matching on `area::feature`,
so re-inventory never clobbers recorded results:

```bash
python3 docs/feature-audit/build_csv.py
```

## Phases

1. **Inventory** — every feature → user story + expected behaviour (this doc).
2. **Test** — verify each story against code/tests; document every error.
3. **Fix** — repair every logistical / UX error found.
4. **Re-test** — re-verify each previously-failing story.
