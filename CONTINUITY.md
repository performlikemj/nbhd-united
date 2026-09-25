# Master Ledger

## Goal
- Implement the safe, explicit OpenClaw 2026.9.4 migration; pass gates and open a draft PR.
## Constraints / Assumptions
- Work only in this worktree. No production calls. No automatic rollback. Stage by path; never stash.
## Key decisions
- Follow DIRECTIVE_openclaw_94_fleet_migration.md and RECON_openclaw_94_fleet.md.
## State
- Done: Migration, guards, lifecycle fixes and runbook implemented; 213 focused tests and static/migration checks pass. Final Docker gate passed (9292 tests, 43 skipped; frontend lint/build passed).
- Now: Round 2 complete: 212 focused tests and full Docker gate PASS (9340 tests, 53 skipped; frontend PASS). Same migration branch / draft PR #1650; no production execution.
- Next: Release orchestrator review and approved canary/prod execution; never automatic.
## Task Map
```
CONTINUITY.md
  └─ CONTINUITY_openclaw_94_migration.md (round 2 complete; draft PR #1650)
```
## Active ledgers
- CONTINUITY_openclaw_94_migration.md
- Existing unrelated ledgers: CONTINUITY_journal_shaping.md; CONTINUITY_cron_feed_redactor_subagent.md (status unconfirmed).
## Cross-task blockers / handoffs
- None.
## Trivial Log
- None.
## Open questions
- None blocking.
## Working set
- Migration directive and reconnaissance; apps/orchestrator; apps/cron; apps/tenants.
## Archived
- None.

## Fix round 1
- Goal: R1–R8 regression-first fixes and read-only verify-only; no production access.
- Now: complete; fix commit fab6c447 pushed with the required co-author trailer; all gates PASS.

## Fix round 2 (complete)
- Main merged cleanly (includes #1649); chat gates and Talk route kept identical to origin/main.
- Hard constraint: 5.28 lifecycle must follow main exactly; production forbidden.
- Simplification rejected for now: operator jobs and direct phase-two runtime creation bypass canonical rows; one-time import cannot guarantee future preservation. Fix individual findings with regression-first coverage.
- Now: regression tests and fixes. Next: Ruff, migration drift, focused tests, serialized Docker gate, scoped commit/push.

## Round 2 final rollup
- Main merged; chat gates/Talk route unchanged from main. Legacy HTTP lifecycle restored; 9.4 recovery isolated. Simplification declined because not every creator is canonical.
- F1–F9 addressed; regression evidence and remaining release prerequisites in CONTINUITY_openclaw_94_migration.md.
- Ruff/format, migration drift, 212 focused tests, Node suite and full Docker gate PASS. Production untouched; PR remains draft/unmerged.
