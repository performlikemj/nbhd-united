# Task Ledger: OpenClaw Round Eight
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: REVIEW8_openclaw_94_migration.md
Owner: codex

## Goal
- Resolve R8-1–4 and stuck-owner reporting; pass gates, commit and push PR #1650.
## Constraints / Assumptions
- No production, deployment or merge. DEFAULT-DENY, pre-staging and prior fixes remain.
- No active migration record: same behavior as main. Only owned local resources; one machine-wide Docker gate.
## Key decisions
- Central tenant-keyed fence must serialize with database and transport writers, surviving hard death through version commit.
- Every RUNNING takeover requires exact token plus explicit confirmed-dead attestation, regardless of lease age.
## State
- Done: All R8 fixes, recovery and main-parity regressions; final 302 focused/contract tests, ruff/format and migration drift checks PASS. Runbook updated.
- Now: Round 8 complete and review-ready on feat/openclaw-94-tenant-migration, PR #1650.
- Next: Reviewer/release-orchestrator handoff; no merge or production execution.
## Links
- Upstream: CONTINUITY.md
## Open questions
- None blocking.
## Working set
- Migration driver/command, cron write boundary, tenant state, runbook and tests.
## Notes
- Preserve all untracked user directive/review inputs.

## Implementation and early verification
- R8-1: indexed tenant fence + central SQL guard; CronJob trigger covers all CRUD/bulk/raw writes. Same guard drains transport/proposal writers before pre-staging. HTTP adapter preserves 409/retry even through broad catches; runtime detail is friendly. Version and fence release commit together.
- R8-2: expired/missing/future RUNNING leases all refuse ordinary retry. Exact --takeover plus --confirm-owner-dead required; --report shows owner and renewal age. Checkpoint CAS preserved.
- R8-3: compare SHA256, unique same-share temp upload/readback + SDK rename(overwrite=True), owner check before rename; no real in-place fallback. Migration and active-record reconciliation use it.
- R8-4: Python integers bounded to abs(value) <= 2**53-1; actual Node boundary checks.
- Early 106-test suite PASS. Initial failures corrected local SQL table name, adapted old expiry tests and isolated DB-free transport fixtures. Full 268-test focused run found only old byte-parity assertion; refined narrowly to permit the authorized decorator while still hashing unchanged main writer/selector bodies.
- Ruff check/format PASS; makemigrations --check --dry-run: no changes. Protected image/wake/reconcile/runtime files still equal fresh origin/main.
- Local DB: oc94-r8-postgres, loopback port 55498, owned anonymous volume c7e47d98d11dc6c63a7765aa46b2305b0f41f64380cc06b88f44ac56cf178244; remove at completion.

- Focused + actual Node contracts: 268 PASS (11.755s), /private/tmp/oc94-r8-focused-final.log. Extra reconcile + typed-runtime suite: 34 PASS (1.039s), /private/tmp/oc94-r8-extra.log.
- Broader cron probe: 553 tests, one typo in the requested module and one DB-free logging fixture needing guard isolation; corrected fixture and reran its module with the correct runtime module (34 PASS above). Full gate covers the remaining cron suite.
- Machine-wide Docker gate running under /private/tmp/nbhd-docker-gate.lock; /private/tmp/oc94-r8-docker-gate.log. Started with 19 GiB; 9 GiB after isolated backend dependency installation. Owned volumes tracked by /private/tmp/oc94-r8-gate-owned-volumes.json.

## Files outside apps/orchestrator/*migration* (this round)
- CONTINUITY.md; CONTINUITY_openclaw-round-eight.md: routing, decisions and validation record.
- apps/cron/gate.py: fence queued cron proposals before accepting them.
- apps/cron/gateway_client.py: shared guard around mutating cron tools.
- apps/cron/share_cron_sync.py: shared guard/active-migration atomic publication; original selector/signer/body unchanged.
- apps/cron/test_gateway_client.py; apps/cron/test_share_cron_sync.py; apps/cron/tests/test_post_reconcile.py: isolate new DB guard in existing DB-free transport unit fixtures.
- apps/orchestrator/management/commands/migrate_tenant_openclaw.py: exact-token/confirmed-dead CLI and RUNNING owner/lease report.
- apps/orchestrator/test_openclaw_round_eight.py: DB CRUD, API/assistant/proposal, parity, concurrent drain, pre-submit repair, atomic death and Python/Node boundary regression coverage.
- apps/orchestrator/test_openclaw_round_four.py: permit only the authorized shared decorator/import while hashing the unchanged main signed writer/selector.
- apps/orchestrator/test_openclaw_round_seven.py: explicit takeover flags and cancellation refusal during simulated post-image hard death.
- apps/tenants/models.py; apps/tenants/migrations/0170_openclaw_migration_cron_fence.py: indexed durable fence plus one SQL guard and CronJob trigger.
- config/settings/base.py: install retryable-JSON exception adapter.
- docs/runbooks/openclaw-94-migration.md: maintenance fence, crash-safe publish, hard-death reporting and takeover evidence.

- First full Docker gate: frontend PASS; backend 9412 tests / 61 skips, 6 fixture-isolation errors. Existing full-suite environment probes remove AZURE_MOCK, exposing the new publisher's real SDK branch in synthetic file tests. The isolated container had no credentials; authentication failed before storage operations. Pinned is_mock=True and explicitly prohibited credential acquisition in those fixtures; no production mutation. Gate-owned resources removed and lock released before retry.

- Final recovery audit: older pre-staged records may predate the new flag. Added schema backfill for non-PASS pre-staged legacy-version records and fence activation on the already-submitted image-resume path; added DB refusal during resumed health probe. Superseded second gate during schema setup, before its test run; terminate/cleanup limited to its tracked backend/postgres/volume and lock.

- Main-parity review caught queued-proposal transaction widening: moved its guard inside the existing transaction, leaving post-commit notification behavior unchanged. Added absent/PASS regression proving the proposal survives notification failure as on main. Superseded the in-progress gate to validate the final transaction boundary; only owned resources stopped.

- Final focused command includes the prior 18-module migration/contract set plus apps.cron.test_gate: 301 PASS (12.384s), /private/tmp/oc94-r8-focused-final.log. Actual Node contracts exercised. Fresh local schema includes the active-record backfill; ruff/format PASS (1694 files), makemigrations --check --dry-run: no changes.
- Final full gate started with 15 GiB free after all code review corrections. Earlier focused DB/volume removed; recreated owned local DB volume is 2b278693975fbdc06dcb60228056d3eb3a842b4668ea1e40540582ac77c8a0f4, retained until final cleanup.
- PR #1650 is OPEN/draft on the requested branch. Local/remote starting head both c7e120c9466bb8730eae6d808ad5e46507b15cf1; no concurrent branch changes.

- HTTP integration review: placed the retry-JSON adapter inside the existing response middleware so CORS/security headers apply to the replacement 409 response. Added browser-origin/header regression; superseded the prior gate and cleaned only its owned resources.

- Final middleware placement validated: 302 focused/contract tests PASS (12.979s); 18 Round 8 tests separately PASS with AZURE_MOCK=false and both Azure credential constructors forbidden (2.652s). /private/tmp/oc94-r8-isolation.log. Focused Postgres container and its recreated anonymous volume removed; only the final gate resources remain.

## Final gates and disposition
- R8-1 FIXED: central indexed guard, database trigger and guarded transports/proposal writes; retryable JSON with CORS/security headers and friendly runtime detail; pre-submit digest repair; legacy-record backfill/resume fence. Absent/PASS transport and proposal-commit behavior tested against main semantics.
- R8-2 FIXED: no RUNNING lease-expiry takeover; exact --takeover plus --confirm-owner-dead, checkpoint CAS retained; report exposes owner token and lease age without remote calls.
- R8-3 FIXED: SHA256 equality skips, unique same-share temp upload/readback + rename(overwrite=True), owner recheck before publication; active-record reconcile uses the same publisher.
- R8-4 FIXED: JavaScript safe-integer bounds enforced before shape matching; real Python/Node boundary contracts pass.
- Final focused/contract suite: 302 PASS (12.979s); separate mock-mode-off/credential-forbidden R8 probe: 18 PASS (2.652s). Logs /private/tmp/oc94-r8-focused-final.log and /private/tmp/oc94-r8-isolation.log.
- Ruff check and format PASS (1694 files); makemigrations --check --dry-run: no changes; both Node migration modules --check PASS.
- Complete make docker-gate PASS: 9416 tests in 664.749s, 61 skips (Node contracts separately exercised locally); config validator/security audit PASS; frontend lint/build PASS. /private/tmp/oc94-r8-docker-gate.log.
- Final git diff --cached --check PASS; protected hibernation/tasks/reconcile/router image updates/suspension/runtime paths equal origin/main bd8abe4e. Signed selector/writer body parity remains pinned; only the authorized wrapper differs.
- All seven owned local/gate anonymous volumes absent, containers removed, machine-wide lock released; 16 GiB free. Shared caches and unrelated resources retained.
- Required commit trailer prepared: Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>.
- User review/directive/recon inputs remain untracked and untouched. No deployment, merge or production mutation; the first isolated gate's credential lookup failed before storage operations, and fixtures now prohibit credential acquisition explicitly.
