# Task Ledger: OpenClaw Round Nine
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: CONTINUITY_openclaw-round-eight.md
Owner: codex

## Goal
- Replace the heavy R8 fence; preserve takeover, crash-safe migration publication, numeric bounds, pre-staging and default-deny. Commit/push PR #1650; no merge.
## Constraints / Assumptions
- No production operations. Preserve untracked review inputs. One machine-wide Docker gate; clean only owned resources.
## Key decisions
- Plain loaded boolean read at user/assistant mutation entry points; background transports and gate service restored to main.
- Migration fences, drains 30 seconds, refreshes final capture and signed digest before image submission. Durable flag survives crashes and clears with version commit.
## State
- Done: Lightweight fence, caller audit, runbook and main-parity/zero-query/drain/crash regressions.
- Now: Round 9 complete, validated and ready for PR #1650 reviewer handoff.
- Next: PR #1650 reviewer handoff; release orchestration/canaries require separate authorization. No merge.
## Links
- Upstream: CONTINUITY.md
## Open questions
- None blocking.
## Working set
- apps/cron; apps/integrations/runtime_views.py; apps/actions/views.py; apps/orchestrator; migration 0170; runbook.
## Notes
- Initial disk: 17 GiB free. No running Docker containers or machine-wide gate lock found.

## Implementation
- Removed SQL functions/trigger, middleware and shared transport guards. gate.py, share_cron_sync.py and base.py equal origin/main; gateway_client retains only migration-opt-in metadata-only diagnostics.
- Request checks cover dashboard CRUD/toggle/bulk, pending one-off cancel, three typed patterns, assistant phase2/timezone, all-channel approval and manual registry deletion.
- Added 30 s nontransactional drain before final capture and canonical/share digest verification. Fence remains durable across failure and appears in every --report result.
- Updated R8/R7 tests to request-boundary semantics; retained takeover, atomic publication and integer contracts. Added source parity and zero-query entry-point regressions.
- Local test DB container: oc94-r9-postgres, loopback 55499, owned volume bded9f2cb2f64c0ba43c4150cb1eec8ebcb64f6fbf3d2a56c25318d14a978ff7.
- First test invocation refused invalid test DB name before connection; corrected to test_nbhd_oc94_r9.

## Validation progress
- ruff check . and ruff format --check . PASS (1695 files); makemigrations --check --dry-run: no changes.
- Initial fixture issues corrected: stale forced-auth tenant cache; main's existing tool telemetry query; Python 3.12 AST hashes; required pending-action payload. Parity test excludes unrelated manual-rollout functions retained from earlier rounds.
- Broad focused run: 648 tests; all behavior/contracts passed, one new observer-connection fixture errored on an unregistered Django alias. Replaced with a direct connection to the same isolated test DB; rerun pending.

- Focused/contract 648 PASS (42.819s), /private/tmp/oc94-r9-focused-final.log; real Node contracts included.
- Final caller audit found settings timezone/heartbeat, document-forget reminder deletion, and fuel plan/preferred-time direct cron writers. Added request-only checks there; background/domain reconciliation remains unchanged. Extended zero-query and main-source parity matrix.
- Superseded Docker gate during dependency installation before tests; terminated only owned backend/script and cleaned its resources/lock. Owned PostgreSQL volume 0a9a445cedbd71d6f5defe9280c0b7171968e8edb45f74c872f8e1128cf2ffca tracked for final absence check.

## Shared-path parity audit (origin/main bd8abe4e)
- apps/cron/share_cron_sync.py: byte-identical to origin/main.
- apps/cron/gate.py: byte-identical to origin/main.
- config/settings/base.py: byte-identical to origin/main.
- apps/cron/gateway_client.py: only the existing migration-opt-in `metadata_only=False` argument and redacted diagnostics remain. Exact changed/new lines: 156; 169–173; 185; 218–223; 262–263; 266–267; 308–310; 328–329; 334–335. The migration's `live_source_jobs` requests `cron.list` with metadata_only=True to avoid exporting sensitive gateway/KV error bodies into logs. Default calls retain main's token lookup, retries, payload normalization and error behavior. No DB queries, decorators, tenant flag reads, transactions or locks added to transport.
- Regression manifest pins unfenced reminder entry-point implementation to main after removing only explicit fence checks/imports (Python 3.12 AST); zero-query checks cover loaded tenant fields and each entry, retaining existing telemetry/approval queries.
- Fresh fetch confirms starting remote branch remains 912b6a0d and main bd8abe4e.

- Final expanded focused/contract suite: 1388 PASS (77.192s), /private/tmp/oc94-r9-focused-complete.log. Includes migration rounds, actual Node contracts, all changed reminder/settings/fuel paths, cron gateway/share/gate/reconcile and actions; no skipped tests.
- makemigrations --check --dry-run PASS (no changes); ruff check/format PASS. The extended probe's only error was a nonexistent fuel test module; corrected to apps.fuel in the final 1388-test run.
- Final Docker gate: /private/tmp/oc94-r9-docker-gate.log, exclusive /private/tmp/nbhd-docker-gate.lock; owned backend/postgres suffix 60179. Started with 15 GiB; 9.5 GiB after dependency installation. Local focused-test DB/container and owned volume removed; shared caches retained.

- Final gate fresh-schema catalog check: zero nbhd_migration_cron_guard/nbhd_migration_cron_row_guard functions; zero nbhd_migration_cron_fence triggers; indexed boolean confirmed. Gate-owned PostgreSQL volume: 8caff0599121e0ca37ebaa3d461d0c05b30e3935fe7211f30504e2c823bd4c8b.

## Final gates and disposition
- Final focused/contract suite: 1388 PASS (77.192s), no skips; actual Node contracts exercised locally. /private/tmp/oc94-r9-focused-complete.log.
- ruff check . and ruff format --check . PASS (1695 files). makemigrations --check --dry-run PASS, no changes. git diff --cached --check PASS.
- Complete make docker-gate PASS: 9423 tests in 649.336s, 61 skips; config validator/security audit PASS; frontend lint/build PASS. /private/tmp/oc94-r9-docker-gate.log.
- Local and gate containers removed; all three tracked owned anonymous volumes removed; exclusive machine-wide lock released. Shared caches and unrelated resources retained.
- Fresh-schema catalog confirms indexed boolean, no old guard functions or cron trigger. No production calls, deployment or merge.
- Main parity: share_cron_sync.py, gate.py and base.py equal origin/main; gateway_client.py retains only the exact migration-opt-in metadata-only lines listed above. Full patch saved to /private/tmp/oc94-r9-shared-paths.patch.
- PR #1650 remains OPEN/draft on feat/openclaw-94-tenant-migration. Required trailer: Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>.
- Preserve all untracked user directive/recon/review inputs. Final git diff --stat origin/main..HEAD report: /private/tmp/oc94-r9-diff-stat.txt (generated after commit).
