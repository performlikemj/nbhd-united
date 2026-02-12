## Parent
- `CONTINUITY.md`

## Root
- `CONTINUITY.md`

## Related
- `CONTINUITY_google-runtime-capability-wiring.md`
- `CONTINUITY_automations-proactive-scaffold.md`

## Owner
- @owner:codex

## Goal
- Implement harness-aligned agent-skills MVP:
  - Persisted journal/review models and runtime APIs.
  - Runtime plugin tools for journal create/list and weekly review create.
  - Managed skill files copied into runtime and seeded into mounted workspace.
  - Docs and tests aligned to current runtime auth and URL contracts.

## Constraints / Assumptions
- Keep runtime auth contract as internal headers (`X-NBHD-Internal-Key`, `X-NBHD-Tenant-Id`).
- Keep routes under `/api/v1/integrations/runtime/{tenant_id}/...`.
- No frontend journal/review UI in this phase.
- Existing Gmail/Calendar runtime tools must remain behaviorally compatible.
- Skills are NBHD-authored only in MVP (no user-authored skill loading).

## Key Decisions
- 2026-02-12: Create dedicated `apps/journal` app instead of overloading integrations models.
- 2026-02-12: Reuse integrations runtime auth helper and view style for new journal/review endpoints.
- 2026-02-12: Seed managed skills into `/home/node/.openclaw/workspace/skills/nbhd-managed` at runtime startup to handle mounted workspace volumes.

## State
- Done:
  - Reviewed remote branch `origin/feature/agent-skills-architecture` and identified docs/runtime contract mismatches.
  - Locked scope and auth decisions with user.
  - Added `apps/journal` app (models, serializers, tests, migration) and registered it in Django settings.
  - Added runtime endpoints:
    - `POST/GET /api/v1/integrations/runtime/{tenant_id}/journal-entries/`
    - `POST /api/v1/integrations/runtime/{tenant_id}/weekly-reviews/`
  - Extended runtime plugin transport to support POST JSON and added journal tools:
    - `nbhd_journal_create_entry`
    - `nbhd_journal_list_entries`
    - `nbhd_journal_create_weekly_review`
  - Added plugin unit tests (`node --test`).
  - Added managed skill files under `agent-skills/` and runtime sync into mounted workspace path `skills/nbhd-managed`.
  - Updated runtime managed `AGENTS.md` guidance and added `docs/agent-skills-architecture.md`.
  - Validation completed:
    - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py makemigrations journal`
    - `node --test runtime/openclaw/plugins/nbhd-google-tools/index.test.mjs`
    - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.journal apps.integrations apps.orchestrator`
    - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps/`
    - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py makemigrations --check --dry-run`
- Now:
  - Implementation complete; pending commit and deployment rollout.
- Next:
  - Deploy backend changes.
  - Deploy runtime image changes.
  - Restart pilot tenant and run e2e smoke before broader rollout.

## Links
- Upstream:
  - `CONTINUITY.md`
- Downstream:
  - None yet.
- Related:
  - `apps/integrations/runtime_views.py`
  - `runtime/openclaw/plugins/nbhd-google-tools/index.js`
  - `runtime/openclaw/entrypoint.sh`
  - `Dockerfile.openclaw`

## Open Questions (UNCONFIRMED)
- None blocking implementation.

## Working Set
- Files:
  - `apps/journal/**`
  - `apps/integrations/runtime_views.py`
  - `apps/integrations/urls.py`
  - `runtime/openclaw/plugins/nbhd-google-tools/index.js`
  - `runtime/openclaw/entrypoint.sh`
  - `Dockerfile.openclaw`
  - `templates/openclaw/AGENTS.md`
  - `agent-skills/**`
  - `docs/agent-skills-architecture.md`
  - `config/settings/base.py`
- Commands:
  - `python manage.py makemigrations journal`
  - `python manage.py test apps.journal apps.integrations apps.orchestrator`

## Notes
- Keep diffs minimal and additive to reduce risk to current runtime capability wiring.
- Runtime transport contract remains header-auth + tenant-scoped route prefix; docs and skills were aligned to this contract.
