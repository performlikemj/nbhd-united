# Task Ledger: Proactive Automations Scaffold

Parent: CONTINUITY.md
Root: CONTINUITY.md
Related: apps/automations, apps/cron, apps/router, frontend/app/automations
Owner: codex

## Goal
- Implement the approved patch plan in order:
  - Track 1: harden orchestrator provisioning around secret backend behavior and dependency source-of-truth.
  - Track 2: scaffold focused v1 proactive automations (`daily_brief`, `weekly_review`) with tenant-scoped API/UI and scheduled dispatch into tenant OpenClaw.

## Constraints / Assumptions
- Keep trigger path as synthetic Telegram update forwarded directly to tenant OpenClaw using existing router forwarding service.
- Keep heartbeat disabled; scheduling runs from one global cron tick evaluating DB due work.
- v1 scope is intentionally narrow (`daily_brief`, `weekly_review`) with strict tenant limits.
- Preserve existing behavior for non-automation flows and keep changes additive.

## Key Decisions
- 2026-02-12: Store timezone per automation record and compute `next_run_at` from timezone + schedule on create/update/run lifecycle.
- 2026-02-12: Use one global cron task (`run_due_automations`) for cadence, with per-tenant/per-automation schedules in DB/UI.
- 2026-02-12: Keep automation dispatch path internal (`forward_to_openclaw`) and bypass public webhook endpoints.

## State
- Done:
  - Track 1 complete:
    - `apps/orchestrator/services.py` now gates `assign_key_vault_role(...)` on `OPENCLAW_CONTAINER_SECRET_BACKEND == "keyvault"`.
    - `apps/orchestrator/test_services.py` explicitly covers keyvault-call and env-skip behaviors.
    - `apps/orchestrator/test_azure_client.py` explicitly asserts idempotent 409 path still calls Azure role assignment API once and does not raise.
    - `requirements.in` updated to include `azure-mgmt-authorization` (source-of-truth aligned with `requirements.txt`).
  - Track 2 backend scaffold complete:
    - Added `apps/automations` app wiring, models, migration, scheduler, tasks, serializers, views, URLs, and tests.
    - Added cron task mapping: `run_due_automations -> apps.automations.tasks.run_due_automations_task`.
    - Added synthetic Telegram dispatch bridge via existing `apps.router.services.forward_to_openclaw`.
  - Track 2 frontend scaffold complete:
    - Added `/frontend/app/automations/page.tsx`.
    - Added Automations navigation + Home quick action link.
    - Added automation types/API/query hooks for list/create/update/pause/resume/delete/manual-run and run history.
- Now:
  - Roll up implementation outcomes for review/commit.
- Next:
  - Optional: tune retry/backoff policy for failed scheduled runs if product behavior needs retries instead of schedule-advance on failure.

## Links
- Upstream:
  - CONTINUITY.md
- Downstream:
  - None.
- Related:
  - CONTINUITY_google-runtime-capability-wiring.md

## Open questions (UNCONFIRMED)
- None currently blocking implementation.

## Working set
- Files:
  - apps/orchestrator/services.py
  - apps/orchestrator/test_services.py
  - apps/orchestrator/test_azure_client.py
  - requirements.in
  - requirements.txt
  - apps/automations/**
  - config/settings/base.py
  - config/urls.py
  - apps/cron/views.py
  - frontend/app/automations/page.tsx
  - frontend/components/app-shell.tsx
  - frontend/app/page.tsx
  - frontend/lib/types.ts
  - frontend/lib/api.ts
  - frontend/lib/queries.ts
- Commands:
  - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.orchestrator apps.automations apps.router`
  - `cd frontend && npm run lint && npm run build`

## Notes (short, factual)
- This workstream is large and cross-cutting, so it is tracked in a dedicated ledger instead of the trivial log.
- Validation outcomes:
  - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.orchestrator apps.automations apps.router` -> passed (`Ran 52 tests`, `OK`).
  - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 .venv/bin/python manage.py makemigrations --check --dry-run` -> `No changes detected`.
  - `cd frontend && npm run lint && npm run build` -> lint/build passed, `/automations` route included in build output.
