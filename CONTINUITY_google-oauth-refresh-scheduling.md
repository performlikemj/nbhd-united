# Task Ledger: Google OAuth Refresh Scheduling

Parent: CONTINUITY_plan-google-oauth-mvp.md
Root: CONTINUITY.md
Related: apps/integrations/tasks.py, apps/cron/views.py, apps/integrations/services.py, apps/integrations/test_tasks.py
Owner: codex

## Goal
Implement Task 2 from the Google OAuth MVP plan: scheduled refresh of expiring OAuth integrations using existing QStash cron execution flow.

## Constraints / Assumptions
- Reuse existing `apps.cron` task trigger infrastructure.
- Keep refresh behavior tenant-scoped and provider-aware.
- Use safe status transitions (`active` -> `expired`/`error`) on failures.

## Key decisions
- Add `refresh_expiring_integrations_task` under `apps.integrations.tasks` and register it in cron `TASK_MAP`.
- Refresh only active integrations nearing expiry.
- Use provider-group credentials from Django settings.

## State
- Done:
  - Task ledger created.
  - Added `refresh_expiring_integrations_task` in `apps/integrations/tasks.py`.
  - Registered refresh task in cron `TASK_MAP` (`apps/cron/views.py`).
  - Added Key Vault token load helper with mock-mode support in `apps/integrations/services.py`.
  - Added task tests in `apps/integrations/test_tasks.py`.
  - Ran validation:
    - `.venv/bin/python manage.py test apps.integrations` -> 13 passed.
    - `.venv/bin/python manage.py test apps.integrations apps.router apps.tenants.tests_telegram` -> 52 passed.
- Now:
  - Task 2 implementation complete; handoff ready for Task 3.
- Next:
  - Start Task 3 token retrieval/access-boundary work.

## Links
- Upstream: CONTINUITY_plan-google-oauth-mvp.md
- Downstream: (none yet)
- Related: CONTINUITY_google-oauth-hardening.md

## Open questions (UNCONFIRMED)
- UNCONFIRMED: Desired refresh lead time before expiry (currently targeting a conservative pre-expiry window).

## Working set
- Commands:
  - `.venv/bin/python manage.py test apps.integrations`
  - `.venv/bin/python manage.py test apps.integrations apps.router apps.tenants.tests_telegram`

## Notes
- Started implementation on 2026-02-10.
