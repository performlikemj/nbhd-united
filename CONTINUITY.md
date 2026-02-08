# Master Ledger: CONTINUITY.md

## Goal
- Stabilize NBHD United as the Django/OpenClaw control plane: provisioning, billing-driven lifecycle, Telegram routing, and integration secret orchestration.
- Success criteria: local environment boots, migrations apply, full test suite passes, service-layer behaviors are validated, and changes are committed on `feature/openclaw-control-plane`.

## Constraints / Assumptions
- Django repo is orchestration-only; OpenClaw remains runtime per tenant.
- External systems (Azure Container Apps, Key Vault, Stripe, Telegram) must be mocked in tests.
- Development uses `AZURE_MOCK=true` and local Postgres/Redis.
- Avoid destructive git operations and preserve unrelated worktree changes.

## Key Decisions
- 2026-02-08: Treat this work as a non-trivial stabilization stream with a dedicated task ledger.
- 2026-02-08: Reconcile refactor schema drift with explicit migrations instead of relying on interactive migration prompts.

## State
- Done:
  - Environment bootstrap and deterministic `.venv` command path.
  - Migration drift fixed and applied (`tenants`, `billing`, `integrations`).
  - Service-layer hardening across tenant lifecycle, Stripe webhook handling, routing, and integrations.
  - Coverage expanded to 47 tests; full suite passing.
- Now:
  - Commit and push changes to `feature/openclaw-control-plane`.
- Next:
  - Share concise rollout/testing summary and open follow-ups if any.

## Task Map
```text
CONTINUITY.md
  └─ CONTINUITY_openclaw-control-plane-hardening.md (@owner:codex)
```

## Active Ledgers
- `CONTINUITY.md`
- `CONTINUITY_openclaw-control-plane-hardening.md`

## Cross-task Blockers / Handoffs
- None currently.

## Trivial Log
- [2026-02-08] Created initial `CONTINUITY.md` bootstrap ledger.

## Open Questions (UNCONFIRMED)
- UNCONFIRMED: Whether remote push to `feature/openclaw-control-plane` is available with current git credentials.

## Working Set
- Files: `apps/**`, `config/**`, `CONTINUITY.md`, `CONTINUITY_openclaw-control-plane-hardening.md`
- Commands: migrate/check/test/makemigrations verification through `.venv/bin/python manage.py ...`

## Archived
- None.
