# Master Ledger

## Goal
- Deliver the small explicit OpenClaw 2026.9.4 migration on draft PR #1650, with explicit manual scope and conservative lossless prechecks; behavior equals main for tenants without an active migration record.
## Constraints / Assumptions
- Work only in this worktree; no production calls, deployment or PR merge. No automatic rollback. Stage by path; never stash.
- User’s rescope supersedes earlier lifecycle/image plans. Keep runtime/openclaw and automatic image/wake paths identical to main. Round 8 permits a cron edit fence only for tenants with active migration records.
## Key decisions
- Round 8: cron edits/transport writers share an indexed tenant fence from pre-staging through atomic version commit; absent/PASS records retain main behavior.
- Cron contract comes from the exact fleet runtime and unchanged signed writer. Ignore proven observations/reproduced defaults, preserve authored constraints; prepare equivalent ISO instants before cutover.
- No recurring pause: a fire during cutover may be skipped; only imminent one-shots defer.
- No private lifecycle store/recovery machinery. Keep migration-only export/checkpoints admin-excluded.
- DEFAULT-DENY: shared Python/JS normalization plus exact fixture-proven control combinations; noncanonical complete live exports alone authorize promotion, with cache-only quarantine.
- Round 4 supersedes wake exception: hibernation, reconciliation and signed selector restored to main.
- Follow-up: 9.4 lifecycle (croner vs croniter semantics, suspend, idle guards).
## State
- Done: Rounds 1–7 retained; all four Round 8 findings fixed, including older-record recovery and absent/PASS parity. Final 302 focused/contract tests, 18 credential-forbidden isolation tests, ruff/format/migration drift and full Docker gate PASS (9416 tests, 61 skips; frontend/config/security PASS).
- Now: Round 8 complete and review-ready for draft PR #1650 on feat/openclaw-94-tenant-migration. Owned local resources cleaned; 16 GiB free. No production mutation.
- Next: Reviewer/release-orchestrator handoff and separately authorized canaries; no merge or production execution by this task.
## Task Map
```
CONTINUITY.md
  ├─ CONTINUITY_openclaw-round-eight.md (complete; draft review)
  ├─ CONTINUITY_openclaw-round-seven.md (complete; draft review)
  ├─ CONTINUITY_openclaw-round-six.md (complete; draft review)
  ├─ CONTINUITY_openclaw-round-five.md (complete; draft review)
  ├─ CONTINUITY_openclaw-round-four.md (complete; draft review)
  ├─ CONTINUITY_openclaw-rescope.md (complete; draft review)
  └─ CONTINUITY_openclaw_94_migration.md (prior implementation/reviews; scope superseded)
```
## Active ledgers
- CONTINUITY_openclaw-round-eight.md
- Existing unrelated ledgers: CONTINUITY_journal_shaping.md; CONTINUITY_cron_feed_redactor_subagent.md (status unconfirmed).
## Cross-task blockers / handoffs
- Release prerequisites: #1651 deployed; actual MI/ACR/console/health canaries and synthetic delivery checks remain release work.
- Follow-ups outside this PR: poller payload logging (pre-existing REVIEW3 #8), noncanonical job persistence, lifecycle queue/Azure/concurrent-wake hardening.
## Trivial Log
- None.
## Open questions
- None blocking.
## Working set
- Migration directive/recon/reviews; apps/orchestrator; apps/cron; apps/tenants; docs/runbooks/openclaw-94-migration.md.
## Archived
- None.
