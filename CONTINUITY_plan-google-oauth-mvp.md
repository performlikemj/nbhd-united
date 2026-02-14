# Planning Ledger: Google OAuth MVP

Parent: CONTINUITY.md
Root: CONTINUITY.md

## Objective
Deliver the MVP value chain: Telegram bot -> read Gmail -> check Google Calendar, using self-managed Google OAuth without adding a third-party integration vendor.

## Work Breakdown
| ID | Task | Ledger | Status | Owner | Depends On |
|----|------|--------|--------|-------|------------|
| 1 | OAuth hardening + identity enrichment (state checks, provider email capture, callback error mapping) | CONTINUITY_google-oauth-hardening.md | completed | codex | - |
| 2 | Automated token refresh scheduling (QStash cron task + failure state transitions) | CONTINUITY_google-oauth-refresh-scheduling.md | completed | codex | 1 |
| 3 | Token retrieval + access boundary (read tokens from Key Vault safely for runtime use) | CONTINUITY_google-oauth-token-boundary.md | completed | codex | 1 |
| 4 | Runtime capability wiring (Gmail read + Calendar lookup path consumed by assistant runtime) | CONTINUITY_google-runtime-capability-wiring.md | in-progress | codex | 2, 3 |
| 5 | End-to-end validation (integration tests + conversational happy-path + revocation/expired-token paths) | (not created) | planned | codex | 4 |
| 6 | Production rollout checklist (env config, consent screen verification, staged release, observability) | (not created) | planned | codex | 5 |

## Delegation Rules
- Create child task ledgers only when execution starts.
- Keep control-plane OAuth tasks separate from runtime capability wiring.
- Child task rollups append here first, then roll up to CONTINUITY.md.

## Dependency Graph
1 -> 2  
1 -> 3  
2,3 -> 4 -> 5 -> 6

## Collected Rollups
- [2026-02-10] Baseline assessment: OAuth authorize/callback/token storage already exists; scheduled refresh and runtime consumption are not wired.
- [2026-02-10] Task 1 complete: provider-bound OAuth state validation, provider email enrichment, callback error mapping, and new view tests (`apps.integrations` + related suite passing).
- [2026-02-10] Task 2 complete: scheduled refresh task implemented + cron registration + token loading helper; integration suites passing.
- [2026-02-10] Task 3 planned in detail with a dedicated execution ledger (`CONTINUITY_google-oauth-token-boundary.md`).
- [2026-02-10] Task 3 Step 1 complete: credential broker implemented with typed errors/on-demand refresh and passing integration suites.
- [2026-02-10] Task 3 Step 2 complete: internal auth helper + setting/env wiring + container env injection and passing broader suite.
- [2026-02-11] Task 3 Step 3 complete: malformed token payload guards, null-expiry refresh eligibility, OAuth authorize secret precheck, replay-safe OAuth state nonce handling, and expanded test coverage (29/79 passing suites).
- [2026-02-11] Task 4 started: created execution ledger `CONTINUITY_google-runtime-capability-wiring.md` and began endpoint scaffold design.
- [2026-02-11] Task 4 milestone: internal runtime Gmail/Calendar endpoints scaffolded (`runtime_views.py`, `google_api.py`, URL wiring, runtime endpoint tests) with suites passing (36/86).
- [2026-02-11] Task 4 plan refinement: prioritize read-only usefulness (message detail + action-item extraction + OpenClaw runtime wiring), defer write actions.
- [2026-02-11] Task 4 Phase A/B complete: read-only OAuth scope defaults + legacy-scope compatibility checks, new Gmail message-detail and Calendar free/busy runtime endpoints, and passing suites (41/91).
- [2026-02-11] OpenClaw upstream analysis complete: plugin-based in-process extension model is suitable for Phase C; no official Gmail/Calendar read plugin found, so custom plugin implementation is required.
- [2026-02-11] Task 4 execution plan finalized: four-track plan defined for per-tenant config injection, native OpenClaw plugin implementation, assistant action-item contract, and Docker/e2e rollout validation.
- [2026-02-11] Runtime-image provenance check: this repo CI/CD (`.github/workflows/ci-cd.yml`) builds only `django:*`; no `nbhd-openclaw:latest` build pipeline found locally. Task 4 Track 2 remains blocked pending runtime source owner/path confirmation.
- [2026-02-11] Task 4 Track 1 Step 3 complete: container runtime env injection added in `apps/orchestrator/azure_client.py` (`NBHD_API_BASE_URL`, `OPENCLAW_CONFIG_JSON`) with payload assertion coverage in `apps/orchestrator/test_azure_client.py`.
- [2026-02-11] Azure discovery update: accessible ACRs (`acrloanarmy`, `nbhdunited`, `sautairegistry`) contain no `nbhd-openclaw` repository and no ACR tasks; only Django Container App is deployed in `rg-nbhd-prod`.
- [2026-02-11] Validation: `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.orchestrator` -> 12 passed.
- [2026-02-11] Task 4 Track 1 Step 4 complete: env-gated plugin/tool wiring added to `generate_openclaw_config` (`plugins.allow`, `plugins.entries.<id>.enabled`, `tools.alsoAllow=[\"group:plugins\"]`) controlled by `OPENCLAW_GOOGLE_PLUGIN_ID`.
- [2026-02-11] Validation refresh: `DATABASE_URL=sqlite:////tmp/nbhd_united_test.sqlite3 AZURE_MOCK=true .venv/bin/python manage.py test apps.orchestrator` -> 14 passed.
- [2026-02-11] Runtime build path created in-repo: `Dockerfile.openclaw`, `runtime/openclaw/entrypoint.sh`, and plugin scaffold `runtime/openclaw/plugins/nbhd-google-tools`.
- [2026-02-11] CI/CD updated to build/push `${REGISTRY}.azurecr.io/nbhd-openclaw:{sha,latest}` on `main`.
- [2026-02-11] Syntax checks passed for runtime scaffold (`node --check` plugin, `sh -n` entrypoint).
- [2026-02-11] OpenClaw runtime compatibility fix: gateway config defaults updated to schema-valid values (`bind: lan`, removed invalid `gateway.auth.mode=none`) and orchestrator tests passing (15).
- [2026-02-11] Local Docker validation passed: runtime image builds and `plugins list` shows `nbhd-google-tools` as loaded.
- [2026-02-11] ACR publish verified for runtime image: `nbhdunited.azurecr.io/nbhd-openclaw` now exists with tags `c637198` and `latest`.
- [2026-02-11] Runtime provisioning secrets moved to Key Vault-first references in container payloads (`keyVaultUrl` + identity) with env fallback mode retained for local/dev; orchestrator tests passing (16).
- [2026-02-14] OAuth reliability patch: Google authorize/callback flow now uses process-local nonce fallback when cache backend is unavailable, preventing Redis/cache outages from hard-500ing integration connects; added view regressions and validated `apps.integrations` suite (54 passed).
- [2026-02-14] Production rollout completed for OAuth/Redis stability:
  - Key Vault `redis-url` rotated to `rediss://...` (Upstash TLS).
  - Container App `nbhd-django-westus2` normalized to canonical `REDIS_URL` secret ref (removed optional `UPSTASH_REDIS_URL` runtime env + secret).
  - Deployed Django image `nbhdunited.azurecr.io/django:manual-20260214150632-4bde901` (ready revision `nbhd-django-westus2--0000055`; rollback checkpoint `--0000052` / `49d33163e4ee0c2104f958785f05fbfb6cae4140`).
  - Added scheduled query alert `nbhd-integrations-authorize-redis-alert` for authorize-path 500 and Redis interruption signatures.
  - Post-deploy checks: unauth authorize smoke returns `403` (not `500`), and log query on revision `--0000055` shows `hits=0` for targeted error signatures.

## Decisions
- Build Google OAuth in-house for MVP; defer Composio unless scope expands to many providers.
- Scope MVP integrations to Gmail + Google Calendar.
- Reuse existing QStash cron infrastructure for refresh automation.
- Use Django proxy boundary for MVP Gmail/Calendar access; revisit direct runtime token handling after stabilization.
- Prioritize read-only intelligence (inbox visibility + action-item extraction) before any write capabilities.
- Implement Phase C runtime wiring as a native OpenClaw plugin/tool package that calls NBHD Django runtime endpoints (defer MCP unless multi-provider scope expands).

## Blockers
- @dependency: Google Cloud consent screen + redirect URI setup must be completed per environment.
- @blocked-by: production env/secret rollout not yet applied end-to-end for Key Vault-backed runtime secrets and plugin env vars.

## State
- Done: Gap assessment and practical timeline (4-8 dev days) completed.
- Now: Task 4 in progress; Track 1 complete, runtime image published, and Key Vault-backed secret wiring merged in control plane.
- Next: observe authenticated integration-connect traffic on revision `--0000055` and then run full runtime e2e validation with plugin-enabled tenant provisioning.
