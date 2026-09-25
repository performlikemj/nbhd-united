# Task Ledger: OpenClaw Round Four
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: CONTINUITY_openclaw-rescope.md; REVIEW4_openclaw_94_migration.md
Owner: codex

## Goal
- Address all eight Round 4 findings; conservative lossless migration with unchanged image and automatic paths.
## Constraints / decisions
- No production calls, merge or deployment. Commit/push PR #1650 branch with requested trailer.
- Restore signed selector too: unmanaged/agent recurring declarations cannot be silently exempted.
- Read-only report and verify-only; payload-free reason counts. One-shot guard before import/time filtering.
- Follow-up: 9.4 lifecycle (croner vs croniter semantics, suspend, idle guards).
## State
- Done: all eight findings implemented, tests/gates passed, runbook and rollup updated.
- Now: implementation complete; commit/push and draft PR description update.
- Next: release orchestrator review and separately authorized canary work; no production execution here.
## Links
- Upstream: CONTINUITY.md
## Open questions
- None blocking.
## Working set
- Migration, manual commands/endpoints, tests, runbook.
## Notes
- Initial tracked working tree clean; supplied reviews/directive/recon untracked, retained.
- Docker socket needs sandbox escalation; no local services started yet.

## Round 4 implementation and validation
- #1/#6: hibernation, reconciliation and signed selector restored byte-for-byte to origin/main; runtime/tasks/router/suspension unchanged. Pinned SHA256 fixture covers all required automatic files and entire runtime tree in git-free Docker/CI snapshots.
- #2/#3: shared explicit scope validation, complete selected-set family guards, endpoints reject malformed/empty/unknown scope before command/publication; --all requires --tenant/--tenants.
- #4: private normalized per-key digest compared inside replica to current canonical truth; snapshot/re-read includes row versions/ownership, bounded single retry then canonical_changed_during_verification.
- #5/#8: canonical one-shots captured/audited before time filtering, both authorities checked for historical resolutions; matching due-time delivery required; no trust in saved cancellation or delivery labels.
- #7: unchanged-writer projection precheck; blocked/deferred initial migrations and --report make no writes. Verify-only reports unsupported declaration identifiers and reason counts. Runbook updated.
- Red run: 9 tests, 19 failures/3 errors reproducing all finding boundaries, log /private/tmp/oc94-r4-red.log.
- Expanded focused runs exposed superseded fixtures and a verify-only exception wrapper; fixed tests for stricter scope and refusal behavior, fixed wrapper to retain BLOCKED_UNSUPPORTED.
- Ruff/format/diff whitespace passed before final small corrections; makemigrations --check --dry-run: no changes.
- Refreshed origin/main: bd8abe4ea79196cf19eb44c3422b84309cb1b34c (unchanged).
- Owned local Postgres nbhd-oc94-r4-tests, 64-hex volume 74a5db44c94dba84946caa891482e408fb511d08b63e23fd8c2ea12dd060e6e7, localhost:55494 only.

- Expanded focused suite: 156 tests PASS (3.790s), /private/tmp/oc94-r4-focused4.log.
- Final audit found a canonical-change race in one-shot error handling; defer that error until the ending reread so a changed canonical set gets the required retry. Final targeted suite: 26 tests PASS (1.116s), including real Node.
- Ruff check / format check (1684 files) / git diff --check PASS. Automatic-path diff against refreshed origin/main is empty.
- Superseded first Docker gate stopped during dependency installation (suffix 88360); only its volume ae8335b9a0f633f641ac99bec2bbaad3fb7fc3adfbf10747a8dc4063753f6d02 removed.
- Final serialized gate running with /private/tmp/nbhd-docker-gate.lock; log /private/tmp/oc94-r4-docker-gate-final.log. Disk 20 GiB free at start.
- Final gate ownership: suffix 89403; Postgres volume e6bb770df03e7f99bc1a5b9fb2e553ac761fed92b6093bd3b42a62b3c92e30c0.

## Final validation and cleanup
- Final `make docker-gate`: exit 0 / PASS. Backend 9347 tests in 662.908s, 53 skipped; config validator/security audit PASS; frontend lint/static build PASS. Log: /private/tmp/oc94-r4-docker-gate-final.log.
- Local focused 156 PASS and final targeted 26 PASS (includes real-Node digest comparisons); Ruff/format/whitespace PASS; makemigrations --check --dry-run: no changes.
- Remote main remains bd8abe4ea79196cf19eb44c3422b84309cb1b34c; automatic-path and full runtime diff empty against it.
- Removed only own localhost test container and exact local/stopped-gate/final-gate 64-hex volumes recorded above. No broad prune. No Docker containers remain; disk recovered to 21 GiB free.
- Production untouched; no live MJ or fleet declarations inspected, no deployments, no merge. Supplied review/directive/recon files retained untracked.
