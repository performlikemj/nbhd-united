# Evening Journal Write Fleet Remediation

Date: 2026-08-16
Branch: `fix/journal-write-hang-remediation`
Base: `origin/main` at `85ff71db`
Delivery: one local commit with subject `fix(pii): runtime writes never block on detection — deferral fleet-wide, old detector pinned, honest issue reporting`

## Outcome

The request-path PII detector dependency has been removed from every audited runtime-capable placeholder writer. Known tenant values are still masked synchronously; neural detection is represented by an `unconfirmed` / `detector-deferred` receipt and completed by the bounded repair sweep.

The repair sweep is now hard-capped, fair across tenants and stores, conflict-safe, and staggered away from the top of the hour. The serving engine remains DeBERTa, and the Django image is pinned to a new DeBERTa-only content tag because repository history proved the old v2 alias had been overwritten by the dual-model build. Honest platform incident reports now tolerate blank optional fields, while new runtime images omit them.

Independent review found no remaining P0/P1 issue in the four remediation lanes.

## Revised incident diagnosis

The user-supplied serving evidence establishes that `PII_DETECTOR_ENGINE` was already `deberta`; Liquid was never selected in the serving app. The new engine executing is therefore **not** the cause of the 2026-08-15 hangs.

The evidence-supported operating diagnosis is contention around the single in-process DeBERTa detector: the top-of-hour repair sweep had absorbed deferred datebook work while every tenant's 21:00 evening cron started together. Datebook runtime writes already used the #1465 deferral escape hatch and completed; journal daily-note and lesson writes still performed synchronous deep detection and exceeded the runtime's 20-second budget. The direct fix is fleet-wide request-path deferral. The sweep cap and `:13` offset prevent the repair job from recreating detector starvation.

The dual-model artifact is still shelved as a precaution and per MJ's decision, but this handback does not claim that Liquid code ran during the incident.

## Item 1 — runtime writes no longer wait on deep detection

### Semantics

- `defer_detection` is supported only for `writer="runtime"` and request-reached `writer="background"` defaults. Owner writes cannot opt in.
- Active known values are synchronously replaced before persistence. This path does not call neural detection, does not mint new bindings, and does not emit live detector telemetry.
- Non-empty deferred content receives `state="unconfirmed"`, `reason="detector-deferred"`, offset-free placeholder metadata, and its original writer provenance.
- The hourly repair sweep already selects `unconfirmed` and `residual` receipts. A genuinely new deferred fragment reactivates a previously terminal receipt and clears exhausted repair metadata so convergence can resume; an unrelated `redaction-error` remains terminal-sticky.
- Flag-off runtime/background behavior remains byte-identical passthrough.

### Runtime-writer audit

The regression inventory conservatively treats every non-literal writer expression as runtime-capable. It snapshots 34 direct authoring calls plus five fresh-Document helpers; adding or removing a seam fails the inventory until it is explicitly classified.

| Domain / call family | Sites | Result |
|---|---:|---|
| Actions gate request | 1 | Newly detector-deferred. |
| Core profile and meditation | 2 | Newly detector-deferred. |
| Datebook review request and device command | 2 | Review request was already safe through #1465; device command newly deferred. |
| Finance account, transaction, and payoff | 3 | Newly deferred; dynamic transaction provenance defers only when runtime. |
| Fuel runtime views, plan expansion/reconciliation, and serializer create/update | 13 | Newly deferred; dynamic owner/background paths keep checked behavior. |
| Integrations lifecycle helpers, five fresh Document defaults, session, lesson, Sautai, and template | 12 | Newly deferred; fresh defaults retain `background` provenance. |
| Journal daily-note, weekly-review, purpose, and service writers | 6 | Newly deferred; dynamic owner paths remain synchronous. |
| **Total enforced by AST inventory** | **39** | **Every runtime-capable production authoring site explicitly defers.** |

### Already-safe and intentional synchronous paths

| Seam | Classification |
|---|---|
| Datebook review-request authoring | Already detector-deferred by #1465. |
| Existing-row Document lookup | Returns the stored row without default authoring or detector work. |
| Flag-off runtime/background path | Detector-free, byte-identical passthrough. |
| Owner Document default creation | Remains checked background authoring; the owner contract was not weakened. |
| `RuntimeDocumentKeepView -> record_keep` | Intentionally synchronous `background` / `MINT_VALIDATED`: PDF/email filenames and excerpts bypass chat ingress and require checked authoring before persistence. It is not part of the runtime-writer inventory. |

### Latency regressions

Three regressions install a detector that would wait 0.5 seconds and prove the write completes in under 0.2 seconds with both detector mocks uncalled:

- existing daily-note section update (`nbhd_daily_note_set_section` path);
- fresh daily-note default creation plus section update;
- lesson suggestion (`nbhd_lesson_suggest` path).

All three also assert synchronous known-value masking and the deferred receipt. The lesson test isolates PII authoring by mocking its existing post-write lesson processing/clustering. This remediation guarantees detector-free durable authoring; it does not claim a latency bound for unrelated downstream lesson work.

## Repair sweep — bounded, fair, and request-wins

The chosen control is a hard per-pass work budget plus schedule staggering, not a sleep inside the worker. A row-only cap was insufficient because recursive JSON can fan out into an arbitrary number of detector inputs.

| Control | Final behavior |
|---|---|
| Global row budget | At most 16 rows per sweep. Oversized caller values are clamped. |
| Global detector-input budget | At most 16 checked strings. Current checked authoring performs at most two detector passes per string, so at most 32 detector calls per sweep. |
| Per-tenant budget | At most four rows and four checked strings. Nonpositive limits perform zero work rather than widening to one. |
| Tenant inspection | At most four rotating active tenants by default. IDs use lazy count/sliced iterators; large entity maps load only for the bounded window. |
| Fairness | The tenant pivot advances by expected hourly capacity; the first registry store advances after a fleet cycle. Empty tenants consume an inspection slot, keeping the total pass bounded while the next hour advances beyond them. |
| Recursive JSON | String leaves are repaired in deterministic chunks. The partial receipt stores a keyed source digest, cursor, and aggregate receipt. A partial never declares the field clean and does not increment repair attempts merely for yielding. |
| Concurrent writes | NER runs outside a transaction. A short row lock then compares every registered source field and its receipt; the sweep persists only if the snapshot still matches. The same compare-and-save protects the failure fallback. A request write always wins and is counted as a conflict. |
| Telemetry | Repair outcome counters are applied only after persistence, so a CAS conflict cannot report a repair, residual, or terminal result that was discarded. |
| Schedule | `placeholder-repair-sweep` moves from `0 * * * *` to `13 * * * *`, leaving synchronized top-of-hour evening writes clear. |

The CI system-cron reconciliation updates the existing QStash schedule after Django cutover; the task remains hourly and retry/DLQ-backed.

## Item 2 — DeBERTa is pinned; Liquid remains dormant

### Engine configuration

The effective default is `deberta` in every repository-controlled layer:

- shared detector configuration and fallback resolver;
- Django settings;
- `.env.example`;
- Django image environment;
- deploy workflow environment;
- Container App deployment preparation and validation.

Deployment preparation explicitly overwrites a stale live value with `deberta`; it does not rely only on the unset-value fallback. Unsupported deploy values fail before the Container App spec is written. Liquid implementation remains available only through an explicit `PII_DETECTOR_ENGINE=liquid` selection.

### Artifact-history correction

The initially obvious rollback name, `deberta-finetuned-pii-v2`, was not safe:

- repository history at `9def3904` shows the dual Dockerfile being built and pushed under both the v2 alias and a Liquid bundle tag;
- `95dec1d7` then switched the consumer to v3 explicitly to avoid further tag mutation.

The remediation therefore pins the old DeBERTa revision `a038061af92047b0afbbd5ca07d7aa0521789379` under the new content-named tag `deberta-only-a038061af92047b0`, used as immutable by convention. `Dockerfile.pii-model` defaults `INCLUDE_LIQUID=false`; CI passes that value explicitly, builds the tag only when absent, and the Django-image smoke test rejects `/app/pii-model/liquid`. Dockerfile and workflow consumers use the same tag. Neither v3 nor the mutable v2 alias remains a deploy consumer.

No ACR or production query was made. If the new tag is not already present, tonight's first main deployment must perform the one-time pinned Hugging Face download/build/push before building Django (approximately 554 MB for the DeBERTa model layer). Subsequent builds reuse ACR. This is a deployment critical-path caveat, not a reason to fall back to the contaminated v2 alias.

### Liquid re-enable procedure

Liquid code was not deleted. A future owning lane must:

1. build `Dockerfile.pii-model` with `INCLUDE_LIQUID=true` under a **new, separate** content tag;
2. update the workflow and Django Dockerfile consumer tag together;
3. set the deploy engine explicitly to `liquid`;
4. update the serving-image smoke for the intended dual artifact;
5. rebuild/redeploy Django and monitor startup RSS, latency, and the detector evaluation criteria.

No existing DeBERTa-only tag should be mutated for re-enable.

## Item 3 — honest platform issue reports no longer 400

### Root cause

The runtime plugin always serialized `tool_name` and `detail` after trimming. Omitted optional inputs therefore became explicit empty strings. The DRF serializer declared both fields `required=False` but retained `allow_blank=False`, contradicting the model's blank-tolerant fields and the public optional contract.

For the incident report, `tool_name` was present and `detail` was omitted. DRF returned the error under `detail`, and the plugin's error compactor rendered the observed `runtime_request_failed (["This field may not be blank."])` response.

### Fix

- Django accepts explicit blank `tool_name` and `detail`, preserving compatibility with already-running runtime images and direct callers.
- The plugin omits absent/blank optional strings and trims/preserves supplied values.
- `summary` remains required and nonblank.
- No model or migration change was needed.

## Deployment and convergence

| Component | What must ship | Convergence behavior |
|---|---|---|
| Django app/image | Runtime-writer deferral, receipt convergence, bounded/CAS repair sweep, blank-tolerant issue serializer, DeBERTa env/tag pin, cron registration | Required for the primary hang fix. Deploying Django immediately makes old runtime images' blank issue reports acceptable. |
| OpenClaw runtime image | Corrected `nbhd_platform_issue_report` sender | The workflow builds the OC image; the orchestrator must canary/roll tenant runtimes to converge the sender. Server tolerance protects tenants still on the old image during rollout. No detector weights or detector engine config live in OC. |
| PII model | `deberta-only-a038061af92047b0` ACR build layer | This is not a separately running model service. CI ensures the layer, Django copies it into its image, and Django redeploy is the only serving redeploy. The first-build caveat above applies if the tag is absent. |
| QStash system cron | Existing `placeholder-repair-sweep` schedule changed to `13 * * * *` | Post-deploy system-cron reconciliation updates the existing schedule idempotently; code deployment alone does not move an already-registered external schedule until reconciliation runs. |

Suggested tonight order: ensure/build the DeBERTa-only layer, deploy and smoke Django, reconcile the system cron, build/ship the OC image, then canary-flip tenant runtimes before the 21:00 JST verification window. No production action was performed from this worktree.

## Verification evidence

| Suite / check | Result |
|---|---|
| Runtime-deferral affected suites | 432 passed. |
| Owner lifecycle + Fuel reconciliation suites | 51 passed. |
| Runtime-deferral focused total | 483 passed; includes the three blocking-detector regressions. |
| Repair sweep | 26/26 passed in the owner run and again in independent review. |
| Detector engine + deploy configuration | 17 total: 16 passed, one real-model opt-in test skipped. |
| Platform issue Django tests | 13/13 passed. |
| Journal plugin schema tests | 9/9 passed. |
| Post-first-gate predicate guard + repair suite | 36/36 passed. |
| Ruff, Ruff formatting, YAML parse, and scoped diff checks | Passed in their respective lanes. |
| Independent cross-review | PASS; no remaining P0/P1 finding. |

### First full-gate run and correction

The first `make docker-gate` run completed the frontend leg successfully and ran 7,890 backend tests with one failure. The new tenant-fairness regression queried future-encrypted `Task.title` with a SQL value predicate (`title__startswith`), which the repository's encrypted-column predicate guard correctly forbids. This was test-only; production sweep behavior was not implicated.

The test now selects its bounded tenant rows by tenant ID and inspects `title` in Python. The encrypted-column guard plus the repair suite then passed 36/36. A complete clean Docker-gate rerun was required rather than treating the first run as acceptable.

### Final clean Docker gate

- Command: `make docker-gate`
- Final backend count/result: 7,890/7,890 passed in 504.807s; 35 skipped; backend leg PASS.
- Final frontend result: ESLint completed with zero errors (four pre-existing warnings), Next.js static build completed, frontend leg PASS.
- Gate tail:

```text
=== BACKEND LEG: PASS ===
=== FRONTEND LEG: PASS ===
=== DOCKER CI-PARITY GATE: PASS ===
```

## Operational fences and delivery

- Mocks/local tests only; no production, tenant, ACR, QStash, or model-service access.
- No deployment, push, or pull request.
- No Liquid code deletion.
- Exactly one local commit contains the remediation and this handback, with the subject shown at the top.
