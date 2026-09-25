# Task Ledger: OpenClaw 9.4 migration (prior rounds)
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: CONTINUITY_openclaw-rescope.md
Owner: codex

## Goal
- Original migration implementation and two review fix rounds, delivered to draft PR #1650.
## Constraints / decisions
- No production calls, deployment, merge or automatic rollback. Same feat/openclaw-94-tenant-migration branch.
- Superseded: the broad 9.4 lifecycle recovery/image design and pause-recurring policy were rejected by the user’s rescope decision. Current authoritative design/validation is in CONTINUITY_openclaw-rescope.md and the runbook.
## State
- Done: original d6081fca; round-one fab6c447; round-two 9c115047. Source and historical details remain in git history.
- Now: closed as a separate workstream; rescope is active.
- Next: release orchestrator review of the rescoped draft.
## Links
- Upstream: CONTINUITY.md
- Related: CONTINUITY_openclaw-rescope.md; docs/runbooks/openclaw-94-migration.md
## Open questions
- None blocking this historical workstream.
## Working set
- Draft PR https://github.com/performlikemj/nbhd-united/pull/1650
## Prior validation (historical, not evidence for the rescope)
- Original: 213 focused tests; Docker gate 9292 tests / 43 skipped; frontend PASS.
- Round one: 173 focused tests; Docker gate 9324 tests / 52 skipped; frontend PASS.
- Round two: 212 focused tests; Docker gate 9340 tests / 53 skipped; frontend PASS. Ruff/format/migration drift PASS.
- Pinned legacy hibernation call trace, MI ACR, private operator response and migration edit-preservation regressions retained where applicable.
- Prior local containers/owned volumes cleaned at round completion. No production access in any round.
