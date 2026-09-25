# Task Ledger: OpenClaw 94 migration
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: DIRECTIVE_openclaw_94_fleet_migration.md; RECON_openclaw_94_fleet.md
Owner: Codex

## Goal
- Explicit single/batch migration with durable step evidence, preservation and verification; lifecycle guards; runbook and draft PR.
## Constraints / Assumptions
- No production access. Awake tenants only. No automatic rollback. Docker gate serialized machine-wide.
- Exact directive-required ledger filename retained despite general kebab-case convention.
## Key decisions
- Image first, version/tag together, config second, signed cron cutover last.
- Fail closed on ambiguous cron provenance and failed verification.
## State
- Done: Implementation, runbook, 213 focused tests, static/migration checks; final Docker gate green (9292 tests, 43 skipped; frontend lint/build PASS).
- Now: Round 2 complete and validated: 212 focused tests and full Docker gate PASS; delivery target feat/openclaw-94-tenant-migration / draft PR #1650.
- Next: Release orchestrator review and approved canaries. No production execution by this task.
## Links
- Upstream: CONTINUITY.md
- Related: docs/runbooks/openclaw-94-migration.md
## Open questions
- None blocking.
## Working set
- apps/orchestrator; apps/cron; apps/tenants; runtime/openclaw.
## Notes
- Initial git status: only directive and recon untracked; branch feat/openclaw-94-tenant-migration.
- No master ledger or locks existed at start. No local .venv in worktree.

## Implementation and validation rollup
- Implemented explicit command and durable JSON checkpoints/lease on Tenant; generated 0168 fields and 0169 RLS relock.
- Source export uses complete live HTTP cron observation, additive import (never delete absent rows), preserves canonical conflicts, disabled jobs and unmanaged rows; agent recurring jobs have explicit re-sync IDs.
- Digest-pinned image and storage retrofit share one saved revision suffix; resume avoids another revision. Version/tag write precedes strict config/workspace regeneration and applied-config observation.
- Operator console adapters inspect payloads only inside the replica; external results contain metadata. Duplicate removal requires a matching signed declaration; two verification polls 25 seconds apart plus health and console error counts.
- 9.4 suspension pauses signed desired state; capture/resume use operator paths. Runtime-family guards protect fleet/task/wake/message image paths.
- Runbook added; architecture wake/rollback guidance corrected.
- Local test DB: isolated pgvector container nbhd-oc94-tests, localhost:55494. No production access.
- `make test-local TESTS='apps.orchestrator.test_openclaw_9_4_migration apps.tenants.test_public_schema_lockdown'`: 19 passed.
- Expanded focused suite (migration, operator JS/transport, SDK, Azure template, lifecycle, guards, signed writer, legacy restore, RLS): 165 passed.
- `ruff check .`: PASS. `ruff format --check .`: PASS (1675 files).
- `manage.py makemigrations --check --dry-run`: no changes detected.
- `make docker-gate`: started after checking no machine-wide nbhd-docker-gate containers; temporary cache under /private/tmp.
- Prior focused failures were legacy fixtures with ambiguous bare target tags/default 9.4 fields, plus new test fixture/assertion mistakes; corrected, then rerun green.
- Review fixes: wait for signed managed-job removal during suspension (do not hide disabled declarations from the helper); require quiet observations across a sync interval. Strict config refresh skips seed refresh/reaping, preserving captured truth. Config stamps leave concurrent newer pending versions pending. Azure LRO/operator subprocess deadlines are bounded.
- All lifecycle observations now use one version-aware reader, including upcoming/in-flight cron guards; operator metadata includes runningAtMs.
- Final expanded focused suite: 199 tests PASS. Ruff lint/format and migration drift checks PASS.
- Broader local orchestrator suite: 1413 run, 1 unrelated error in existing GeminiTtsSmokeTests because the shared local google-genai lacks SpeechMetadata. No changes made to shared venv; pinned Linux Docker gate is authoritative for this SDK contract.
- First Docker gate is exercising an earlier snapshot; a final gate is required after the review fixes. Draft PR does not yet exist; no production execution.
- First Docker gate PASS: 9,286 backend tests, 43 skipped; frontend lint/static build PASS. Existing Gemini SDK test passes with pinned dependencies.
- Final code's focused suite PASS: 199 tests. Final serialized Docker gate started on reviewed snapshot (includes subsequent suspension/config/lifecycle guard fixes).

- Second Docker run: frontend PASS; backend 9289 run / 43 skipped / 3 failures in apps.cron.test_image_update. Cause: old HTTP mocks on 9.4 fixtures now correctly routed through operator CLI. Updated mocks and versioned target tags; production code unchanged.
- Expanded focused suite including dispatch integration: 210 PASS; Ruff and migration checks PASS. Third serialized Docker gate running for final verification.

- Final directive review found already-9.4 no-ops needed health verification. Added read-only image/config/cron/health/log verification outside the row-lock transaction, fail-closed batch behavior, three regression tests, and the exact approved canary order to the runbook. No-op checks never mutate the running tenant or its migration history.
- Third Docker backend PASS: 9289 tests / 43 skipped; frontend still running. A fourth gate is required after the no-op verification change.
- Third Docker gate PASS: backend 9289 tests / 43 skipped and frontend lint/build. Final no-op verification suite: 213 PASS; Ruff and migration drift checks PASS. Fourth serialized gate running on final code.
- Final Docker gate PASS on complete code: 9292 backend tests / 43 skipped; frontend lint and static build PASS; exit 0. Focused suite 213 PASS, Ruff lint/format PASS, migration drift check no changes, diff whitespace checks PASS. No production calls.
- Git: implementation d6081fca pushed on feat/openclaw-94-tenant-migration; draft PR https://github.com/performlikemj/nbhd-united/pull/1650 targets main; never merged. Final ledger-only completion commit does not change validated code.
- Open release risks: real ACR/console/canary behavior and synthetic chat/Brave/delivery/hibernate checks remain for release orchestrator; resolve agent re-sync IDs and maintenance-window one-shots. No inverse migration or automatic rollback.

## Fix round 1
- Goal: R1–R8 regression-first fixes and read-only verify-only; no production access.
- Now: complete; fix commit fab6c447 pushed with the required co-author trailer; all gates PASS.

### Fix round 1 regression evidence
- Initial regression-first run: 20 tests, 7 failures / 3 errors. Reproduced R1 real-redactor response loss; R2 absent resume and poisoned snapshot; R3 recovery cleared with missing jobs; R4 false PASS after expiration; R5 stale export reused; R6 destination mismatch accepted; R7 CLI-dependent registry failure; R8 no warning; verify-only unknown option. Added imminent/unsupported preflight tests also failed before fixes.
- Implemented private outer-node response, private share-to-DB full declaration capture and verified restore, aborted-suspension recovery, snapshot retention, live re-capture/history and owned-row cancellation, 20-minute/running cutover guard, explicit one-shot dispositions, shared supported-field contract and execution-field mapping, system-assigned MI ACR data plane, structured warning and read-only verify-only codes.
- Local focused suite: 161 PASS. Node sync suite: 28 PASS, 1 optional pinned package-source test skipped. Ruff check/format PASS; makemigrations --check --dry-run no changes; git diff --check PASS.
- Docker gate started after confirming no nbhd-docker-gate container was running; log /private/tmp/oc94-r1-docker-gate.log. Only local isolated Postgres used (port 55494).
- Own local Postgres volume: 510eccb009cc1d88b05a1d0fa16e7723899c720d2a9ad815ccedcb19604b5c1e; retain ownership evidence for targeted cleanup only.

- Extra integration probe found ES-module export syntax in the embedded controller comparator. Added to real-redactor regression, observed failure, fixed, then 161 focused tests passed again. Offline real-Node controller bridge also PASS.
- First round-1 Docker gate intentionally stopped (137) after this fix; its own volume d9a06bfc26e042b3c739e89a9291a887f1e2fb14af793f9d44d60a900910225f removed. Corrected serialized gate running; log /private/tmp/oc94-r1-docker-gate-final.log.

- R8 lookahead follow-up: regression reproduced an unstructured traceback and two warnings when queue retry also failed. Both unknown-cron-state idle paths now emit one payload-free structured WARNING. Expanded focused suite: 173 PASS; Ruff/format PASS.
- Superseded second gate stopped before full tests; removed only its recorded volume c0f01e72bb8f7f0d83f487a2bfc141fdd2cf7bef506b4fc6bce82d6368169dc4. Final complete-code serialized gate: /private/tmp/oc94-r1-docker-gate-complete.log.
- Draft PR #1650 confirmed open/draft, head feat/openclaw-94-tenant-migration, base main. No production access or merges.

### Regression index (all included in the passing 173-test focused suite)
- R1: test_r1_real_redactor_private_response_survives — no NBHD_RESULT before fix; real redactor and embedded pinned comparator pass after fix.
- R2: test_r2_aborted_hibernate_resumes_scheduling / test_r2_retry_keeps_original_snapshot — missing resume and overwritten snapshot reproduced; also test_r2_azure_failure_restores_before_return.
- R3: test_r3_missing_noncanonical_job_cannot_clear_recovery — state incorrectly cleared before fix; full declaration durability, verified resume, real-Node private-file recreation covered by additional R3 tests.
- R4: test_r4_expired_captured_one_shot_fails_verification / test_r4_imminent_job_defers_before_image — both previously failed to raise; test_r4_all_one_shot_dispositions_accounted_for covers pending/delivered.
- R5: test_r5_retry_recaptures_live_edit_and_keeps_history — zero HTTP calls before fix; live recapture, history and canceled imported-row handling now pass.
- R6: test_r6_destination_mismatch_never_matches_or_deletes / test_r6_unsupported_declaration_rejected_at_capture — false match and accepted unsupported delivery reproduced; all mapped execution fields tested with Node.
- R7: test_r7_system_identity_acr_data_plane — CLI-dependent failure before fix; mocked AAD/exchange/token/manifest HEAD succeeds with exact pull scope, no client ID.
- R8: test_r8_suspend_probe_failure_warns_once_without_payload — missing warning before fix; lookahead test_gateway_failure_defers_even_if_retry_publish_fails also reproduced traceback/two warnings, now single structured line.
- Verify-only: test_verify_only_is_read_only_and_reason_codes_only — unknown option before fix; PASS code and unchanged DB record after fix.
- Final gate's own Postgres volume: 660abbdaec232c3caf039ad4529e7db2c71193c886b5ccf29ee2857d31c62c26 (container suffix 81340).

### Fix round 1 final validation
- `ruff check .`: PASS; `ruff format --check .`: PASS (1677 files); `makemigrations --check --dry-run`: no changes; `git diff --check`: PASS.
- Focused suite: 173 tests PASS. Real Node/redactor regressions executed locally. Node signed-sync suite: 28 PASS / 1 optional package-source check skipped.
- Complete-code `make docker-gate`: PASS, exit 0. Backend: 9324 tests in 681.094s, 52 skipped; config validator/security audit PASS; frontend lint and static build PASS. Log: /private/tmp/oc94-r1-docker-gate-complete.log.
- Removed only own recorded anonymous volumes and own nbhd-oc94-r1-tests container; no broad Docker pruning. No production calls, deployment or merge.
- Release risks: build a fresh runtime image with the updated signed adapter; real managed-identity/ACR and console behavior still require approved canaries. Unsupported declarations and unresolved expired one-shots intentionally stop migration. Existing ID-only lifecycle recovery records cannot prove missing payload restoration and remain blocked for manual recovery.

- Completion: fab6c447 pushed to origin/feat/openclaw-94-tenant-migration. Required trailer present. Only the user-provided directive/recon/review files remain untracked. Draft PR #1650 is unmerged; production untouched.

## Fix round 2 (complete)
- Main merged cleanly (includes #1649); chat gates and Talk route kept identical to origin/main.
- Hard constraint: 5.28 lifecycle must follow main exactly; production forbidden.
- Simplification rejected for now: operator jobs and direct phase-two runtime creation bypass canonical rows; one-time import cannot guarantee future preservation. Fix individual findings with regression-first coverage.
- Now: regression tests and fixes. Next: Ruff, migration drift, focused tests, serialized Docker gate, scoped commit/push.

### Round 2 regression evidence and decisions
- Baseline run: 8 tests, 7 failures + 1 error (F1 legacy suspend call sequence; F2 missing durable intent/wake; F4 hourly anchor rejected; F5 mutating eligibility and overwritten canonical edit; F6 historical cancellation blocked; F7 raw admin fields and replica-dependent cleanup). Separate real-Node F3 probe failed with 2 enabled copies instead of 1.
- Initial two local test invocations overlapped teardown of the isolated test DB; no shared/prod DB involved. Subsequent runs serialized. Logs: /private/tmp/oc94-r2-red.log and oc94-r2-f3-red.log.
- Simplification NOT adopted: native cron remains allowed via group:openclaw; typed automation tools use Django but RuntimeCronPhase2SummaryView still calls cron.add directly, and operator CLI mutations bypass canonical rows. One-time import cannot guarantee future creation/cancellation durability. A fully canonical creator contract needs a separate runtime/tool rollout.
- F1/HARD RULE: restored main's literal legacy hibernation/capture bodies and legacy warning behavior. New recovery dispatch is file-cron-only. Pinned origin/main source fixture compares complete hibernate call traces (success, suspend failure, Azure failure). Legacy suspend/resume bodies remain main's. Legacy wake retains main's allowlist behavior, including when opted in; KEEP THE ALLOWLIST EMPTY during rollout.
- F2: queue recovery callback and commit sleep/intent marker before Azure deactivation; reconcile inactive/active/unknown state. Inactive stays marked asleep and gets cron wake; active resumes before marker clearing; unknown retains queued recovery. Completed recovery callbacks no-op. Post-Azure DB failure tested.
- F3: restore serialized by exclusive short-lived share lock, re-read complete runtime set before each mutation, match by original ID/recovery key/equivalent declaration/name conflict, and verify complete matching set. Recreated jobs with new IDs are reused rather than duplicated.
- F4: runtime anchorMs allowed, unsupported execution controls still refused. Hourly phase restored by disabled add then operator cron.update schedule+enabled; subsequent polls retain IDs. Node test verifies ordering and exact phase.
- F5: eligibility before export/import/canonical flip; DEFERRED releases reconciliation. Import version baseline commits atomically with rows; retries preserve newer canonical edits and dashboard tombstones. Explicit --pause-recurring disables saved enabled IDs, verifies quiet, recovers interrupted pre-submit pause; missed recurring fires skipped, imminent one-shots still deferred.
- F6: complete recapture before old due time audits missing IDs as cancelled_at_source / superseded_at_source; expired absence still fails closed without delivery evidence.
- F7: raw fields excluded from admin; superuser metadata-only summary. Transfer cleanup uses Azure Files directly; delayed QStash deletion is queued before transfer for process/replica-loss recovery. Runbook defines encryption/access/retention requirements.
- F8: git diff origin/main -- apps/router/chat_gates.py apps/router/talk_route.py is empty.
- F9: baseline runbook missing 7 required execution contracts; corrected document passes checks for revision-pinned nbhd-django-westus2 console, /app, both MJ verify-only commands, MI data-plane/HTTPS/WSS/health/share prerequisites and supported pause.
- Focused iteration: 142 executable tests PASS (one additional misspelled test label caused command failure); expanded 210-test run found only five old log assertions that incorrectly expected new 9.4 warning behavior on legacy fixtures. Restored those assertions from main and added file-cron-only warning test. Final expanded run pending.
- Ruff lint/format PASS; makemigrations --check --dry-run: no changes; Node signed-sync: 29 PASS, 1 optional pinned package-source skip. Full Docker gate pending.
- Own local Postgres container nbhd-oc94-r2-tests; owned anonymous volume fe7857f8b2e7a0c7f1ef45d01e434cff326737c36507ecfc90342f6a38378ca4. No broad prune; remove only recorded owned volumes.

- Final focused suite: 211 PASS (7.003s); Ruff check/format, migration drift and whitespace PASS. Node 29 PASS / 1 optional skip. Serialized make docker-gate started after verifying no other gate containers; log /private/tmp/oc94-r2-docker-gate.log.

- Docker gate ownership: nbhd-docker-gate-*-12306, Postgres volume 3225c634f256c29eeec7bec2ffb924d525a321ef25323c900372bbbc6704c066. PR #1650 confirmed OPEN/DRAFT, head feat/openclaw-94-tenant-migration, base main.

- First round-2 Docker gate: frontend PASS; backend 9339 run / 52 skipped / 3 failures + 1 error, all new R2 tests. Full-suite environment probes change AZURE_MOCK; our tests implicitly relied on startup state, and the new real-Node test needed the existing optional-Node guard for Python-only backend image. Fixed test isolation with explicit local mock environment and Node availability guard; production code unchanged. Removed only own gate volume 3225c634f256c29eeec7bec2ffb924d525a321ef25323c900372bbbc6704c066. Focused rerun pending, then serialized complete-code Docker rerun.

- Test-isolation focused rerun: 211 PASS. Second Docker run (suffix 22379) deliberately stopped (137) before completion after identifying an import rollback bookkeeping edge: ownership versions mutated the in-memory record before transaction commit. Regression-first test added; fix pending its red result. Removed only second gate volume 40b93de8a8a71452a990dadcb71b8094edf865e1e038d5529a559c30bb6f46b9. No production calls.

- Rollback regression reproduced: test_f5_rolled_back_import_does_not_keep_ownership_versions failed with versions present after rolled-back row insert. Fixed by saving a separate checkpoint copy inside the transaction and publishing versions to the caller only after commit. Expanded final focused suite: 212 PASS (7.095s); Ruff/format and diff checks PASS. Complete-code serialized gate running; log /private/tmp/oc94-r2-docker-gate-complete.log.

- Complete-code gate ownership: nbhd-docker-gate-*-24578; anonymous Postgres volume d9aab489c8ac90dbba18bdb8d0a3d625a2b5c33046b9695f8dd2f0e4f53f6bab. Backend running; source unchanged after snapshot.

### Round 2 final validation
- `ruff check .` and `ruff format --check .`: PASS (1678 Python files); `makemigrations --check --dry-run`: no changes; staged diff whitespace: PASS.
- Focused suite: 212 tests PASS (7.095s), including real-Node duplicate restoration and pinned main call-trace comparison. Node signed-sync suite: 29 PASS, 1 optional upstream-package-source check skipped.
- Complete-code `make docker-gate`: PASS, exit 0. Backend: 9340 tests in 735.464s, 53 skipped (Python backend image has no Node; real-Node tests were run locally). Config validator/security audit PASS. Frontend lint/static build PASS. Log: /private/tmp/oc94-r2-docker-gate-complete.log.
- No production calls, deployment, or PR merge. Draft PR #1650 remains the delivery target on the same branch. Required co-author trailer prepared for the scoped fix commit.
- Remaining release prerequisites: fresh runtime image for updated signed poller; real MI/ACR, WSS, share and tenant health canaries; empty rollout allowlist preserves main's legacy wake behavior. Explicit recurring pause skips missed recurrences and may extend on migration failure. Unsupported controls/unresolved expired one-shots fail closed; private-data encryption/access/retention prerequisites remain operator responsibilities. No automatic rollback.

- Cleanup complete: removed own nbhd-oc94-r2-tests container and remaining recorded volumes fe7857f8b2e7a0c7f1ef45d01e434cff326737c36507ecfc90342f6a38378ca4 / d9aab489c8ac90dbba18bdb8d0a3d625a2b5c33046b9695f8dd2f0e4f53f6bab. Earlier superseded gate volumes also removed; no broad pruning or unrelated resource removal.
