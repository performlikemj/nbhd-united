# Task Ledger: Google Runtime Capability Wiring

Parent: CONTINUITY_plan-google-oauth-mvp.md
Root: CONTINUITY.md
Related: apps/integrations/services.py, apps/integrations/internal_auth.py, apps/integrations/views.py, apps/integrations/urls.py
Owner: codex

## Goal
Execute Task 4 from the Google OAuth MVP plan: expose internal Django endpoints that let the assistant runtime read Gmail and check Google Calendar using the Task 3 token boundary (no refresh token exposure).

## Constraints / Assumptions
- Runtime calls must be authenticated with internal shared-key + tenant scoping.
- Endpoints are internal-only and should never leak provider refresh/access tokens in response payloads.
- Keep external/public integration endpoints stable.
- Prefer incremental rollout: scaffold one Gmail read path and one Calendar availability/list path first.
- For the current phase, exclude write actions (send/reply/archive/create) and prioritize read + insight extraction.

## Key decisions
- Reuse Task 3 broker (`get_valid_provider_access_token`) for all runtime Google API calls.
- Keep provider HTTP calls in a dedicated service helper to simplify retries and tests.
- Return compact, assistant-friendly normalized payloads rather than raw Google API responses.
- Expand Gmail support with message detail retrieval so assistant can infer action items from body context.
- Keep action execution manual by user; assistant only recommends action items in this phase.
- Implement Phase C as a native OpenClaw plugin (not MCP-first) that calls NBHD internal Django runtime endpoints.

## State
- Done:
  - Task 4 ledger created and scoped.
  - Scaffolded internal runtime endpoint module `apps/integrations/runtime_views.py` with:
    - `RuntimeGmailMessagesView` (`GET /api/v1/integrations/runtime/<tenant_id>/gmail/messages/`)
    - `RuntimeCalendarEventsView` (`GET /api/v1/integrations/runtime/<tenant_id>/google-calendar/events/`)
    - Shared internal auth + tenant scope enforcement via `X-NBHD-Internal-Key` and `X-NBHD-Tenant-Id`.
    - Stable broker/internal/provider error mapping to deterministic JSON codes + statuses.
  - Added Google provider helper module `apps/integrations/google_api.py` for normalized Gmail metadata and Calendar event retrieval.
  - Wired runtime URLs in `apps/integrations/urls.py`.
  - Added endpoint tests in `apps/integrations/test_runtime_views.py`.
  - Validation:
    - `.venv/bin/python manage.py test apps.integrations` -> 36 passed.
    - `.venv/bin/python manage.py test apps.integrations apps.orchestrator apps.router apps.tenants.tests_telegram` -> 86 passed.
  - Phase A complete (read-only defaults + compatibility):
    - Updated Google OAuth defaults to `gmail.readonly` and `calendar.readonly` in `apps/integrations/services.py`.
    - Added compatibility-aware scope guard in broker (`IntegrationScopeError`) accepting legacy broader scopes (`gmail.modify`, `calendar`) for already-connected tenants.
  - Phase B complete (richer read endpoints):
    - Added Gmail message detail extraction in `apps/integrations/google_api.py`:
      - body decoding (plain/html), normalized headers, label ids, and thread context.
    - Added Google Calendar free/busy retrieval in `apps/integrations/google_api.py`.
    - Added new runtime endpoints in `apps/integrations/runtime_views.py` and `apps/integrations/urls.py`:
      - `GET /api/v1/integrations/runtime/<tenant_id>/gmail/messages/<message_id>/`
      - `GET /api/v1/integrations/runtime/<tenant_id>/google-calendar/freebusy/`
    - Added runtime + scope tests in:
      - `apps/integrations/test_runtime_views.py`
      - `apps/integrations/test_services.py`
      - `apps/integrations/test_views.py`
  - Validation:
    - `.venv/bin/python manage.py test apps.integrations` -> 41 passed.
    - `.venv/bin/python manage.py test apps.integrations apps.orchestrator apps.router apps.tenants.tests_telegram` -> 91 passed.
  - OpenClaw upstream architecture analyzed from source checkout (`openclaw/openclaw`):
    - Confirmed in-process plugin model (`openclaw.plugin.json` + `api.registerTool(...)`) is first-class for runtime extension.
    - Confirmed optional plugin tools are gated by tool allowlists (agent/global).
    - Confirmed plugin-config JSON schema and strict plugin id validation are enforced at config validation time.
    - Confirmed no official built-in Gmail/Calendar read plugin in extensions; custom plugin is required for NBHD Django proxy integration.
  - Track 1 Step 3 complete (control-plane config/env injection):
    - Updated `apps/orchestrator/azure_client.py` to pass:
      - `NBHD_API_BASE_URL` runtime env value,
      - `OPENCLAW_CONFIG_JSON` runtime env value (tenant-scoped generated config).
    - Added `apps/orchestrator/test_azure_client.py` to assert container-app payload wiring.
  - Track 1 Step 4 complete (config generator plugin/tool wiring, env-gated):
    - Added optional setting `OPENCLAW_GOOGLE_PLUGIN_ID` in `config/settings/base.py` and `.env.example`.
    - Updated `apps/orchestrator/config_generator.py`:
      - when plugin id is configured, include `plugins.allow` and `plugins.entries.<id>.enabled`,
      - when plugin id is configured, add `tools.alsoAllow = [\"group:plugins\"]`.
    - Added config generator tests in `apps/orchestrator/tests.py` for plugin-enabled and plugin-omitted paths.
  - Runtime discovery findings (Azure):
    - `az acr list` returned `acrloanarmy`, `nbhdunited`, `sautairegistry`.
    - `az acr repository list` showed no `nbhd-openclaw` repository in any accessible ACR.
    - `az acr task list` returned no build tasks in accessible ACRs.
    - `az containerapp list -g rg-nbhd-prod` shows only `nbhd-django-westus2` deployed.
  - Runtime image creation path added in this repo:
    - Added `Dockerfile.openclaw` to build `nbhd-openclaw` image from official OpenClaw npm package.
    - Added runtime entrypoint `runtime/openclaw/entrypoint.sh` that materializes `OPENCLAW_CONFIG_JSON` into `~/.openclaw/openclaw.json`.
    - Added plugin scaffold `runtime/openclaw/plugins/nbhd-google-tools` with:
      - `openclaw.plugin.json`
      - `package.json`
      - `index.js` (read-only Gmail/Calendar tool wrappers over internal NBHD endpoints).
    - Updated CI/CD workflow `.github/workflows/ci-cd.yml` to build/push:
      - `${REGISTRY}.azurecr.io/nbhd-openclaw:${GITHUB_SHA}`
      - `${REGISTRY}.azurecr.io/nbhd-openclaw:latest`
  - Track 1 plugin-path completion:
    - Added `OPENCLAW_GOOGLE_PLUGIN_PATH` setting and env sample.
    - Updated config generator plugin wiring to include `plugins.load.paths` when plugin id is configured.
  - OpenClaw gateway schema compatibility fix:
    - Updated `apps/orchestrator/config_generator.py` gateway defaults to:
      - `bind: "lan"` (instead of invalid `0.0.0.0`)
      - removed invalid `gateway.auth.mode: "none"` block.
    - Added test coverage in `apps/orchestrator/tests.py`.
  - Local runtime validation (Docker):
    - `docker build -f Dockerfile.openclaw -t nbhd-openclaw:local .` -> passed.
    - `docker run ... nbhd-openclaw:local --help` with schema-compatible config -> passed.
    - `docker run ... nbhd-openclaw:local plugins list` -> `nbhd-google-tools` loaded from `/opt/nbhd/plugins/nbhd-google-tools/index.js`.
  - ACR publish verified:
    - `az acr repository list -n nbhdunited` includes `nbhd-openclaw`.
    - `az acr repository show-tags -n nbhdunited --repository nbhd-openclaw` shows `c637198` and `latest`.
  - Key Vault-backed runtime secret wiring added for container provisioning:
    - `apps/orchestrator/azure_client.py` now builds Container Apps secrets as Key Vault references by default (`keyVaultUrl` + tenant user-assigned `identity`), with explicit fallback mode `OPENCLAW_CONTAINER_SECRET_BACKEND=env`.
    - Added settings/env wiring:
      - `OPENCLAW_CONTAINER_SECRET_BACKEND`
      - `AZURE_KV_SECRET_ANTHROPIC_API_KEY`
      - `AZURE_KV_SECRET_TELEGRAM_BOT_TOKEN`
      - `AZURE_KV_SECRET_NBHD_INTERNAL_API_KEY`
    - Updated tests and validation:
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.orchestrator` -> 16 passed.
  - OAuth authorize/callback cache resilience fix for Google providers:
    - Updated `apps/integrations/views.py` to keep a process-local one-time nonce fallback store and use it when cache backend read/write fails.
    - Prevented hard 500 responses when cache/Redis is unavailable during OAuth state creation or callback verification.
    - Added regression tests in `apps/integrations/test_views.py`:
      - authorize succeeds when `cache.set` fails,
      - callback succeeds once and rejects replay when cache get/delete fails.
    - Validation:
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true PREVIEW_ACCESS_KEY= .venv/bin/python manage.py test apps.integrations.test_views` -> 8 passed.
      - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true PREVIEW_ACCESS_KEY= .venv/bin/python manage.py test apps.integrations` -> 54 passed.
  - Production Redis normalization + OAuth stabilization rollout completed (2026-02-14):
    - Rollback checkpoint captured before rollout:
      - revision: `nbhd-django-westus2--0000052`
      - image: `nbhdunited.azurecr.io/django:49d33163e4ee0c2104f958785f05fbfb6cae4140`
    - Updated Key Vault secret `redis-url` to Upstash TLS form (`rediss://...`) and confirmed secret version refresh.
    - Removed optional `UPSTASH_REDIS_URL` runtime env + `upstash-redis-url` container secret from `nbhd-django-westus2` (kept canonical `REDIS_URL -> secretRef redis-url`).
    - Built/pushed/deployed Django image with OAuth fallback patch:
      - deployed image: `nbhdunited.azurecr.io/django:manual-20260214150632-4bde901`
      - ready revision: `nbhd-django-westus2--0000055`
    - Added operational alert rule:
      - `nbhd-integrations-authorize-redis-alert` (`Microsoft.Insights/scheduledQueryRules`)
      - query watches integration authorize 500s + Redis connection interruption signatures.
    - Post-deploy validation:
      - smoke calls to `/api/v1/integrations/authorize/gmail/` and `/api/v1/integrations/authorize/google-calendar/` return `403` (not `500`) when unauthenticated.
      - Log Analytics check on revision `nbhd-django-westus2--0000055`:
        - authorize 500 / Redis interruption signatures in last 20m -> `hits=0`.
- Now:
  - Backend Phase A/B completed; Track 1 complete and runtime image pipeline scaffold is now in-repo.
  - Track 2 plugin scaffold is implemented and locally validated in Docker.
  - Runtime provisioning path is now Key Vault-first for OpenClaw container secrets.
  - Production cache wiring is normalized to Upstash TLS via `REDIS_URL`, and OAuth authorize endpoints are stable in post-deploy checks.
- Next:
  - Observe production logs for 60 minutes during real authenticated integration-connect traffic and confirm no authorize-path regressions.
  - Keep rollback target (`nbhd-django-westus2--0000052` / `49d33163e4ee0c2104f958785f05fbfb6cae4140`) available if production behavior regresses.
  - Run full authenticated Gmail/Google Calendar connect smoke in UI and validate expected redirect/error handling semantics.
  - Add assistant-level action-item extraction contract and tests (read-only recommendations only).

## Execution plan (tenant config + plugin)
### Track 1: Per-tenant OpenClaw config injection
1. Runtime source ownership and build path
   - Locate and confirm the source repo/path that produces `nbhd-openclaw:latest`.
   - Record owner and deploy trigger path in this ledger.
2. Config materialization strategy
   - Decide one path for `openclaw.json` injection:
     - Preferred: container startup writes `/home/node/.openclaw/openclaw.json` from `OPENCLAW_CONFIG_JSON` env var.
     - Fallback: mount config file from storage and point OpenClaw to it.
   - Keep one canonical mechanism across local + Azure.
3. Control-plane wiring
   - Update `apps/orchestrator/azure_client.py` to pass tenant config through the chosen mechanism.
   - Ensure runtime env includes:
     - `NBHD_API_BASE_URL` (internal Django URL),
     - `NBHD_TENANT_ID`,
     - `NBHD_INTERNAL_API_KEY` (secret ref only).
4. Plugin/tool enablement in generated config
   - Update `apps/orchestrator/config_generator.py` to include plugin/tool registration in `openclaw.json` for each tenant.
   - Keep tool allowlist read-only (no write actions).
5. Verification
   - Extend orchestrator tests to assert config injection fields and env wiring.
   - Smoke check provisioning in `AZURE_MOCK=true` path.

### Track 2: Native OpenClaw plugin (read-only Google capability)
1. Plugin scaffold in OpenClaw runtime source
   - Add plugin package with `openclaw.plugin.json` and runtime entry.
   - Register tools:
     - `gmail_list_messages`
     - `gmail_get_message_detail`
     - `calendar_list_events`
     - `calendar_get_freebusy`
2. Django internal endpoint client
   - Plugin HTTP client calls existing NBHD runtime endpoints:
     - `/api/v1/integrations/runtime/<tenant_id>/gmail/messages/`
     - `/api/v1/integrations/runtime/<tenant_id>/gmail/messages/<message_id>/`
     - `/api/v1/integrations/runtime/<tenant_id>/google-calendar/events/`
     - `/api/v1/integrations/runtime/<tenant_id>/google-calendar/freebusy/`
   - Send headers:
     - `X-NBHD-Internal-Key: NBHD_INTERNAL_API_KEY`
     - `X-NBHD-Tenant-Id: NBHD_TENANT_ID`
3. Error contract normalization
   - Map internal API errors (`integration_not_connected`, `integration_refresh_failed`, etc.) to clear tool errors so agent can explain next step.
4. Runtime tests
   - Add plugin unit tests for request shaping, auth headers, and error mapping.
   - Add one local smoke command that invokes plugin tools against running Django API.

### Track 3: Assistant usefulness contract (read-only)
1. Response shape
   - Standardize assistant output as:
     - Inbox Brief (new/unread/high-priority),
     - Calendar Brief (today + next 24h constraints),
     - Action Items (explicit user-follow-up tasks with source email id).
2. Prompt/tool-use policy
   - Update runtime agent instructions to:
     - fetch data first,
     - never claim unseen messages,
     - include uncertainty if data missing,
     - avoid write actions.
3. Contract tests
   - Add tests (or scripted golden checks) to ensure output includes message references and actionable bullets.

### Track 4: End-to-end validation and rollout
1. Local docker validation
   - Bring up Django + dependencies + runtime image.
   - Validate: OAuth-connected tenant -> plugin tools return Gmail/Calendar data.
2. Failure-path validation
   - Validate integration missing, token expired/refresh failed, and provider failure paths.
3. Rollout gate
   - Require passing suites + smoke checklist before enabling for non-test tenants.

## Links
- Upstream: CONTINUITY_plan-google-oauth-mvp.md
- Downstream: CONTINUITY_plan-google-oauth-mvp.md (Task 5 e2e validation)
- Related: CONTINUITY_google-oauth-token-boundary.md

## Open questions (UNCONFIRMED)
- UNCONFIRMED: Exact assistant contract for Gmail response shape (messages list fields and max defaults).
- UNCONFIRMED: Whether production deployment currently uses `rg-nbhd-prod` and `nbhd-django-westus2` (recent `az containerapp` calls encountered DNS/resource lookup instability from this environment).
- UNCONFIRMED: Whether router -> OpenClaw webhook forwarding requires additional gateway auth settings once runtime runs with current defaults.

## Working set
- Candidate files:
  - apps/integrations/runtime_views.py
  - apps/integrations/google_api.py
  - apps/integrations/services.py
  - apps/integrations/urls.py
  - apps/integrations/test_runtime_views.py
  - apps/integrations/test_services.py
  - apps/integrations/test_views.py
  - apps/orchestrator/azure_client.py
  - apps/orchestrator/test_azure_client.py
  - Dockerfile.openclaw
  - runtime/openclaw/entrypoint.sh
  - runtime/openclaw/plugins/nbhd-google-tools/*
- Candidate commands:
  - `.venv/bin/python manage.py test apps.integrations`
  - `.venv/bin/python manage.py test apps.integrations apps.orchestrator apps.router apps.tenants.tests_telegram`
  - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.orchestrator`
  - `docker build -f Dockerfile.openclaw -t nbhd-openclaw:local .`

## Notes (short, factual)
- Use existing integration status transitions from Task 3 for not-connected/expired/error paths.
- Internal runtime auth should fail closed with deterministic status and JSON error codes.
- Verified on 2026-02-11:
  - `.github/workflows/ci-cd.yml` previously had only Django image build/push (`${REGISTRY}.azurecr.io/django:*`).
  - `apps/orchestrator/azure_client.py` references runtime image `${AZURE_ACR_SERVER}/nbhd-openclaw:latest`.
  - Added local runtime Dockerfile and workflow build step for `nbhd-openclaw`.
  - `az acr repository list` in `acrloanarmy`, `nbhdunited`, and `sautairegistry` shows no `nbhd-openclaw` repository.
  - `az acr task list` shows no ACR build tasks in accessible registries.
  - `az containerapp list -g rg-nbhd-prod` shows only Django app deployment.
  - `docker-compose.yml` maps Postgres host port `5432:5432`, which can conflict with local Postgres from other projects.
  - Validation:
    - `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.orchestrator` -> 15 passed.
    - `node --check runtime/openclaw/plugins/nbhd-google-tools/index.js` -> passed.
    - `sh -n runtime/openclaw/entrypoint.sh` -> passed.
    - `docker build -f Dockerfile.openclaw -t nbhd-openclaw:local .` -> passed.
    - `docker run ... nbhd-openclaw:local plugins list` -> `nbhd-google-tools` loaded.

## Immediate unblock steps
1. Trigger build/push on `main` (or run manual Docker push) so `nbhd-openclaw` appears in `nbhdunited` ACR.
2. Set production/staging env:
   - `OPENCLAW_GOOGLE_PLUGIN_ID=nbhd-google-tools`
   - `OPENCLAW_GOOGLE_PLUGIN_PATH=/opt/nbhd/plugins/nbhd-google-tools`
3. Run runtime smoke:
   - provision a test tenant,
   - verify plugin tools can read Gmail/Calendar via internal endpoints.

## Practical timeline (remaining)
- Day 0.5: run first `nbhd-openclaw` build/push and verify ACR tags.
- Day 1: run Track 2 runtime smoke validation and tighten plugin error handling based on observed responses.
- Day 2: implement Track 3 (assistant response contract + prompt/tool policy + checks).
- Day 3: execute Track 4 e2e validation in Docker and finalize rollout checklist.
