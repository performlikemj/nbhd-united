# Task Ledger: Remediation Hardening Implementation

Parent: `CONTINUITY_plan-remediation-hardening.md`
Root: `CONTINUITY.md`
Related: `apps/billing`, `apps/router`, `apps/tenants`, `frontend/app`, `frontend/lib`, `config/settings`, `README.md`
Owner: `codex`

## Goal
- Implement review-driven security/reliability remediations across billing, webhook auth, onboarding behavior, logout/session revocation, and routing copy configuration.

## Constraints / Assumptions
- Keep changes minimal, safe, and backward-compatible where possible.
- Preserve existing endpoint contracts unless hardening requires explicit validation.
- Extend tests for each behavior change.
- Validate with backend tests and frontend lint/build.

## Key decisions
- 2026-02-10: Execute P1 fixes first (Stripe API key wiring and Telegram webhook secret enforcement) before lower-priority remediations.
- 2026-02-10: Use SimpleJWT blacklist for logout revocation instead of custom token revocation storage.

## State
- Done:
  - Created implementation ledger for remediation execution.
  - Added Stripe API key mode selection and fail-fast config guards for checkout/portal endpoints.
  - Hardened Telegram webhook auth: fail closed when secret missing and constant-time secret comparison.
  - Fixed onboarding auto-tenant creation side-effect by moving mutation into `useEffect` with duplicate-attempt guard.
  - Implemented logout refresh-token revocation with SimpleJWT blacklist and frontend logout API call.
  - Replaced hardcoded onboarding domain with `FRONTEND_URL` in Telegram fallback message.
  - Updated docs/env examples for webhook secret and frontend/api URL configuration.
  - Verification passed:
    - `.venv/bin/python manage.py test apps` (83 tests, pass)
    - `.venv/bin/python manage.py check` (pass)
    - `npm --prefix frontend run lint` (pass)
    - `npm --prefix frontend run build` (pass)
- Now:
  - Ready for user review.
- Next:
  - Apply migrations in runtime environments to create JWT blacklist tables (`python manage.py migrate`).
  - User to set Stripe test/live keys and Telegram webhook secret in environment.

## Links
- Upstream:
  - `CONTINUITY_plan-remediation-hardening.md`
  - `CONTINUITY.md`
- Downstream:
  - None
- Related:
  - `apps/billing/views.py`
  - `apps/router/views.py`
  - `apps/router/services.py`
  - `apps/tenants/auth_views.py`
  - `config/settings/base.py`
  - `frontend/lib/api.ts`
  - `frontend/components/app-shell.tsx`
  - `frontend/app/onboarding/page.tsx`

## Open questions (UNCONFIRMED)
- None.

## Working set
- Commands:
  - `.venv/bin/python manage.py test apps`
  - `npm --prefix frontend run lint`
  - `npm --prefix frontend run build`

## Notes
- This workstream executes items from `CONTINUITY_plan-remediation-hardening.md` IDs 1-6.
- Added backend tests for:
  - Stripe API-key wiring and misconfiguration behavior
  - Telegram secret mismatch/missing config behavior
  - Logout refresh token blacklisting behavior
- `showmigrations token_blacklist` failed in this environment due unavailable local Postgres (`localhost:5432` operation not permitted), but remediation validation still passed via Django test database runs.
