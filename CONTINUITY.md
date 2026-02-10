# Master Ledger: CONTINUITY.md

## Goal
- Stabilize NBHD United as the Django/OpenClaw control plane and scaffold the separate Next.js subscriber frontend.
- Success criteria: backend orchestration is stable and tested, and `frontend/` has a runnable starter implementing onboarding/integrations/usage/billing surfaces.

## Constraints / Assumptions
- Django repo is orchestration-only; OpenClaw remains runtime per tenant.
- External systems (Azure Container Apps, Key Vault, Stripe, Telegram) are mocked in tests where needed.
- Development uses `AZURE_MOCK=true` and local Postgres/Redis.
- Frontend remains a separate build artifact from backend deployment.

## Key Decisions
- 2026-02-08: Reconcile refactor schema drift with explicit migrations instead of interactive prompts.
- 2026-02-08: Continue work from `main`; merged and deleted `feature/openclaw-control-plane`.
- 2026-02-08: Scaffold frontend under `frontend/` using hand-authored Next.js files and offline-safe fonts.

## State
- Done:
  - Backend stabilization merged on `main` with passing tests.
  - Remote feature branch removed after merge.
  - Frontend scaffold implemented and validated (`npm run lint`, `npm run build`).
  - Remediation hardening implemented and validated across billing/webhook/auth/frontend flows.
- Now:
  - Ready for user review of remediation changes.
- Next:
  - Apply migrations + environment secrets in runtime environments and deploy.

## Task Map
```text
CONTINUITY.md
  ├─ CONTINUITY_plan-remediation-hardening.md (@owner:codex)
  │    └─ CONTINUITY_remediation-hardening.md (@owner:codex)
  ├─ CONTINUITY_openclaw-control-plane-hardening.md (@owner:codex)
  └─ CONTINUITY_frontend-scaffold.md (@owner:codex)
```

## Active Ledgers
- `CONTINUITY.md`
- `CONTINUITY_plan-remediation-hardening.md`
- `CONTINUITY_remediation-hardening.md`
- `CONTINUITY_openclaw-control-plane-hardening.md`
- `CONTINUITY_frontend-scaffold.md`

## Cross-task Blockers / Handoffs
- None currently.

## Trivial Log
- [2026-02-08] Created initial `CONTINUITY.md` bootstrap ledger.

## Open Questions (UNCONFIRMED)
- UNCONFIRMED: Preferred production auth/session contract for frontend API calls.

## Working Set
- Files: `apps/**`, `config/**`, `frontend/**`, `CONTINUITY*.md`
- Commands: Django verify commands and frontend `npm` validate commands

## Archived
- None.
