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
- Round 4 supersedes wake exception: hibernation, reconciliation and signed selector restored to main.
- Follow-up: 9.4 lifecycle (croner vs croniter semantics, suspend, idle guards).
## State
- Done: Round 5's six findings fixed against exact 2026.9.4-8ceb89f runtime; 14 matrix fixtures plus reserved-monitor probes. Ruff/format/migration drift PASS; 129 focused + 18 final adapter tests PASS; Docker gate PASS (9359 tests / 55 skipped, config/security checks, frontend lint/build).
- Now: Round 5 implementation d72a4db0 committed/pushed to draft PR #1650; PR description updated; ready for review.
- Next: Release orchestrator review, read-only sizing and separately approved canaries. No production tenant/DB access, deployment or merge by this task.
## Task Map
```
CONTINUITY.md
  ├─ CONTINUITY_openclaw-round-five.md (complete; draft review)
  ├─ CONTINUITY_openclaw-round-four.md (complete; draft review)
  ├─ CONTINUITY_openclaw-rescope.md (complete; draft review)
  └─ CONTINUITY_openclaw_94_migration.md (prior implementation/reviews; scope superseded)
```
## Active ledgers
- CONTINUITY_openclaw-round-five.md
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
