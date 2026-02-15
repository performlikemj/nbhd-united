# Master Ledger: CONTINUITY.md

## Goal
- Stabilize NBHD United as the Django/OpenClaw control plane and scaffold the separate Next.js subscriber frontend.
- Success criteria: backend orchestration is stable and tested, and `frontend/` has a runnable starter implementing onboarding/integrations/usage/billing surfaces.

## Constraints / Assumptions
- Django repo is orchestration-only; OpenClaw remains runtime per tenant.
- External systems (Azure Container Apps, Key Vault, Stripe, Telegram) are mocked in tests where needed.
- Development uses `AZURE_MOCK=true` and local Postgres/Redis.
- Production cache backend is Upstash Redis (`REDIS_URL` / `rediss://...` for Django cache).
- Frontend remains a separate build artifact from backend deployment.

## Key Decisions
- 2026-02-08: Reconcile refactor schema drift with explicit migrations instead of interactive prompts.
- 2026-02-08: Continue work from `main`; merged and deleted `feature/openclaw-control-plane`.
- 2026-02-08: Scaffold frontend under `frontend/` using hand-authored Next.js files and offline-safe fonts.
- 2026-02-10: Plan Google OAuth MVP in-house (Gmail + Calendar first), defer Composio until broader integration scope.
- 2026-02-10: Use Django proxy access boundary for Gmail/Calendar in MVP; defer direct OpenClaw token handling to a later phase.
- 2026-02-11: Prioritize read-only assistant value (email/calendar visibility + action-item extraction), defer write actions.
- 2026-02-11: Use native OpenClaw plugin/tool integration for runtime wiring to NBHD endpoints (defer MCP-first approach for current scope).
- 2026-02-12: Implement proactive automation v1 as focused scope (`daily_brief`, `weekly_review`) with DB-backed per-automation timezone schedules and global cron evaluation.
- 2026-02-12: Implement harness-aligned agent-skills MVP via runtime header auth contract (`X-NBHD-Internal-Key`, `X-NBHD-Tenant-Id`) with backend/runtime scope only (no frontend journal UI in this phase).
- 2026-02-12: Build global Codex base skills platform in `~/.codex` (pilot-first via `nbhd-united`) using CONTINUITY-ledger governance and Wave 1 ops skills (`continuity-ledger-ops`, `skill-architecture-ops`).

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
  - Production OAuth/Redis stabilization rollout completed (2026-02-14):
    - Key Vault `redis-url` normalized to Upstash TLS (`rediss://...`) and runtime canonicalized on `REDIS_URL`.
    - Optional `UPSTASH_REDIS_URL` runtime env + container secret removed from `nbhd-django-westus2`.
    - Deployed Django image `nbhdunited.azurecr.io/django:manual-20260214150632-4bde901` (ready revision `nbhd-django-westus2--0000055`).
    - Added alert rule `nbhd-integrations-authorize-redis-alert` for authorize-path 500 and Redis interruption signatures.
    - Post-deploy log checks on revision `--0000055` show zero targeted error hits in initial window.
  - Runtime build path created in-repo for `nbhd-openclaw` (Dockerfile, entrypoint, plugin scaffold, CI build/push step).
  - Local runtime Docker validation passed (`nbhd-openclaw:local` builds; `nbhd-google-tools` plugin loads in container).
  - Runtime image published and verified in ACR (`nbhdunited.azurecr.io/nbhd-openclaw:{c637198,latest}`).
  - OpenClaw provisioning now defaults to Key Vault-backed secret references (`keyVaultUrl` + identity) for Anthropic/Telegram/internal runtime auth secrets.
  - Proactive automations v1 scaffold completed:
    - Track 1 hardening patch applied and validated (backend-gated KV role assignment + test assertions + dependency source-of-truth alignment).
    - Track 2 focused automation scope implemented (`daily_brief`, `weekly_review`) with backend app/API/scheduler/cron bridge and frontend `/automations` management page.
    - Validation complete:
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.orchestrator apps.automations apps.router` -> `Ran 52 tests`, `OK`.
      - `cd frontend && npm run lint && npm run build` -> passed.
  - Harness-aligned agent-skills MVP implementation completed (pending deploy):
    - Added `apps/journal` app (models + serializers + tests + migration).
    - Added runtime endpoints for journal entries and weekly reviews under `/api/v1/integrations/runtime/{tenant_id}/...`.
    - Extended runtime plugin to support POST JSON and added journal tools (`nbhd_journal_create_entry`, `nbhd_journal_list_entries`, `nbhd_journal_create_weekly_review`).
    - Added managed skill files under `agent-skills/` and runtime workspace sync (`skills/nbhd-managed`) in entrypoint.
    - Added architecture/runbook doc at `docs/agent-skills-architecture.md`.
    - Validation complete:
      - `node --test runtime/openclaw/plugins/nbhd-google-tools/index.test.mjs` -> `3 passed`.
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.journal apps.integrations apps.orchestrator` -> `Ran 73 tests`, `OK`.
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps/` -> `Ran 153 tests`, `OK`.
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py makemigrations --check --dry-run` -> `No changes detected`.
  - Global Codex skills platform bootstrap completed in `~/.codex`:
    - Created codex-home continuity ledgers (`~/.codex/CONTINUITY*.md`) and Wave 1 skills:
      - `continuity-ledger-ops`
      - `skill-architecture-ops`
    - Added skills platform guide docs under `~/.codex/docs/skills-platform/`.
    - Generated pilot metrics artifact:
      - `~/.codex/metrics/skills-platform/pilot-nbhd-united.jsonl` (20 scenarios).
    - Added repo pilot planning ledger:
      - `CONTINUITY_plan-codex-skills-pilot.md`.
  - Secure branch integration implementation completed (`CONTINUITY_secure-branch-integration.md`):
    - Implemented doc-aligned OpenClaw tool policy (`tools.allow` / `tools.deny` / `tools.elevated`) and plugin gating.
    - Added usage dashboard endpoints/services with hardened query validation and settings-driven subscription pricing.
    - Added user timezone model/profile/middleware integration with runtime header forwarding and active-tenant config refresh hook.
    - Added OpenClaw config conformance smoke script + CI job.
    - Validation complete:
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_fullcheck.sqlite3 AZURE_MOCK=true PREVIEW_ACCESS_KEY= .venv/bin/python manage.py makemigrations --check --dry-run` -> `No changes detected`.
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_fullcheck.sqlite3 AZURE_MOCK=true PREVIEW_ACCESS_KEY= .venv/bin/python manage.py migrate --noinput` -> passed.
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_fullcheck.sqlite3 AZURE_MOCK=true PREVIEW_ACCESS_KEY= .venv/bin/python manage.py check` -> passed.
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_fullcheck.sqlite3 AZURE_MOCK=true PREVIEW_ACCESS_KEY= COMPOSIO_API_KEY= COMPOSIO_GMAIL_AUTH_CONFIG_ID= COMPOSIO_GCAL_AUTH_CONFIG_ID= .venv/bin/python manage.py test apps/` -> `Ran 238 tests`, `OK`.
      - `cd frontend && npm ci && npm run lint && npm run build` -> passed.
  - Secure branch integration rollout completed (all squash-merged to `main`):
    - PR1 `codex/agent-tool-policy` -> merged (`eccaa687e7df6c906aefb771ef0d90c8cf3772ce`)
    - PR2 `codex/usage-dashboard-clean` -> merged (`49d33163e4ee0c2104f958785f05fbfb6cae4140`)
    - PR3 `codex/user-timezone-clean` -> merged (`4bde901ddcfbf7f2370ee1911216ae88aead7c15`)
- Now:
  - Task 4 runtime capability wiring in progress (runtime image published; pending production plugin env + Key Vault secret rollout and full e2e).
  - Harness-aligned skills MVP is ready for commit/deploy rollout (`CONTINUITY_agent-skills-harness-mvp.md`).
  - Global Codex skills platform build-out in progress (`CONTINUITY_codex-global-skills-platform.md` + `~/.codex/CONTINUITY*.md`).
  - Secure branch integration wave is complete; monitor post-merge deploy/runtime telemetry (`CONTINUITY_secure-branch-integration.md`).
- Next:
  - Observe authenticated Gmail/Calendar integration-connect traffic on revision `nbhd-django-westus2--0000055` and confirm continued zero authorize-path 500s.
  - Apply remaining production plugin env vars + Key Vault secret mapping for runtime provisioning, then run full provisioned-tenant e2e.
  - Wire operational QStash schedule for `run_due_automations` cadence in deployed environment and monitor initial run telemetry.
  - Deploy backend + runtime image for journal skills and run pilot tenant e2e smoke.
  - Complete `~/.codex` Wave 1 skills + docs + pilot metrics capture for `nbhd-united`.
  - Observe pilot behavior for one week, then activate `yardtalk` and `netwatcher` if gates remain stable.

## Task Map
```text
CONTINUITY.md
  ├─ CONTINUITY_secure-branch-integration.md (@owner:codex, in-progress)
  ├─ CONTINUITY_codex-global-skills-platform.md (@owner:codex, in-progress)
  ├─ CONTINUITY_plan-codex-skills-pilot.md (@owner:codex, in-progress)
  ├─ CONTINUITY_agent-skills-harness-mvp.md (@owner:codex, in-progress)
  ├─ CONTINUITY_automations-proactive-scaffold.md (@owner:codex, completed)
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
- `CONTINUITY_secure-branch-integration.md`
- `CONTINUITY_codex-global-skills-platform.md`
- `CONTINUITY_plan-codex-skills-pilot.md`
- `CONTINUITY_agent-skills-harness-mvp.md`
- `CONTINUITY_automations-proactive-scaffold.md`
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
- @handoff-to:CONTINUITY_codex-global-skills-platform.md - bootstrap `~/.codex` continuity ledgers, Wave 1 ops skills, docs, and pilot scenario metrics.
- @handoff-to:CONTINUITY_plan-codex-skills-pilot.md - monitor pilot behavior and finalize rollout recommendation for `yardtalk` and `netwatcher`.
- @handoff-to:CONTINUITY_agent-skills-harness-mvp.md - implementation complete; ready for commit/deploy rollout.
- @handoff-to:CONTINUITY_automations-proactive-scaffold.md - implementation complete; ready for commit/deploy.
- @handoff-to:CONTINUITY_google-runtime-capability-wiring.md - Task 4 active; implement Track 1/2 (tenant config injection + OpenClaw plugin runtime wiring).
- @handoff-to:CONTINUITY_secure-branch-integration.md - rollout complete on `main`; continue deploy/runtime observation, keep `agent-skills-architecture` deferred.
- @blocked-by: production Key Vault + Container App env rollout still pending before runtime e2e.

## Trivial Log
- [2026-02-08] Created initial `CONTINUITY.md` bootstrap ledger.
- [2026-02-10] Assessed Google OAuth (Gmail/Calendar) MVP readiness; identified gaps in scheduled token refresh wiring and runtime data-access consumption.
- [2026-02-11] Validated Stripe checkout -> signed webhook completion smoke path locally (tenant transitioned to ACTIVE with mock container provisioning).
- [2026-02-11] Validated Stripe checkout -> webhook -> provisioning path inside Docker (`web` + `postgres`) with `AZURE_MOCK=true`.
- [2026-02-14] Wired Telegram webhook success path to call `record_usage(...)` for message-level usage increments.
- [2026-02-14] Added `/review` reviewer entry-point link on `/login` to support Stripe audit navigation.
- [2026-02-15] Fixed journal runtime tests to use `NBHD_INTERNAL_API_KEY` in overrides for consistent internal auth checks.

## Open Questions (UNCONFIRMED)
- UNCONFIRMED: Preferred production auth/session contract for frontend API calls.

## Working Set
- Files: `apps/**`, `runtime/**`, `templates/**`, `agent-skills/**`, `docs/**`, `config/**`, `frontend/**`, `CONTINUITY*.md`
- Commands: Django verify commands and frontend `npm` validate commands

## Archived
- None.
