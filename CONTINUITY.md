# Master Ledger

## Goal
- Deliver the small explicit OpenClaw 2026.9.4 migration on draft PR #1650, with explicit manual scope and conservative lossless prechecks; all automatic paths equal main.
## Constraints / Assumptions
- Work only in this worktree; no production calls, deployment or PR merge. No automatic rollback. Stage by path; never stash.
- User’s rescope supersedes earlier lifecycle/image plans. Keep runtime/openclaw, router/panels and automatic image/wake paths identical to main.
## Key decisions
- Cron contract comes from the exact fleet runtime and unchanged signed writer. Ignore proven observations/reproduced defaults, preserve authored constraints; prepare equivalent ISO instants before cutover.
- No recurring pause: a fire during cutover may be skipped; only imminent one-shots defer.
- No private lifecycle store/recovery machinery. Keep migration-only export/checkpoints admin-excluded.
- DEFAULT-DENY: shared Python/JS normalization plus exact fixture-proven control combinations; noncanonical complete live exports alone authorize promotion, with cache-only quarantine.
- Round 4 supersedes wake exception: hibernation, reconciliation and signed selector restored to main.
- Follow-up: 9.4 lifecycle (croner vs croniter semantics, suspend, idle guards).
## State
- Done: Round 6 fixes implemented with failing-first tests; 75 exact-image captures (70 accepted cases, 5 blocked, 43 normalized shapes); 239 focused/contract tests PASS. Ruff/format and migration drift PASS; protected automatic/runtime paths equal fresh origin/main.
- Now: Round 6 implementation 6f21bc9a committed/pushed to draft PR #1650; Docker gate PASS (9383 tests / 57 skipped, config/security and frontend lint/build). Owned local containers/volumes cleaned; no production access.
- Next: Release orchestrator review and separately authorized canaries; no merge or production execution by this task.
## Task Map
```
CONTINUITY.md
  ├─ CONTINUITY_openclaw-round-six.md (complete; draft review)
  ├─ CONTINUITY_openclaw-round-five.md (complete; draft review)
  ├─ CONTINUITY_openclaw-round-four.md (complete; draft review)
  ├─ CONTINUITY_openclaw-rescope.md (complete; draft review)
  └─ CONTINUITY_openclaw_94_migration.md (prior implementation/reviews; scope superseded)
```
## Active ledgers
- CONTINUITY_openclaw-round-six.md
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
