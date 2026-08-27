# DIRECTIVE — Phase 2: catalog-aware workout programming (variety + pictures by construction)

Author: Fable, 2026-08-26. Owner: MJ ("go with A", 08-26). Executors: **codex** (all backend + plugin work),
**opencode ox-alpha** (independent critique of this directive before dispatch, and of the final diff),
**pstack** (`/plan` review of this directive, `/land-and-deploy` for the PR). **basecamp/qwen: not needed** —
there is no iOS change in Phase 2; the phone already matches catalog names to figures.

Review 1 (ox-alpha, 08-26, verdict ready-with-fixes) folded: 10 findings — the PII blocker (Decision 4),
audit horizon = 14 d not 7, summary caps at 10 rows, day-level `week_overrides` replacement, the
`SUBAGENT_READ_ONLY_TOOLS` surface, facet/payload trim, sha256 drift pin, metric units, `preferred_days`
contradiction. Review 2 (pstack `/plan`, 08-26, CEO + eng phases, codex + Claude voices; artifacts in the session
scratchpad `phase2/plan/`) folded: `nbhd_fuel_get_plan` over the existing plan-detail endpoint instead of
`horizon_days`; extend the EXISTING PII fitness stop-lists (`_FITNESS_VOCAB/_TOKENS`, `is_never_a_name`,
`retire_stoplisted_bindings`) instead of a new allowlist; middleware already counts runtime calls (no extra
event); limit 50/100 (Core has 45 entries); search also matches `primaryMuscle` while `match` stays exact;
stale `index.js:796/:817` examples; JS mirror of the read-only tool list + `tools-reference.md`; write-time
`unmatched_exercises` warning (User Challenge 2 — accepted by Fable: warn-only, exact, no auto-attach).
Not applied: UC1 (metric → progression report; MJ's variety metric stands, coverage already reported), UC4
(dated fleet rollout — a follow-up after canary soak, MJ picks the date), TASTE 1 (slugs in prescriptions —
names-only stands).

## Why (evidence, MJ's tenant, read-only prod query 2026-08-26)

59 planned sessions with content use only **13 distinct recipes**, each repeated 7–8× ("Lower Power" ×8
identical, "Athletic Power" ×8, "Mobility & Recovery" ×8). 48 distinct exercises out of 302 illustrated
ones. Causes: (1) a plan is one weekly `schedule_json` template stamped across 4 weeks and `week_overrides`
is only used for loads/deloads; (2) the assistant has never seen the catalog — it only drives pictures on
the phone — so it programs from memory with rules that push "compound, consistent".

Side finding: the PII scrubber turns exercise words into people — stored rows read `[person_710] pushdowns`
(Tricep), `standing [person_695] raises` (Calf). Owner views rehydrate, but it is a false positive that will
bite matching/analytics — and (review 1) it also means a **search response can come back as
`[PERSON_710] Pushdown`** unless the catalog endpoint is exempt from known-value egress.

## What the code already provides (read, not assumed; line numbers at origin/main 7f725f7d)

- Fuel tools = OpenClaw plugin `runtime/openclaw/plugins/nbhd-fuel-tools/index.js` (`api.registerTool(wrap({...}))`,
  `callRuntime(api, {path: fuelPath(api, "/audit/"), method})`, `renderPayload`; tools register
  `{optional: true}` → reachable by exact name via tool search, `index.js:186`); contracts listed in
  `openclaw.plugin.json` `contracts.tools`; tests `index.test.mjs` (NOT run in CI today — `ci-cd.yml` lists
  the other plugins' `node --test` lines but not this one). Existing: audit, summary, get/log/update/delete_workout,
  body weight, sleep, profile, create/update/delete_plan.
- Tool policy: main sessions get `group:plugins` (`apps/orchestrator/tool_policy.py:93-96`, merged at
  `config_generator.py:3000-3004`) so a new plugin tool loads without a policy edit — but
  `SUBAGENT_READ_ONLY_TOOLS` (`config_generator.py:49-104`) is an explicit enumeration that already lists
  three `nbhd_fuel_*` tools and demands sync.
- Runtime endpoints: `apps/fuel/urls.py` `runtime/<uuid:tenant_id>/…` → `apps/fuel/runtime_views.py`
  (`_internal_auth_or_401`, `_get_tenant_or_404`, `_FuelResponseGuard` whose `pii_egress_text_fields`
  includes `"name"` and `"activity"`, `runtime_views.py:39-56`). Plan schedule validation:
  `_validate_normalize_schedule` (enforces `has_prescription` for every category — Phase 1e, #1526; override
  days validate with `require_detail=True`, `:1853`). **`week_overrides` merge at DAY level**: an override day
  replaces the base day wholesale (`:1881-1886`, `:1961-1966`); `null` = rest; `update_plan.week_overrides`
  replaces the whole map (`index.js:909-912`).
- Audit view: window is **14 days** (`horizon_14d_end`, `runtime_views.py:2881`; the "~7 days" is the USER.md
  Fuel window in `fuel.md:335`). Neither audit rows (`:2905-2921`) nor summary rows (`:857-871`) carry
  `detail_json` or an emptiness flag, and summary caps `planned_workouts[:10]` (`:856`) — so the agent
  cannot see which future sessions are empty without one `get_workout` per row.
- Plan detail: `RuntimeWorkoutPlanDetailView.get` (`runtime_views.py:2463-2476`) already returns
  `_serialize_plan(plan, include_workouts=True)` — every workout row of the plan (`:1372-1380`: id/date/status…,
  no `detail_json`, no emptiness flag). The plugin already hits `/plans/<id>/` for update/delete (`index.js:937`,
  `:973`) but has no `get_plan` tool.
- Telemetry: the platform-logs middleware already emits one content-free event per runtime call keyed by URL
  name; detail keys must be declared in `DETAIL_ALLOWLIST["fuel"]` (`apps/platform_logs/telemetry.py:52`).
  `_emit_fuel_event` (`runtime_views.py:153-172`) is for call-site reason codes only.
- Read-only tool list has FOUR surfaces: `SUBAGENT_READ_ONLY_TOOLS` (`config_generator.py:49-104`), its JS
  mirror in `runtime/openclaw/plugins/nbhd-routing-context/index.js` (+ `test.js`), the equality test
  `apps/orchestrator/test_subagent_offload.py`, and `templates/openclaw/docs/tools-reference.md` (:187 lists
  `nbhd_fuel_audit`). Stale plugin text: `index.js:796` (empty mobility day / `{"blocks"}` example) and `:817`
  (deload override without `detail_json`) contradict the server's `require_detail=True`.
- Rules: `templates/openclaw/rules/fuel.md` — "Workout Plan Generation", "Plan Updates" ("Fill in my
  workouts"), "Fitness programming knowledge" (the consistency bias). Line 43 still says save
  `preferred_days` as weekday indices, contradicting the name-mandating contracts (`index.js:704`, `:783`).
  Rules reach tenants at config apply (`apps/orchestrator/personas.py`); snapshot/consistency tests
  `apps/orchestrator/test_workspace_rules.py`, `test_fuel_guidance_consistency.py`.
- Catalog data (source of truth today): nbhd-ios `NBHD/Fuel/WorkoutGuideCatalog.json` (v2: 302 entries
  `{slug,name,equipment,primaryMuscle,isStretch,frames}` + 315 aliases, 0 dangling) and the exact
  normalize/match/search rules in `NBHD/Fuel/WorkoutGuideCatalog.swift:47-95` (lowercase → drop apostrophes →
  non-alphanumerics→"-" → trim; exact key, then minus trailing "s"/"es"; search = contains on
  name/slug/aliases with the exact match first; muscle/equipment filters).
- PII (`apps/pii/`): PERSON bindings are minted at **owner chat ingress** (`_WRITER_POLICIES` owner =
  `MINT_ALL`, `authoring.py:32-36`), then known-value substitution runs on every fuel write path independent of
  NER (`authoring.py:376`, `:410`, `:480`; word-boundary regex `egress.py:31-62,107`) and on responses via
  `_FuelResponseGuard`. Registry paths are recursive `detail_json.**` (`store_registry.py:239-244`) and
  `_author_leaf` gets no leaf path (`authoring.py:593-657`) → the realistic scoping seam is
  `model_label`/`field`, not JSON leaf. **Fitness stop-lists already exist**: `_FITNESS_VOCAB` (phrase-level,
  full-span match, PERSON+LOCATION, "generous on purpose", `redactor.py:206`), `_FITNESS_TOKENS` (`:378`),
  `_FITNESS_PHRASES` (`:430`); `is_never_a_name` (`:754-772`) is shared by the detection filter AND the
  `retire_stoplisted_bindings` backfill (`apps/pii/management/commands/retire_stoplisted_bindings.py`,
  `<tenant_id>|--all`, `--commit`), narrowed by `_RETIRE_EXEMPT_TOKENS`. "Tricep"/"Calf" are simply missing
  from those sets. Golden check runner: `python -m apps.pii.golden_check`.
- Shipping a plugin change: image tag `<OPENCLAW_CURRENT_VERSION>-<7sha>` (`tool_policy.py:20`); running
  tenants never auto-roll. Canary recipe + config apply for a hibernated tenant: memory `prod-django-exec-pty`.

## Decisions (pre-made)

1. **Catalog lives in the backend too** (+ a test that every `_EXERCISE_REGISTRY` key in `apps/common/llm_lookups.py`
   resolves via `catalog.match` or is listed as an intentional non-catalog key; `catalog.py` documents why a third
   normalizer exists next to `normalize_exercise` and `_span_tokens`)**:** `apps/fuel/data/workout_guide_catalog.json` — a byte copy of the
   iOS file. Pure Python `apps/fuel/catalog.py` mirrors the Swift normalize/match/search exactly. Drift guard:
   the test pins the file's **sha256** (updated deliberately when iOS bumps the catalog) + asserts 302 entries,
   315 aliases, every alias target exists, and spot names ("Nordic Hamstring Curl", "Romanian Deadlift").
2. **Two new read tools, compact by design.**
   `nbhd_fuel_search_exercises({query?, muscle?, equipment?, limit?})` → `GET runtime/<tenant>/exercises/`
   (URL name `runtime-fuel-exercises`) → `{"results":[{"name","muscle","equipment","stretch":bool}], "total": n,
   "guidance": "<one line>"}`; `"muscles"` + `"equipment_types"` only when `q` is empty or a filter value is
   unknown (unknown filter → 200 with empty results + the legal lists, so the model self-corrects). `limit`
   default 50, max 100 (Core has 45 entries, Glutes 38); `q` capped at 80 chars. `search` also matches the
   normalized `primaryMuscle` ("hamstring" → Romanian Deadlift); `match` (the picture contract) stays exact.
   Names only — no slugs, no image paths. The view does NOT mount `_FuelResponseGuard`'s egress fields
   (`pii_egress_text_fields = ()`): the catalog is public static data. No extra telemetry event — the
   middleware counts the URL name. Tool description carries the valid muscle/equipment values once and says:
   use returned names **verbatim** so the app shows a figure; call it for accessories, mobility movements,
   "what else could I do for X".
   `nbhd_fuel_get_plan({plan_id})` → existing `GET runtime/<tenant>/plans/<id>/`; the plan's workout rows gain
   `"has_prescription": bool` (computed exactly like the write-path guard) — the fill-in walk reads this.
   Both tools go on all four read-only-list surfaces. **Write-time feedback (UC2):** create/update plan,
   update_workout and log responses include `"unmatched_exercises": [names]` when a prescribed name has no
   exact catalog match; the plugin renders one line ("No figure for: X — use exact catalog names"). Warn-only,
   never rejects, never auto-attaches.

3. **Rules, not a new brain** (`fuel.md`): keep the **main lifts** stable across the block; **accessories rotate**
   every 1–2 weeks via `week_overrides` — the example MUST show a **complete day object** (main lifts + the
   swapped accessories) because an override day replaces the base day wholesale and needs a full prescription;
   say that `update_plan.week_overrides` replaces the whole map; **mobility movements** come from the catalog with
   hold times; prefer a catalog name over an invented one; call `nbhd_fuel_search_exercises` while designing and
   while filling in. Fix the `preferred_days` indices sentence (line 43), the stale `index.js:796/:817` examples,
   and add a one-line fallback for when `nbhd_fuel_search_exercises` is unavailable (fleet tenants get rules
   before the image). "Fill in my workouts" walks **all** future planned workouts: `nbhd_fuel_summary` →
   plan ids → `nbhd_fuel_get_plan` → every row with `has_prescription: false` → `update_workout`, re-check
   until none remain. Audit rows (`next_14d`) also gain `has_prescription` for the "this week" case. (Paging
   via summary cannot work: it caps at 10 rows; `horizon_days` on audit was the runner-up — dropped because
   the plan endpoint already returns exactly the target set.)

4. **PII — extend the existing fitness stop-lists (PERSON *and* LOCATION); retire the false bindings with the existing command.**
   (pstack E11: `_is_fitness_span` suppresses on ANY token match, so bare tokens widen suppression fleet-wide —
   surnames `meadows, pendlay, zercher, kroc, arnold, farmer, jefferson` are phrase-only; `romanian, bulgarian,
   nordic` go to `_DEMONYM_STOPLIST` because they arrive as LOCATION spans; the change lives in `_filter_results`
   so `golden_check` (empty map) proves a/b and a seeded-map Django test proves retirement.)
   (a) `_FITNESS_VOCAB` gains every catalog entry name and alias (phrase-level, derived from the catalog JSON at
   import — one source), so "Arnold Press", "Hindu Push-up", "Copenhagen Plank", "Cossack Squat", "Farmers
   Walk" are suppressed as whole spans without bare-listing name-shaped tokens. (b) `_FITNESS_TOKENS` gains only
   clearly non-name single tokens: `tricep, triceps, bicep, biceps, calf, calves, lateral, nordic, romanian,
   bulgarian, pushdown(s)` (demonyms via `_DEMONYM_STOPLIST`). Never bare-list `arnold, farmer, jefferson, hindu,
   russian, copenhagen, cossack, hack, meadows, pendlay, zercher, kroc`. (c) `is_never_a_name` therefore covers them, so `manage.py retire_stoplisted_bindings
   <tenant_id> --commit` retires MJ's "Tricep"/"Calf" bindings in C5 (Fable runs it; MJ's tenant only — fleet
   `--all` is follow-up T8 after the behaviour is seen). Confirm `_RETIRE_EXEMPT_TOKENS` does not shield them.
   If a heal/junk-sweep step exists that rewrites stored `[person_N]` leaves, codex names it; Fable runs it for
   MJ's tenant in C5. Golden cases are leaf-shaped (`detail_json.**` authors each leaf) with negative controls;
   `python -m apps.pii.golden_check` joins the C4 gate. Detection is not loosened anywhere else.

5. **No fuzzy auto-attach anywhere** (unchanged): the tool is a browse/search for a model choosing from a
   list; the phone's picture lookup stays exact.
6. **Measure:** the search endpoint emits one content-free `_emit_fuel_event(reason_code="catalog_search")`
   per call (counts only). Acceptance on MJ's tenant after one real plan (re)generation, same 4-week span:
   **distinct prescribed exercise names ≥ 2× the pre-regeneration distinct count** (baseline measured first;
   tenant-wide today: 48) *and* ≥ 90 % of prescribed names match the catalog (`catalog.match(name)`).

## Tasks (codex, one worktree `.claude/worktrees/workout-catalog-programming`, branch `feat/workout-catalog-programming`)

| # | What | Gate |
|---|---|---|
| C1 | `apps/fuel/data/workout_guide_catalog.json` (copy), `apps/fuel/catalog.py` (normalize/match/search/muscles/equipment_types), tests mirroring the Swift suites + sha256 pin | `manage.py test apps.fuel` |
| C2 | Exercises view + URL (`runtime-fuel-exercises`, egress-exempt, limit 50/100, facets on empty q/unknown filter); `has_prescription` on plan rows + audit rows; `unmatched_exercises` on the four write responses; plugin tools `nbhd_fuel_search_exercises` + `nbhd_fuel_get_plan` in `index.js` + `openclaw.plugin.json`; fix `index.js:796/:817`; all four read-only-list surfaces; `ci-cd.yml` gets the missing `node --test …/nbhd-fuel-tools/index.test.mjs` line | fuel + orchestrator tests, plugin tests, `ruff` |
| C3 | Rules edits per Decision 3 (+ snapshot tests updated deliberately) | `test_fuel_guidance_consistency.py`, `test_workspace_rules.py` |
| C4 | PII per Decision 4 (a, b) + leaf-shaped golden cases | `apps.pii` tests + `python -m apps.pii.golden_check` |
| C5 | Merge (`gh pr merge --auto --squash`) → CI builds image `<ver>-<sha>` → canary-roll MJ's container (image first) → stamp tag + bump pending + `apply_single_tenant_config_task` (rules second) → verify `registered tool=nbhd_fuel_search_exercises` and `nbhd_fuel_get_plan` in the container log → `retire_stoplisted_bindings <MJ tenant> --commit` (+ heal step if one exists) | live |
| C6 | Acceptance on MJ's tenant: baseline variety query first; MJ asks for a new 4-week plan; Fable re-runs the variety query + coverage check | numbers in the ledger |

Run order after compaction: (0) ox-alpha critique of THIS file → folded; (1) pstack `/plan` review → fold;
(2) codex C1–C4 in one run (self-contained prompt, read-first list above, absolute venv
`/Users/michaeljones/Projects/nbhd-united/.venv`, one commit per task, no push); (3) Fable reviews the diff
(rules diff read in full; tool description read in full; no fuzzy matching; names-only payload; PII retire
scope is V∖X only); ox-alpha reviews the diff; (4) push + PR + auto-merge; (5) C5 roll; (6) C6 measure.

## Acceptance

1. `nbhd_fuel_search_exercises(query="hamstring")` and `(muscle="Hamstrings")` from MJ's assistant both return catalog names incl. "Nordic Hamstring Curl", "Romanian Deadlift" **with no `[PERSON_…]` placeholders**; equipment filter works; payload has no slugs/paths.
2. A fresh 4-week plan for MJ: main lifts constant, accessories differ between week 0 and week 2 (visible in `week_overrides`, each override day a complete prescription), mobility days list hold-time movements with figures on the phone; variety + coverage numbers per Decision 6.
3. "Fill in all my planned workouts" fills every future empty session (`nbhd_fuel_get_plan` shows `has_prescription: false` rows, then none); a plan written with an invented name comes back with `unmatched_exercises` naming it.
4. On the NEW plan, stored exercise names contain no `[person_…]`/`[location_…]` placeholders for exercise vocabulary (read-only query); MJ's "Tricep"/"Calf" bindings retired (old rows keep their placeholders until the heal sweep — T8); PII golden set + `golden_check` green.
5. All tests green; rules snapshot tests updated deliberately, not weakened.

## Later (not now)

**Fleet rollout (pstack UC4):** after MJ's canary soaks, a dated `fleet-rollout` of the image + config apply so every tenant gets the tools and rules together — MJ picks the date. pstack's deferred list (backend-served catalog + ETag, registry derivation, bulk prescribe / deep-merge overrides, adherence measurement, fleet-wide retire T8, single-source tool lists, executable tool-description contract) lives in the scratchpad `phase2/plan/TODOS.md` → copy into the ledger at wrap-up.

**PII follow-up (MJ 08-26, "go with 1"):** after C5 lands, Fable runs ONE content-free query — count of single-token PERSON bindings per tenant (counts only, no values) — to size the general "common words get minted as people" problem; small → done, big → its own lane ("single common words never mint"). Not part of this PR.

JA aliases (腕立て伏せ → Push-up) · in-style yoga pack drawn + PR'd upstream to workout-guide · Apple Health
workout write (parked: `nbhd-ios/CONTINUITY_fuel_health_workout_write.md`) · linking planned rows with
imported Watch rows · size trim of the asset pack if the ~+15 MB bothers MJ.
