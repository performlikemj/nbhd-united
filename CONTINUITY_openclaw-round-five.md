# Task Ledger: OpenClaw Round Five
Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: REVIEW5_openclaw_94_migration.md
Owner: codex

## Goal
- Resolve six findings using exact runtime evidence; commit fixtures/fixes and push.
## Constraints / Assumptions
- No production DB, tenant containers, deploy or merge. Automatic paths/runtime match main.
- Local isolated runtime, throwaway credentials; >=8 GB disk before pull/retention.
## Key decisions
- Real CLI contract; unchanged selector and writer supply inputs.
## State
- Done: Six findings fixed with exact-runtime fixtures; gates passed; local containers/owned volumes removed.
- Now: Final commit/push to draft PR #1650.
- Next: Release orchestrator review; no production execution or merge by this task.
## Links
- Upstream: CONTINUITY.md
## Open questions
- None.
## Working set
- apps/orchestrator; docs/runbooks/openclaw-94-migration.md
## Notes
- Shell has python3, not python.

- Local test Postgres: nbhd-oc94-r5-tests; localhost:55495; owned volume 16647e4628dd272258ada94d052c6450a07ed132747eba8be13094a2dffe9f31.
- Initial focused canary/resume tests: 18 PASS; /private/tmp/oc94-r5-initial-tests.log.
- Refreshed origin/main remains bd8abe4ea79196cf19eb44c3422b84309cb1b34c; parity diff empty.
- Ruff check / whitespace PASS; local makemigrations --check --dry-run: no changes.
- Pull progress confirmed using attached TTY: shared npm layer 115/210.7 MB, all other layers downloaded; not a failed pull.
- Exact image pulled: manifest sha256:c244ff686863c1e9e4fc9fc55d1fac3b6a746e76039a9632db8ae082a0f21ddc; image sha256:30c6c1e03b602d33090da9e7e8d46cf3a09f4242d5ab5e7d220dc4b06ee3e803; amd64 under local emulation.
- Runtime nbhd-oc94-r5-contract: network none, no ports/mounts, fleet entrypoint bypassed; synthetic token/config; plugins and cron firing disabled. CLI version OpenClaw 2026.9.4 (3a9d69d); NODE_OPTIONS empty. Gateway ready; disk 21 GB free after pull.

## Runtime evidence and fixes
- Captured 12 original cases plus 2 prepared one-shots; all raw list output retained. Real CLI metadata also includes scheduledToolPolicy={version:1,mode:trusted}; accept only this reproduced default.
- Runtime automatically creates reserved heartbeat/skill-collection-review monitors. Local CLI rejects removing or claiming both namespaces; committed runtime-owned evidence. Operator filters those exact namespace/agent pairs, not arbitrary legacy jobs.
- Confirmed main and isolated systemEvent input rejected (--no-deliver); anchors/destinations silently lost; disabled selector excludes; offset and Z one-shots normalize and fail unchanged writer comparison.
- Prepared one-shots pass two real writer reconciliation rounds with zero mutations/stable IDs. Unknown definitions and nondefault policies remain refused.
- First runtime-backed regression run: 6 tests / 18 failures reproduced boundaries. First fixed focused run: 102 PASS (3.484s); /private/tmp/oc94-r5-focused.log.
- Implemented semantic comparator, observation/default-policy validation, canonical-aware verify-only timing, pre-cutover stable ISO preparation, manual canary guard, fresh resume verification.

## Gates and cleanup
- Final focused + contract suite: 129 PASS (3.376s), including actual Node comparison/console bundle tests and signer tests. /private/tmp/oc94-r5-focused-final.log.
- Ruff check/format check (1686 files) and git diff --check PASS. Local makemigrations --check --dry-run: no changes.
- Refetched origin/main: still bd8abe4ea79196cf19eb44c3422b84309cb1b34c. Required automatic paths, signed selector and entire runtime diff empty.
- Local runtime container removed after capture; image retained with 20 GB free.
- Single machine-wide make docker-gate running with /private/tmp/nbhd-docker-gate.lock; log /private/tmp/oc94-r5-docker-gate.log. Gate suffix 28563; owned volume 07886b142bcc8db75dd628ec8b7233406ce54de40339473ca1615dee6ac7ea9f.
- Focused-test container and exact owned volume removed. Gate remains the only local test environment.
- Canary playbook updated for explicit versioned same-family tags (legacy Makefile default now intentionally refused); documentation-only change after gate snapshot.
- PR #1650 verified OPEN/draft on the requested branch; no merge.
- Fixture operator tests now pin Date.now to captured createdAtMs (test-only update after snapshot); final 18 Node/contract/adapter tests PASS in 0.831s, /private/tmp/oc94-r5-final-node-contract.log. Ruff remains clean.

## Final gate and cleanup
- `make docker-gate`: PASS / exit 0; 9359 backend tests in 711.705s, 55 skipped. Config validator/security audit PASS; frontend lint/static build PASS. /private/tmp/oc94-r5-docker-gate.log.
- 129 focused/contract tests PASS; final 18 comparator/adapter tests PASS after pinning fixture clock. Ruff/format, migration drift and whitespace PASS.
- Both owned 64-hex volumes removed, as were runtime/test/gate containers. Machine-wide gate lock released. No broad cleanup/prune.
- Disk 18 GB free after gate; pulled image retained (>=8 GB). Source writer, signed selector and automatic paths still equal refreshed origin/main bd8abe4ea79196cf19eb44c3422b84309cb1b34c.
- No production database or tenant container accessed/changed; ACR authentication/pull was the only Azure operation. No deploy or merge.
