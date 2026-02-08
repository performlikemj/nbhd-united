# Task Ledger: OpenClaw Control Plane Hardening

Parent: `CONTINUITY.md`
Root: `CONTINUITY.md`
Related: `apps/tenants`, `apps/billing`, `apps/orchestrator`, `apps/router`, `apps/integrations`, `config/settings`
Owner: `codex`

## Goal
- Get the Django control plane fully working after refactor and expand test coverage for critical orchestration paths.

## Constraints / Assumptions
- Use local Postgres/Redis and `DJANGO_SETTINGS_MODULE=config.settings.development`.
- Use project interpreter at `.venv/bin/python`.
- Keep external dependencies mocked in tests (Azure, Stripe, Key Vault, Telegram).
- Prefer minimal, safe diffs aligned with current architecture.

## Key Decisions
- 2026-02-08: Reconcile schema drift with explicit hand-authored migrations (`tenants.0002`, `billing.0003`, `integrations.0003`) instead of interactive `makemigrations` defaults.
- 2026-02-08: Add service hardening for webhook idempotency, rate limiting, duplicate tenant prevention, and integration token refresh.

## State
- Done:
  - Bootstrapped local dependencies (`docker compose up -d postgres redis`) and aligned environment to `.venv`.
  - Applied migration reconciliation and removed model/migration drift (`makemigrations --check --dry-run` clean).
  - Hardened services in tenants, billing, integrations, router, and settings defaults.
  - Expanded test coverage from 19 to 47 tests across lifecycle, Stripe webhook flows, router forwarding/rate limits, orchestration failures, and integration refresh.
  - Full suite green: `.venv/bin/python manage.py test`.
- Now:
  - Prepare commits and push to `feature/openclaw-control-plane`.
- Next:
  - Execute git commit(s) and push branch.

## Links
- Upstream:
  - `CONTINUITY.md`
- Downstream:
  - None.
- Related:
  - `README.md`
  - `docker-compose.yml`

## Open Questions (UNCONFIRMED)
- UNCONFIRMED: Remote push credentials/access for `feature/openclaw-control-plane` in this environment.

## Working Set
- Files:
  - `apps/tenants/services.py`
  - `apps/billing/services.py`
  - `apps/billing/views.py`
  - `apps/integrations/services.py`
  - `apps/router/services.py`
  - `apps/router/views.py`
  - `config/settings/base.py`
  - `apps/tenants/migrations/0002_reconcile_refactor.py`
  - `apps/billing/migrations/0003_reconcile_refactor.py`
  - `apps/integrations/migrations/0003_reconcile_refactor.py`
  - `apps/tenants/test_services.py`
  - `apps/billing/test_services.py`
  - `apps/billing/test_webhooks.py`
  - `apps/orchestrator/test_services.py`
  - `apps/router/test_services.py`
  - `apps/router/test_views.py`
  - `apps/integrations/test_services.py`
- Commands:
  - `.venv/bin/python manage.py migrate`
  - `.venv/bin/python manage.py makemigrations --check --dry-run`
  - `.venv/bin/python manage.py test apps/`
  - `.venv/bin/python manage.py test`
  - `.venv/bin/python manage.py check`

## Notes
- Baseline failure was schema mismatch (`users.telegram_username` missing) plus migration drift in `tenants`, `billing`, and `integrations`.
- External systems are mocked at service boundaries in tests:
  - Azure Container Apps/Identity via patched orchestrator functions
  - Stripe webhook verification/dispatch via patched `stripe.Webhook.construct_event`
  - Telegram forwarding via patched async forwarding path
  - OAuth token refresh via patched `httpx.post`
  - Key Vault writes/deletes via patched integration service helpers
