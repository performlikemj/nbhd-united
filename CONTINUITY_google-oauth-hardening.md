# Task Ledger: Google OAuth Hardening

Parent: CONTINUITY_plan-google-oauth-mvp.md
Root: CONTINUITY.md
Related: apps/integrations/views.py, apps/integrations/services.py, apps/integrations/test_views.py
Owner: codex

## Goal
Implement Task 1 from the Google OAuth MVP plan: harden OAuth callback/state handling and enrich integration records with provider email metadata.

## Constraints / Assumptions
- Keep existing OAuth endpoint contracts stable for frontend (`connected` and `error` query params).
- No new infrastructure dependencies for Task 1.
- Preserve current tenant scoping and auth model.

## Key decisions
- Include provider in signed OAuth state payload and enforce provider match in callback.
- Capture provider email at connect-time when available from provider APIs.
- Use explicit callback error codes for predictable frontend behavior.
- For MVP, keep Gmail/Calendar data access behind Django proxy boundaries (do not grant direct Google-token handling to OpenClaw runtime yet).

## State
- Done:
  - Task ledger created.
  - Added provider-bound OAuth state helpers and callback validation in `apps/integrations/views.py`.
  - Added provider-email enrichment (`fetch_provider_email`) and pass-through to `connect_integration`.
  - Added explicit callback error mapping for unknown provider, invalid state, missing params, not configured, exchange/callback failures.
  - Expanded Google OAuth scopes to include `openid` and `email` in `apps/integrations/services.py`.
  - Added view tests in `apps/integrations/test_views.py`.
  - Ran validation:
    - `.venv/bin/python manage.py test apps.integrations` -> 9 passed.
    - `.venv/bin/python manage.py test apps.integrations apps.router apps.tenants.tests_telegram` -> 48 passed.
- Now:
  - Task 1 implementation complete and rolled up.
- Next:
  - No further work in this ledger.

## Links
- Upstream: CONTINUITY_plan-google-oauth-mvp.md
- Downstream: (none yet)
- Related: CONTINUITY.md

## Open questions (UNCONFIRMED)
- UNCONFIRMED: Whether to keep OAuth callback `error` query values stable long-term or migrate to a typed frontend error contract.

## Working set
- Commands:
  - `.venv/bin/python manage.py test apps.integrations`
  - `.venv/bin/python manage.py test apps.integrations apps.router apps.tenants.tests_telegram`

## Notes
- Started implementation on 2026-02-10.
