# Task Ledger: OpenClaw Round Seven
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: REVIEW7_openclaw_94_migration.md
Owner: codex

## Goal
- Fix R7-1 through R7-5, record real-image pre-staged boot evidence, pass gates, commit and push.
## Constraints / Assumptions
- No production access, deployment, merge, or changes to protected main-parity paths.
- DEFAULT-DENY and all prior fixes retained. Only owned local resources cleaned; one machine-wide Docker gate.
## Key decisions
- Signed cron file precedes image submission; config remains image-first.
- Recovery takeover requires exact owner identity and confirmed termination; all checkpoint writes fenced.
## State
- Done: All five R7 findings fixed; real-image boot fixture, 254 focused/contract tests and complete Docker gate PASS. Runbook updated; protected main parity confirmed.
- Now: Completed implementation on feat/openclaw-94-tenant-migration for draft PR #1650.
- Next: Reviewer/release-orchestrator handoff; separately authorized canaries only. No merge or production execution.
## Links
- Upstream: CONTINUITY.md
## Open questions
- None blocking.
## Working set
- Migration, preservation/operator adapters, manual guard, personas, tests, runbook.
## Notes
- Preserve untracked user directive/review files.

## Implementation and local validation
- R7-1: exact canonical signed bytes read back before image; signed_prestaged/recovery checkpoint; post-health canonical/file comparison; 60-minute canonical+runtime one-shot guard; 10-minute lease and operator-attested dead-owner CAS takeover. Old owner checkpoint/mutation refusal tested.
- Real-image boot PASS: unchanged default entrypoint on local exact manifest c244ff68... installs two pre-staged jobs (recurring + at), stable IDs/digests across two polls, no manual cron mutations. Fixture prestaged-boot.json + reproducible script committed with the work. Network none, synthetic data, scheduled firing/plugins disabled; owned capture container removed.
- R7-2: type-tagged Python shape keys, shared scalar checks before normalization, integer-only generated policy; JS boolean/numeric boundary coverage. JavaScript cannot retain JSON number spelling 1 vs 1.0; Python rejects floats before submission.
- R7-3: exact runtime controls evaluated in-replica; only fixed reasons and hashed identifiers returned; golden model/typed/main-agent cases pass.
- R7-4: validated tag@sha256 accepted; family, malformed digest and unresolved submission refusals retained.
- R7-5: opt-in strict renderer context flows to persona loaders and nested KV reads; cold-cache sentinel withheld, defaults preserved.
- Focused + contract suite: 253 tests PASS (8.932s), /private/tmp/oc94-r7-focused-complete.log. Ruff check and format PASS (1690 files); makemigrations --check --dry-run: no changes.
- Fresh origin/main parity PASS for hibernation, cron_reconcile, tasks, container_updates, cron suspension, signed selector, runtime/. git diff --check PASS.
- Docker gate next: 19 GiB free, no competing gate; machine-wide lock wrapper /private/tmp/oc94-r7-run-gate.py, log /private/tmp/oc94-r7-docker-gate.log; cleanup limited to captured owned volumes/containers.

- Final review retained the prior stale-DB-connection checkpoint retry, now fenced on both update attempts. Added stale-connection/takeover regression: final focused+contract count 254 PASS (9.047s).
- Superseded first gate deliberately during initial test-DB setup to include this correction; owned backend/postgres/volume cleaned by wrapper. No competing gate. Restart begins with 17 GiB free; prior log /private/tmp/oc94-r7-docker-gate-superseded.log.

## Final gates and cleanup
- `<project-python> manage.py test` with 17 focused modules (the Round Six set plus test_openclaw_round_seven), local loopback Postgres/AZURE_MOCK=true, --keepdb --noinput: 254 PASS (9.047s). Includes actual Node contracts; /private/tmp/oc94-r7-focused-complete.log.
- `<project-ruff> check .` and `format --check .`: PASS, 1690 files formatted. `manage.py makemigrations --check --dry-run`: no changes.
- `node --check apps/orchestrator/migration_cron_digest.mjs` and comparator: PASS.
- Complete `make docker-gate`: PASS; 9398 tests in 650.316s, 60 skips (Node-dependent tests exercised by the local contract gate), config validator/security audit PASS, frontend lint/build PASS. /private/tmp/oc94-r7-docker-gate.log.
- `git diff --cached --check`: PASS. Protected files/runtime equal origin/main bd8abe4ea79196cf19eb44c3422b84309cb1b34c.
- All owned capture/test/gate containers and anonymous DB volumes removed, machine-wide lock released. 18 GiB free; pre-existing image retained. No unrelated cleanup.
- PR #1650 confirmed OPEN/draft with the expected branch. User-supplied untracked review/directive/recon files preserved. No production calls, deployment, or merge.
