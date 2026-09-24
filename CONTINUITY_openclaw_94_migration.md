# Task Ledger: OpenClaw 94 migration
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: DIRECTIVE_openclaw_94_fleet_migration.md; RECON_openclaw_94_fleet.md
Owner: Codex

## Goal
- Explicit single/batch migration with durable step evidence, preservation and verification; lifecycle guards; runbook and draft PR.
## Constraints / Assumptions
- No production access. Awake tenants only. No automatic rollback. Docker gate serialized machine-wide.
- Exact directive-required ledger filename retained despite general kebab-case convention.
## Key decisions
- Image first, version/tag together, config second, signed cron cutover last.
- Fail closed on ambiguous cron provenance and failed verification.
## State
- Done: Implementation, runbook, 213 focused tests, static/migration checks; final Docker gate green (9292 tests, 43 skipped; frontend lint/build PASS).
- Now: Complete: implementation commit d6081fca pushed; draft PR #1650 open against main.
- Next: Release orchestrator review and later canary execution. No production execution by this task.
## Links
- Upstream: CONTINUITY.md
- Related: docs/runbooks/openclaw-94-migration.md
## Open questions
- None blocking.
## Working set
- apps/orchestrator; apps/cron; apps/tenants; runtime/openclaw.
## Notes
- Initial git status: only directive and recon untracked; branch feat/openclaw-94-tenant-migration.
- No master ledger or locks existed at start. No local .venv in worktree.

## Implementation and validation rollup
- Implemented explicit command and durable JSON checkpoints/lease on Tenant; generated 0168 fields and 0169 RLS relock.
- Source export uses complete live HTTP cron observation, additive import (never delete absent rows), preserves canonical conflicts, disabled jobs and unmanaged rows; agent recurring jobs have explicit re-sync IDs.
- Digest-pinned image and storage retrofit share one saved revision suffix; resume avoids another revision. Version/tag write precedes strict config/workspace regeneration and applied-config observation.
- Operator console adapters inspect payloads only inside the replica; external results contain metadata. Duplicate removal requires a matching signed declaration; two verification polls 25 seconds apart plus health and console error counts.
- 9.4 suspension pauses signed desired state; capture/resume use operator paths. Runtime-family guards protect fleet/task/wake/message image paths.
- Runbook added; architecture wake/rollback guidance corrected.
- Local test DB: isolated pgvector container nbhd-oc94-tests, localhost:55494. No production access.
- `make test-local TESTS='apps.orchestrator.test_openclaw_9_4_migration apps.tenants.test_public_schema_lockdown'`: 19 passed.
- Expanded focused suite (migration, operator JS/transport, SDK, Azure template, lifecycle, guards, signed writer, legacy restore, RLS): 165 passed.
- `ruff check .`: PASS. `ruff format --check .`: PASS (1675 files).
- `manage.py makemigrations --check --dry-run`: no changes detected.
- `make docker-gate`: started after checking no machine-wide nbhd-docker-gate containers; temporary cache under /private/tmp.
- Prior focused failures were legacy fixtures with ambiguous bare target tags/default 9.4 fields, plus new test fixture/assertion mistakes; corrected, then rerun green.
- Review fixes: wait for signed managed-job removal during suspension (do not hide disabled declarations from the helper); require quiet observations across a sync interval. Strict config refresh skips seed refresh/reaping, preserving captured truth. Config stamps leave concurrent newer pending versions pending. Azure LRO/operator subprocess deadlines are bounded.
- All lifecycle observations now use one version-aware reader, including upcoming/in-flight cron guards; operator metadata includes runningAtMs.
- Final expanded focused suite: 199 tests PASS. Ruff lint/format and migration drift checks PASS.
- Broader local orchestrator suite: 1413 run, 1 unrelated error in existing GeminiTtsSmokeTests because the shared local google-genai lacks SpeechMetadata. No changes made to shared venv; pinned Linux Docker gate is authoritative for this SDK contract.
- First Docker gate is exercising an earlier snapshot; a final gate is required after the review fixes. Draft PR does not yet exist; no production execution.
- First Docker gate PASS: 9,286 backend tests, 43 skipped; frontend lint/static build PASS. Existing Gemini SDK test passes with pinned dependencies.
- Final code's focused suite PASS: 199 tests. Final serialized Docker gate started on reviewed snapshot (includes subsequent suspension/config/lifecycle guard fixes).

- Second Docker run: frontend PASS; backend 9289 run / 43 skipped / 3 failures in apps.cron.test_image_update. Cause: old HTTP mocks on 9.4 fixtures now correctly routed through operator CLI. Updated mocks and versioned target tags; production code unchanged.
- Expanded focused suite including dispatch integration: 210 PASS; Ruff and migration checks PASS. Third serialized Docker gate running for final verification.

- Final directive review found already-9.4 no-ops needed health verification. Added read-only image/config/cron/health/log verification outside the row-lock transaction, fail-closed batch behavior, three regression tests, and the exact approved canary order to the runbook. No-op checks never mutate the running tenant or its migration history.
- Third Docker backend PASS: 9289 tests / 43 skipped; frontend still running. A fourth gate is required after the no-op verification change.
- Third Docker gate PASS: backend 9289 tests / 43 skipped and frontend lint/build. Final no-op verification suite: 213 PASS; Ruff and migration drift checks PASS. Fourth serialized gate running on final code.
- Final Docker gate PASS on complete code: 9292 backend tests / 43 skipped; frontend lint and static build PASS; exit 0. Focused suite 213 PASS, Ruff lint/format PASS, migration drift check no changes, diff whitespace checks PASS. No production calls.
- Git: implementation d6081fca pushed on feat/openclaw-94-tenant-migration; draft PR https://github.com/performlikemj/nbhd-united/pull/1650 targets main; never merged. Final ledger-only completion commit does not change validated code.
- Open release risks: real ACR/console/canary behavior and synthetic chat/Brave/delivery/hibernate checks remain for release orchestrator; resolve agent re-sync IDs and maintenance-window one-shots. No inverse migration or automatic rollback.
