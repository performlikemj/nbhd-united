# Task Ledger: OpenClaw Round Six
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: REVIEW6_openclaw_94_migration.md
Owner: codex

## Goal
- Resolve R6-1 through R6-5 with failing-first tests and local real-image evidence; commit and push.
## Constraints / Assumptions
- No production access, deployment, or merge. Automatic paths and runtime identical to origin/main.
- DEFAULT-DENY: shared normalized declaration shapes need golden unchanged-writer evidence.
- Only owned local containers/volumes may be cleaned; one machine-wide docker gate.
## Key decisions
- Exact control values/combinations require lossless stable real-writer evidence; data-slot types cannot be spoofed by object values.
- Quarantine is private non-signed migration JSON and commits atomically with cache removal/promotion; canonical authority and prior provenance refusals remain intact.
## State
- Done: All five findings implemented; 75 local fixtures and 43-shape allowlist, 239 focused/contract tests, ruff/format/migration drift checks PASS; runbook/matrix updated.
- Now: All gates PASS; implementation staged for commit/push to draft PR #1650. Owned local containers/volumes cleaned.
- Next: Commit/push implementation and validation rollup; no merge or production calls.
## Links
- Upstream: CONTINUITY.md
## Open questions
- None blocking.
## Working set
- apps/orchestrator; apps/cron/gateway_client.py; scripts/capture_openclaw_94_contract*; docs/runbooks/openclaw-94-migration.md
## Notes
- Untracked directives/reviews are user inputs; preserve them.

## Regression and implementation milestones
- Failing-first: 9 new tests, 8 failures + 1 error reproduce all five findings (`/private/tmp/oc94-r6-red.log`).
- Local tests: owned pgvector container nbhd-oc94-r6-tests on loopback 55496; no production calls.
- Capture: nbhd-oc94-r6-contract, exact pre-pulled image; network none, no ports/mounts, synthetic config/token, scheduler/plugins disabled. Initial startup race failed metadata-only; restarted capture after local readiness.
- Implemented cache quarantine in private migration record atomically with promotion, authored-only instants + rendered semantic assertion, Azure live-family guards, quiet gateway option + sanitized manual failures.
- Shared cron-normalization.json defines Python/JS normalization and shape variable slots. Control values/combinations are literal and default-denied without stable fixture evidence.
- First focused run after fixes and partial capture: 126 tests PASS (3.317s), including real Node normalization/comparator, import/signing, gateway and canary tests. Full evidence capture still running.
- Full capture complete: 75 cases / 70 ACCEPT / 5 BLOCKED; 43 normalized shapes. Local runtime container removed.
- Final focused suite: 238 PASS (8.013s), including actual operator execution for all accepted captures; makemigrations --check --dry-run: no changes; ruff check/format PASS.
- Docker gate acquired machine-wide lock with 19 GiB free; ongoing log /private/tmp/oc94-r6-docker-gate.log.
- Additional default-deny boundary test reproduced shape-marker object collision (2 assertions fail). Both shape interpreters now classify non-scalar data-slot values as unsupported. Added after first gate snapshot; rerun full gate after its completion.
- Quarantine retains full row provenance/identifiers/timestamps as well as declaration data; added assertion failed first, then passed.
- Final focused + contract run: 239 PASS (8.067s), /private/tmp/oc94-r6-focused-final.log. Round-six suite includes JS operator checks for every accepted fixture and malformed shape-marker refusals.
- Stopped outdated first gate during backend tests to include final boundary fixes; its backend/frontend containers and exact owned volume removed. Focused-test container/anonymous volume also removed. No unrelated resources cleaned.
- Final Docker gate starts with 18 GiB free and no other running gate; same machine-wide lock, log /private/tmp/oc94-r6-docker-gate-final.log.

## Per-finding disposition
- R6-1: Fixed; live-only noncanonical authority, atomic full-row quarantine outside signed set, reported counts, rollback/retry regression coverage.
- R6-2: Fixed; at/atMs/expr authored instant only; rendered semantic digest assertion before persistence; legacy/prepared fixture import tests.
- R6-3: Fixed; unresolved image submission blocks every manual guard; Azure image identity must agree with source/target family, including canary.
- R6-4: Fixed; shared normalization spec, exact fixture-backed allowlist, six typed models across schedules/fallbacks plus nullable/legacy/alias/control probes. 75 captures, 70 ACCEPT/5 BLOCKED, 43 proven shapes; unknown combinations denied.
- R6-5: Fixed; opt-in metadata-only gateway/Key Vault/config-render failure paths and sanitized canary/manual errors; capture exceptions/progress metadata-only. Default callers retain prior behavior.

## Gate commands
- `/Users/michaeljones/Projects/nbhd-united/.venv/bin/ruff check .` and `ruff format --check .`: PASS (1688 files formatted).
- `<project-python> manage.py makemigrations --check --dry-run` with the local loopback test DB and AZURE_MOCK=true: no changes.
- `<project-python> manage.py test apps.orchestrator.test_openclaw_round_six apps.orchestrator.test_openclaw_round_five apps.orchestrator.test_openclaw_round_four apps.orchestrator.test_openclaw_rescope apps.orchestrator.test_tenant_openclaw_migration apps.orchestrator.test_runtime_operator apps.orchestrator.test_canary_tenant_image apps.cron.test_gateway_client apps.cron.test_share_cron_sync apps.orchestrator.test_openclaw_9_4_migration apps.orchestrator.test_openclaw_r1 apps.orchestrator.test_openclaw_r2 apps.orchestrator.test_openclaw_schema_shape apps.orchestrator.test_bump_command apps.orchestrator.test_bump_all_tenant_images apps.orchestrator.test_envelope_registry --keepdb --noinput`: 239 PASS, including local Node contracts.
- `node --check scripts/capture_openclaw_94_contract.mjs`: PASS.
- `git diff --cached --check`: PASS; recorded fixture source SHA256 values all match the unchanged production inputs.
- `make docker-gate`: final run in progress, owner wrapper /private/tmp/oc94-r6-final-run-gate.py; containers suffix 63336; owned DB volume a0506b5d2e4c5502a9ba42c72ea5615f7d363837d9c9c666d34346e64c099ec5.
- First complete full gate: frontend PASS; backend ran 9383 tests / 57 skipped with four failures in `apps.cron.test_rollout_atomic_bump.RolloutAtomicBumpEndpointTest`. These legacy tests lacked live-Azure evidence now required by R6-3. Added mocked live same-family image to that test class; no application behavior changed.
- Targeted verification: `<project-python> manage.py test apps.cron.test_rollout_atomic_bump --keepdb --noinput` against the owned previous-gate Postgres volume, loopback only. Result pending below.
- Rollout endpoint suite: 14 PASS (0.310s). Ruff check/format remain PASS. Removed the owned endpoint-test container and reused owned DB volume.
- Rerun full gate after test-fixture correction: /private/tmp/oc94-r6-docker-gate-complete.log; no other gate; 18 GiB free before start.

## Final validation
- Complete `make docker-gate` PASS: 9383 tests in 633.090s / 57 skipped; config validator/security audit PASS; frontend lint/build PASS. Log: /private/tmp/oc94-r6-docker-gate-complete.log.
- Focused + Node contracts: 239 PASS; separately verified rollout endpoint suite: 14 PASS. New Node tests run locally and are among the expected Docker skips where Node is unavailable in the Python image.
- Fresh origin/main remains bd8abe4ea79196cf19eb44c3422b84309cb1b34c. Hibernation, cron_reconcile, tasks, container_updates, cron suspension, signed selector and entire runtime tree are identical.
- All owned test/capture/gate containers and gate DB volumes removed; exact image retained. No production calls, deployment, PR merge, or unrelated cleanup.
- PR #1650 remains OPEN/draft on feat/openclaw-94-tenant-migration. User-supplied untracked directives/reviews remain untouched.
