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
- 2026-02-10: Plan Google OAuth MVP in-house (Gmail + Calendar first), defer Composio until broader integration scope.
- 2026-02-10: Use Django proxy access boundary for Gmail/Calendar in MVP; defer direct OpenClaw token handling to a later phase.
- 2026-02-11: Prioritize read-only assistant value (email/calendar visibility + action-item extraction), defer write actions.
- 2026-02-11: Use native OpenClaw plugin/tool integration for runtime wiring to NBHD endpoints (defer MCP-first approach for current scope).

## State
- Done:
  - Backend stabilization merged on `main` with passing tests.
  - Remote feature branch removed after merge.
  - Frontend scaffold implemented and validated (`npm run lint`, `npm run build`).
  - Remediation hardening implemented and validated across billing/webhook/auth/frontend flows.
  - Google OAuth MVP Task 3 completed (credential broker + internal auth groundwork + failure semantics hardening).
  - Google OAuth MVP Task 4 scaffold milestone completed (internal tenant-scoped Gmail/Calendar runtime endpoints + tests).
  - Google OAuth MVP Task 4 Phase A/B completed (read-only OAuth scope defaults + Gmail message detail + Calendar free/busy endpoints).
  - Google OAuth MVP Task 4 Track 1 completed in control plane (runtime env injection + env-gated plugin/tool wiring in generated OpenClaw config).
  - Runtime build path created in-repo for `nbhd-openclaw` (Dockerfile, entrypoint, plugin scaffold, CI build/push step).
  - Local runtime Docker validation passed (`nbhd-openclaw:local` builds; `nbhd-google-tools` plugin loads in container).
- Now:
  - Task 4 runtime capability wiring in progress (Track 2 scaffold + local validation complete; awaiting first ACR publish + full e2e).
- Next:
  - Publish first `nbhd-openclaw` image from this repo CI, set plugin env vars, and run full provisioned-tenant e2e.

## Task Map
```text
CONTINUITY.md
  ├─ CONTINUITY_plan-google-oauth-mvp.md (@owner:codex, in-progress)
  │    ├─ CONTINUITY_google-oauth-hardening.md (@owner:codex, completed)
  │    ├─ CONTINUITY_google-oauth-refresh-scheduling.md (@owner:codex, completed)
  │    ├─ CONTINUITY_google-oauth-token-boundary.md (@owner:codex, completed)
  │    └─ CONTINUITY_google-runtime-capability-wiring.md (@owner:codex, in-progress)
  ├─ CONTINUITY_plan-remediation-hardening.md (@owner:codex)
  │    └─ CONTINUITY_remediation-hardening.md (@owner:codex)
  ├─ CONTINUITY_openclaw-control-plane-hardening.md (@owner:codex)
  └─ CONTINUITY_frontend-scaffold.md (@owner:codex)
```

## Active Ledgers
- `CONTINUITY.md`
- `CONTINUITY_plan-google-oauth-mvp.md`
- `CONTINUITY_google-oauth-hardening.md`
- `CONTINUITY_google-oauth-refresh-scheduling.md`
- `CONTINUITY_google-oauth-token-boundary.md`
- `CONTINUITY_google-runtime-capability-wiring.md`
- `CONTINUITY_plan-remediation-hardening.md`
- `CONTINUITY_remediation-hardening.md`
- `CONTINUITY_openclaw-control-plane-hardening.md`
- `CONTINUITY_frontend-scaffold.md`

## Cross-task Blockers / Handoffs
- @handoff-to:CONTINUITY_google-runtime-capability-wiring.md - Task 4 active; implement Track 1/2 (tenant config injection + OpenClaw plugin runtime wiring).
- @blocked-by: runtime image publish still pending after new workflow path (must verify ACR tags and smoke runtime).

## Trivial Log
- [2026-02-08] Created initial `CONTINUITY.md` bootstrap ledger.
- [2026-02-10] Assessed Google OAuth (Gmail/Calendar) MVP readiness; identified gaps in scheduled token refresh wiring and runtime data-access consumption.
- [2026-02-11] Validated Stripe checkout -> signed webhook completion smoke path locally (tenant transitioned to ACTIVE with mock container provisioning).
- [2026-02-11] Validated Stripe checkout -> webhook -> provisioning path inside Docker (`web` + `postgres`) with `AZURE_MOCK=true`.

## Open Questions (UNCONFIRMED)
- UNCONFIRMED: Preferred production auth/session contract for frontend API calls.

## Working Set
- Files: `apps/**`, `config/**`, `frontend/**`, `CONTINUITY*.md`
- Commands: Django verify commands and frontend `npm` validate commands

## Archived
- None.
