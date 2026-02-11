# Task Ledger: Google OAuth Token Boundary

Parent: CONTINUITY_plan-google-oauth-mvp.md
Root: CONTINUITY.md
Related: apps/integrations/services.py, apps/integrations/views.py, apps/cron/views.py, apps/orchestrator/azure_client.py, config/settings/base.py
Owner: codex

## Goal
Plan and execute Task 3 from the Google OAuth MVP plan: build a safe token-retrieval boundary that keeps refresh/access tokens inside Django while enabling runtime-facing Google capability APIs in Task 4.

## Constraints / Assumptions
- MVP boundary is Django proxy first; OpenClaw runtime should not receive refresh tokens.
- Keep existing frontend integration endpoints stable.
- Reuse current tenant model + integration status fields (`active`, `expired`, `error`, `revoked`).
- Avoid storing raw OAuth tokens in logs, responses, or persistent debug artifacts.

## Key decisions
- Introduce a dedicated credential broker service layer that returns a short-lived usable access token only to Django internal callers.
- Refresh on-demand if token is near expiry, using existing `refresh_integration_tokens` and provider credentials.
- Add internal runtime auth for future proxy endpoints (`Task 4`) using explicit server-to-server secret + tenant scoping checks.
- Treat missing/invalid token material as status transitions (`active` -> `expired` or `error`) with deterministic error types.

## State
- Done:
  - Task 3 execution plan drafted and sequenced.
  - Step 1 credential broker implemented in `apps/integrations/services.py`:
    - Added `get_valid_provider_access_token(...)` with on-demand refresh behavior.
    - Added typed broker error classes and `ProviderAccessToken` return type.
    - Added provider credential resolver (`get_provider_client_credentials`).
    - Added status transitions for invalid/missing token material and refresh failures.
  - Refactored refresh scheduling task to reuse shared provider credential resolver.
  - Added Step 1 test coverage in `apps/integrations/test_services.py`.
  - Step 2 boundary-auth groundwork implemented:
    - Added internal auth helper (`apps/integrations/internal_auth.py`) with shared-key + tenant scope validation.
    - Added `NBHD_INTERNAL_API_KEY` setting in `config/settings/base.py` and `.env.example`.
    - Injected `NBHD_INTERNAL_API_KEY` into tenant runtime container env in `apps/orchestrator/azure_client.py`.
    - Added internal auth tests in `apps/integrations/test_internal_auth.py`.
  - Step 3 failure semantics + boundary hardening implemented:
    - Normalized malformed token payload handling in broker and Key Vault loader (`dict` validation before field access).
    - Updated refresh scheduler to include unknown-expiry integrations (`token_expires_at is null`) and guard malformed token payloads.
    - Enforced OAuth authorize precondition to require both provider client ID and secret.
    - Added one-time signed OAuth state nonces to prevent callback state replay.
  - Validation:
    - `.venv/bin/python manage.py test apps.integrations` -> 29 passed.
    - `.venv/bin/python manage.py test apps.integrations apps.orchestrator apps.router apps.tenants.tests_telegram` -> 79 passed.
- Now:
  - Task 3 implementation complete; preparing handoff into Task 4 runtime capability wiring.
- Next:
  - Create Task 4 ledger and scaffold internal runtime-facing Gmail/Calendar proxy endpoints using Task 3 broker/auth boundary.

## Links
- Upstream: CONTINUITY_plan-google-oauth-mvp.md
- Downstream: (not created yet) Task 4 runtime wiring ledger
- Related: CONTINUITY_google-oauth-refresh-scheduling.md

## Open questions (UNCONFIRMED)
- UNCONFIRMED: Final header contract for internal runtime auth (`X-NBHD-Internal-Key` + tenant header naming) before production cutover.

## Working set
- Candidate files:
  - apps/integrations/services.py
  - apps/integrations/internal_auth.py (new)
  - apps/integrations/runtime_views.py (new or existing views module extension)
  - apps/integrations/urls.py
  - config/settings/base.py
  - apps/orchestrator/azure_client.py
  - apps/integrations/test_services.py
  - apps/integrations/test_runtime_views.py (new)
- Candidate commands:
  - `.venv/bin/python manage.py test apps.integrations`
  - `.venv/bin/python manage.py test apps.integrations apps.router apps.tenants.tests_telegram`

## Notes
- Step 1 (Credential Broker):
  - Add typed broker method (e.g., `get_valid_provider_access_token(tenant, provider)`).
  - Load token payload from Key Vault; reject if integration not active/revoked.
  - If access token missing or near expiry, refresh using stored refresh token.
  - Return only access token + expires metadata to caller; never return refresh token.
- Step 2 (Boundary Contract):
  - Define internal auth requirement for runtime-to-Django calls.
  - Add env var for internal key in settings and inject into container env in provisioning path.
  - Document and enforce tenant scoping in boundary checks.
- Step 3 (Failure Semantics):
  - Normalize broker exceptions: not connected, expired/revoked, provider misconfig, refresh failed.
  - Map to stable HTTP status codes for internal callers (used by Task 4 endpoints).
- Step 4 (Tests + Auditability):
  - Unit tests for broker success/refresh/failure transitions.
  - Security tests ensuring refresh token never appears in responses/log payloads.
  - Endpoint auth tests for boundary contract.
- Definition of Done for Task 3:
  - Broker implementation complete + tests passing.
  - Internal boundary auth in place and validated.
  - Task 4 can consume a stable, token-safe internal API without touching Key Vault directly.
