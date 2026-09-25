# Task Ledger: OpenClaw migration rescope
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: CONTINUITY_openclaw_94_migration.md
Owner: codex

## Goal
- Small explicit migration, manual fleet refusal, Postgres-based cron wake only for file-cron tenants. Commit/push same draft branch; no production.
## Constraints / decisions
- User rescope supersedes prior lifecycle/image scope. Runtime image, tasks/router automatic updates, suspension/resume restored to main.
- Keep migration checkpoints/export private and admin-excluded; remove lifecycle private fields and transfer/recovery callbacks.
- No recurring pause; recurring fires during roughly five-minute cutover may be skipped. One-shot imminence defers before import/mutation.
- Keep digest/storage revision, fresh additive capture, newer canonical edits, image-first config, signed write, verification/verify-only, MI ACR, fail-stop/no rollback.
## State
- Done: rescope implementation, runbook, regression coverage and all required gates.
- Now: implementation complete; same draft PR #1650 ready for review.
- Next: release orchestrator reviews and performs approved canaries; no production execution here.
## Links
- Upstream: CONTINUITY.md
- Related: docs/runbooks/openclaw-94-migration.md; REVIEW3_openclaw_94_migration.md
## Open questions
- None blocking.
## Working set
- apps/orchestrator; apps/cron; apps/tenants; runbook.
## Notes
- Initial branch diff: 43 files, 3984 insertions / 65 deletions.
- No production calls. User-supplied review/directive/recon files remain untracked.

- Removed image edits (including Dockerfile change), automatic path guards, suspension/resume/capture rewrites, lifecycle field, transfer files/callbacks, sleep markers and pause-recurring.
- Comparator moved into controller-only migration module; delivery.to verification retained. Storage retrofit is explicit migration-only opt-in.
- File-cron hibernation uses signed writer rows; known noncanonical count is a payload-free estimate from Postgres/snapshot, no operator capture.
- Owned local Postgres: nbhd-oc94-rescope-tests; volume c161b5ec66a20356827bbbf40001738aa6ec931475a1017763b4953bece27e31.
- Ruff check/format PASS; focused suite and migration drift running locally.
- Focused final: 197 tests PASS (6.716s), including Node comparator/redactor and pinned-main 5.28 trace. Initial runs found old fixture expectations; corrected recurring→one-shot guard fixture, same-family manual tags, explicit storage opt-in and legacy lookahead version.
- `makemigrations --check --dry-run`: no changes. `ruff check .` / `ruff format --check .`: PASS (1681 files).
- Source/AST audit: runtime/openclaw, Dockerfile, tasks.py, entire apps/router and suspension.py match origin/main. Only existing hibernation function changed is hibernate_idle_tenant; one new canonical helper.
- Now: final serialized Docker gate, then commit/push; no production calls.

## REVIEW3 disposition
- #1: automatic task/message/wake guards removed; manual guards are the explicit user-approved exception.
- #2: deactivation recovery rewrite, callbacks and intent markers removed. Main’s lifecycle failure/race behavior remains a follow-up.
- #3 / #4: all runtime image/poller changes reverted to main. Migration comparator is controller-only and fails verification on unsupported image behavior.
- #5: recurring pause option/state/mutations/recovery removed; missed cutover recurrences may be skipped.
- #6: absent canonical row plus absent live job yields timestamped cancelled disposition; either authority remaining prevents this shortcut.
- #7: four manual fleet entry points refuse family crossings/ambiguous sources; atomic endpoint requires explicit valid tenant_id and rejects malformed bodies before publishing.
- #8: image logging changes reverted; pre-existing payload logging is explicitly tracked for a separate image follow-up.
- #9: private declaration restoration/creator race removed with lifecycle restoration machinery.
- Docker gate ownership: suffix 51773; volume f1a1f959d5f5aac512230598b5a782a293c8664233c59c7e4bc374a33dd11fd4; log /private/tmp/oc94-rescope-docker-gate.log.
- PR #1650 confirmed OPEN/DRAFT, head feat/openclaw-94-tenant-migration, base main.
- Superseded first gate stopped during dependency install after finding one older manual-command test with an unknown source image. Fixture now pins its already-current image; final focused suite includes this module. No production-code behavior changed.
- Expanded complete focused suite: 204 tests PASS (7.492s); log /private/tmp/oc94-rescope-focused-204.log.
- Superseded gate exited 137 before testing; its recorded volume was removed. Final gate suffix 53033; owned volume c4911590a080ca1ba6d2bf3135ad5e28c95b240906a858fbf546778120c0bdd4; log /private/tmp/oc94-rescope-docker-gate-final.log.

## Final validation / handoff
- `ruff check .`: PASS; `ruff format --check .`: PASS (1681 Python files); `makemigrations --check --dry-run`: no changes; staged whitespace PASS.
- Focused suite: 204 tests PASS (7.492s), including local real-Node comparator/redactor, cancelled one-shot, manual scope/family guards, canonical wake and pinned-main legacy trace.
- Final `make docker-gate`: PASS, exit 0. Backend 9332 tests in 663.916s / 51 skipped; config validator/security audit PASS. Frontend lint (four pre-existing warnings, zero errors) and static build PASS.
- Final log: /private/tmp/oc94-rescope-docker-gate-final.log. Runtime image, automatic tasks, router and suspension source match origin/main; only hibernate_idle_tenant changes among existing lifecycle functions.
- Cleanup: removed own nbhd-oc94-rescope-tests and all three recorded gate/local 64-hex volumes; no broad prune or unrelated deletion.
- Delivery: same feat/openclaw-94-tenant-migration branch and draft PR #1650; required co-author trailer. Production untouched; PR not merged.
- Release limitations: actual MI/ACR/console/image declaration compatibility and synthetic chat/Brave/delivery/wake checks remain canary prerequisites. Noncanonical count is a snapshot/Postgres estimate, not a fresh runtime inventory.
