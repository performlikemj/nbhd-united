# Secrets, Identity & Supply Chain — Security Audit

Scope: where credentials live, how they reach the control plane and the per-tenant
runtime, which bearer secrets function as authentication, where they can leak, what
happens to them on teardown, and the third-party dependency surface (Python +
npm + container base images). Read first: [Identity, Auth, Billing &
Integrations](../reference/identity-billing-integrations.md) (auth classes, trust
model), [Infrastructure & Deployment](../reference/infrastructure-and-deployment.md)
(KV↔managed-identity plumbing, env-var contract, CI/CD, image build), and
[Invariants](../agents/invariants.md) (#11 secrets discipline, #10 KV
`identityref:` gotcha). This doc does not re-derive facts already established
there — it audits them and adds what those docs don't cover: full secret-type
inventory, leak-surface verification, deprovision lifecycle, and supply chain.

All values below are secret **names**/**locations**, never contents.

---

## 1. Key Vault (`kv-nbhd-prod`) secret inventory by type

| Type | Naming pattern | Scope | Written by | Read by | Cite |
|---|---|---|---|---|---|
| Platform-shared LLM keys | `anthropic-api-key`, `openai-api-key`, `openrouter-api-key`, `brave-api-key` | Fleet-wide — every tenant MI gets read RBAC on the same four secrets | Ops (out-of-band) | Every `oc-*` container via `secretRef` | `apps/orchestrator/azure_client.py:145` (`DEFAULT_TENANT_KV_SECRETS`) |
| Platform shared runtime token | `nbhd-internal-api-key` (backs both `NBHD_INTERNAL_API_KEY` and `OPENCLAW_GATEWAY_TOKEN`) | Fleet-wide, legacy fallback path | Ops | All containers (gateway auth) + Django | `azure_client.py:756-779` |
| Platform channel creds | `telegram-bot-token`, `telegram-webhook-secret`, `line-channel-access-token`, `line-channel-secret` | Fleet-wide (single shared Telegram bot, single LINE channel) | Ops | Django control plane only (poller/webhook), not containers | `config/settings/base.py:405-422` |
| Platform admin key | `openrouter-management-key` | Fleet-wide, elevated (creates/revokes **every** tenant's OpenRouter sub-key) | Ops | Django (`apps/billing/openrouter_admin.py:83`) only, never a container | `openrouter_admin.py:83` |
| Platform config templates | `soul-md`, `agents-md` | Fleet-wide seed content (not credentials, but KV-hosted config — see §6 survivability) | Ops | Django, seeded to every tenant's share at first boot | `config/settings/base.py:503-508` |
| Per-tenant internal key | `tenant-<uuid>-internal-key` | One per tenant | Django at provision time | That tenant's container only (`X-NBHD-Internal-Key`) | `azure_client.py:282` |
| Per-tenant OAuth tokens | `<key_vault_prefix>-<provider>-token` (Google, Sautai) | One per (tenant, provider) | Django (`store_tokens_in_key_vault`) | Django only — containers never read OAuth tokens from KV directly (see §5 for the Google exception) | `apps/integrations/services.py:353,379` |
| Per-tenant BYO LLM token | `<key_vault_prefix>-byo-<provider>-<mode>` | One per (tenant, provider) | Django (`apps/byo_models/services.py:115`) | That tenant's container, via `CLAUDE_CODE_OAUTH_TOKEN` env `secretRef` | `apps/byo_models/services.py:31,49` |
| Per-tenant OpenRouter sub-key | `<key_vault_prefix>-openrouter-key` | One per tenant | Django, mirrors BYO naming | That tenant's container | `apps/billing/openrouter_admin.py:317-324` |
| Control-plane infra creds | `DATABASE_URL`, `ADMIN_DATABASE_URL`, `REDIS_URL`, `QSTASH_TOKEN`/signing keys, `STRIPE_*`, `DJSTRIPE_WEBHOOK_SECRET`, `SENTRY_DSN`, storage account key | Fleet-wide, Django-only | Ops | Django only (env vars on `nbhd-django-westus2`), **not** exposed to any `oc-*` container | [infra doc §"Selected contract"](../reference/infrastructure-and-deployment.md) |

Postgres never holds a raw secret value except one documented exception: `Tenant.internal_api_key` is stored **raw** in the `tenants` row itself (`apps/tenants/models.py:332`, `CharField`, no hashing) — not just referenced. The KV copy (`tenant-<uuid>-internal-key`) is the runtime source of truth for the container side; the DB copy is what `validate_internal_runtime_request` compares against on every internal call (`apps/integrations/internal_auth.py:123`). A Postgres compromise (e.g. a broad SQL-injection or an admin-role credential leak) therefore yields every tenant's internal key directly, no KV access needed — narrower than a full KV compromise but still a full-fleet runtime-impersonation credential set sitting in one table.

BYO tokens are the one credential class that structurally **cannot** leak from Postgres: `BYOCredential` stores only `key_vault_secret_name` + metadata (`identity-billing-integrations.md` §6).

---

## 2. Managed-identity model

Each tenant gets a user-assigned identity `mi-nbhd-<prefix>` (`azure_client.py:99-118`) granted exactly two roles at provision time:

1. **Key Vault Secrets User**, scoped **per-secret** (not vault-wide) — the four `DEFAULT_TENANT_KV_SECRETS` plus that tenant's own `tenant-<uuid>-internal-key` (`azure_client.py:139-186`, `assign_key_vault_role`).
2. **AcrPull** on `nbhdunited.azurecr.io`.

This bounds the blast radius of a stolen MI token (e.g. via IMDS SSRF from inside a compromised/prompt-injected container) to: the four shared platform LLM keys (same ones every other tenant's container can already read — no incremental tenant-to-tenant exposure there) plus that tenant's own internal key (which the container legitimately holds anyway). It does **not** grant read access to another tenant's BYO token, OAuth token, or OpenRouter sub-key secret — those aren't in the per-secret grant list, so cross-tenant secret exfiltration via a stolen MI token is not possible through this path (per-tenant KV secret names are also unpredictable only insofar as `key_vault_prefix` embeds the tenant UUID — see §4 for the container-native path that *is* readable, i.e., the file share).

Gotcha (already an invariant, restated for completeness): the Container Apps `secrets[].identity` and `identityref:` fields must reference the **`mi-nbhd-` name**, not the `oc-` container name — using the wrong identity name is a silent misconfiguration, not a hard failure, per [invariant #10](../agents/invariants.md).

**Fallback-to-plaintext gap** (already flagged as [med] in the infra reference doc, restated here because it is a secrets-discipline issue): `_build_container_secret` (`azure_client.py:71-96`) falls back to an inline `{"name","value"}` Container App secret — i.e., the raw value stored directly on the Container App resource instead of a KV reference — whenever the vault name, KV secret name, or identity id is empty, logging only a `WARNING`. A provisioning misconfiguration (e.g. a blank `AZURE_KEY_VAULT_NAME`) silently downgrades every secret for that container from "KV reference, per-secret RBAC" to "plaintext on the resource, readable by anyone with `Microsoft.App/containerApps/read` + secret-list RBAC on the Container App itself" — a materially different exposure surface with no deploy-time signal.

---

## 3. Two secret delivery paths

| Path | Mechanism | Verifies at |
|---|---|---|
| Django control plane | Plain env vars on `nbhd-django-westus2`, sourced from KV **out-of-band** (not via `keyvaultref:` in Django itself — `django-environ` just reads `os.environ`) | Container App configuration (outside this repo) |
| Per-tenant `oc-*` containers | Container Apps native `keyVaultUrl` + `identity` secret references, resolved by the platform at container start, exposed to the process as env vars via `secretRef` | `azure_client.py:84-96, 858` |

Practical consequence for this audit: Django's secret set is **not self-documenting from the repo** — the authoritative list is whatever is configured on the Container App, "mirrored nowhere in-repo except by convention" (already flagged [high] in the infra doc as the split-brain env-var contract). For this security review, that means an auditor cannot get a complete, current list of what's actually deployed to Django by reading source; `.env.example` and `base.py` are best-effort documentation, not ground truth.

**Env vars are frozen at container create time** for `oc-*` containers — `apply_single_tenant_config_task`/`update_container_image` change only the image, not `env`/`NODE_OPTIONS` (`azure_client.py:869-881`). Practically: rotating a KV secret's **value** propagates on next container restart (the reference resolves fresh), but changing which secret **name** a container references, or adding a new `secretRef` env var, requires a one-shot ops update per existing tenant — a rotation runbook has to account for this or silently miss already-provisioned tenants.

---

## 4. Bearer secrets that ARE the authentication

Two credentials gate entire request classes by simple equality/membership, not a signed token — these are the highest-value targets in the system because possession alone is authorization.

### `Tenant.internal_api_key` / `tenant-<uuid>-internal-key`

- Validated with `secrets.compare_digest` (constant-time) — `internal_auth.py:178`.
- Since Phase 1d (2026-06-22) it is the **only** accepted credential for the runtime callback surface (~50 endpoints); the legacy shared-global fallback was removed after a 7-day zero-hit audit (`internal_auth.py:163-167`).
- **No rotation mechanism found in code.** Generated once at provisioning; there is no management command, admin action, or scheduled task that regenerates a tenant's `internal_api_key` and re-pushes both the Postgres row and the KV secret. Rotation, per [invariant #11](../agents/invariants.md), is manual via the `/rotate-keys` skill. A leaked per-tenant key (e.g. exfiltrated by a prompt-injected agent reading its own `process.env`, which is legitimate access to its own key) is valid indefinitely unless an operator notices and rotates it by hand.
- Blast radius of a leaked per-tenant key is **already scoped to one tenant** by design (that is the entire point of Phase 1d) — service-role RLS is set only for the matching `tenant_id`.

### `DEPLOY_SECRET`

- Gates the CI→Django `/api/cron/*` post-deploy endpoints (force-reseed-crons, bump-pending-configs, register-system-crons, and the extraction backfill in `apps/journal/extraction_views.py:37`) via the `X-Deploy-Secret` header.
- **Every comparison site uses plain `==`**, not a constant-time compare: `apps/cron/views.py:681,1266,1326,1521` and `apps/journal/extraction_views.py:39` all do `provided == deploy_secret`. This is inconsistent with the rest of the codebase's own established pattern — the internal-key path uses `secrets.compare_digest` and the PKCE exchange uses `hmac.compare_digest` (`identity-billing-integrations.md` §3). A timing side-channel against a single shared secret used across five endpoints is a low-probability but real, easily-fixed gap.
- Lives as a GitHub Actions repo secret (consumed at `ci-cd.yml` post-deploy `curl` steps) and as the Django env var of the same name — two independent copies that must be kept in sync manually; no rotation automation found.

### The shared fallback that still exists

`NBHD_INTERNAL_API_KEY` / `OPENCLAW_GATEWAY_TOKEN` (KV secret `nbhd-internal-api-key`) is still provisioned to **every** container as a platform-shared value (`azure_client.py:756-779`) even though Phase 1d made the per-tenant key the only credential the internal-auth validator accepts. It's not dead — it still backs `OPENCLAW_GATEWAY_TOKEN` (the OpenClaw gateway's own inbound auth, a different trust boundary than Django's runtime callback validator) and is the value that would need rotating fleet-wide (all containers, one restart wave) if it ever leaked, versus a per-tenant key rotation which touches one tenant. Already flagged [low] in the infra reference doc; noted here because it's the single highest-blast-radius rotatable secret in the fleet after the four platform LLM keys.

---

## 5. Where secrets can leak

### 5a. Logs — CONFIRMED, currently unmitigated: Telegram bot token in Log Analytics

`production.py`'s `LOGGING` dict wires exactly one filter, `RedactBYOPasteBody`, onto the `console` handler (`config/settings/production.py:125-149`). **`RedactTelegramToken` does not exist in this codebase** — `apps/router/logging_filters.py` is absent from `main`, and grepping for `RedactTelegramToken`/`scrub_telegram_token` across `apps/` returns nothing.

The exposure this leaves open: Telegram's Bot API embeds the token in the URL **path** (`https://api.telegram.org/bot<id>:<secret>/getUpdates`), not a header. All Telegram calls in this repo go through raw `httpx` (`apps/router/poller.py:14,150` — no `python-telegram-bot` client is actually used despite it being a pinned dependency, see §7), and `httpx` logs every outbound request at `INFO` (`HTTP Request: GET <url> "..."`). The production root logger is `INFO` with a single `console` handler and no `httpx` logger level override anywhere in `config/settings/*.py`. Root cause and full detail already captured in prior incident notes; verified independently here against current `main`:

- The token rides in `record.args` as an `httpx.URL` object, not a plain string, so a naive string-match filter (like `RedactBYOPasteBody`'s own approach) would not have caught it even if adapted — a purpose-built filter is required.
- **LINE is not affected** by this class of leak — its channel token travels as an `Authorization: Bearer` header, which `httpx` does not log at `INFO`.
- Sentry is not a secondary exposure path here: `SENTRY_LOGS_LEVEL` defaults to `WARNING` (`base.py:625`), and `httpx` INFO chatter is below that threshold — but this means Sentry filtering out this stream is incidental, not a designed control, and offers no protection if anyone raises log verbosity for debugging.
- This affects **every** Telegram call, not just polling — `getUpdates`, `sendMessage`, `sendPhoto`, `getFile`, file downloads, `editMessageText`, `answerCallbackQuery` all carry the token in the URL and all get logged.

**Practical exposure:** the shared bot token (one token, fleet-wide, since there is a single central poller — `poller.py:1`) streams into Azure Log Analytics in plaintext on an ongoing basis. Anyone with read access to `ContainerAppConsoleLogs_CL` for `nbhd-django-westus2`, or anyone running `az containerapp logs show` against it (including an unredacted pull into an agent/debugging session), sees the live credential. Because Telegram bot tokens don't expire on their own, a stored-log exposure is a standing compromise until the token is rotated at BotFather — rotation is an operator action with fleet-wide ingress impact, independent of whether the logging filter is ever shipped. **This is scoped as [high] and open** — verified absent from `main` at the time of this audit, not merely a historical incident.

### 5b. File share — CONFIRMED [high] (verified per the lead's original finding, blast radius assessed here)

`_write_gws_credentials_to_file_share` (`apps/integrations/services.py:637-703`) writes `gws-credentials.json` to the tenant's SMB share containing `client_id`, `client_secret`, and `refresh_token` as plaintext JSON, called from `connect_integration` (`services.py:781`) whenever a tenant connects (or reconnects) Google Workspace. Two separate exposure vectors compound here:

1. **Per-tenant refresh token.** Scopes granted are `gmail.modify`, `calendar` (full, not `.readonly`), `drive.file`, `tasks` (`services.py:284-291`). `gmail.modify` in particular grants read, send, delete, and label/filter modification on the user's entire mailbox — not a narrow scope. A refresh token doesn't expire on its own; anything with share read access (including the LLM agent process running in that same container, which is the exact threat model a prompt-injection attack targets) has durable access to the user's Gmail, Calendar, and Tasks for as long as the token is valid, with no re-authentication checkpoint.
2. **Platform-wide `client_secret` duplicated per tenant.** The same write embeds `GOOGLE_OAUTH_CLIENT_SECRET` — one platform-wide value — into every connected tenant's share. This means the platform's OAuth client secret exists in as many plaintext copies as there are GWS-connected tenants, not one. Compromising any single tenant's share leaks a credential usable in confused-deputy / consent-phishing scenarios against the app's own OAuth client identity, independent of that tenant's own refresh token.

The write also bypasses the `_put_share_file`/`sanitize_share_text` chokepoint ([invariant #2](../agents/invariants.md)) — every other text write to the share goes through it (`azure_client.py:427-489, 532, 593`); this one hand-rolls a `ShareFileClient.upload_file` call directly (`services.py:685-702`), so it gets none of the control-byte sanitization the invariant exists to guarantee (low incremental risk here since the payload is JSON built server-side, not user input, but it's an unexplained exception to a documented chokepoint).

**Mitigating factor:** disconnect calls `_delete_gws_credentials_from_file_share` (`services.py:705`), so the file does not outlive an explicit disconnect — the exposure window is "as long as the integration stays connected," not "forever regardless of state."

**Fix directions** (assessed for feasibility against this codebase's shape):
- **Short-lived access tokens fetched on demand** is the strongest fix: have the `gws` CLI invocation go through a thin wrapper (mirroring `claude-with-token.sh`'s pattern for the BYO Claude CLI, `Dockerfile.openclaw:91`) that calls back to Django's internal-auth-gated runtime surface for a fresh access token at call time, never persisting a refresh token to the share at all. This is more work (a new runtime endpoint + wrapper script) but eliminates the standing-credential-on-share problem entirely and is consistent with how BYO credentials are already handled (Django never re-exposes the raw token; the container only ever sees a short-lived, purpose-scoped value).
- **KV-ref injection** (inject the refresh token as a container-level `secretRef` env var instead of a share file, matching the BYO/`CLAUDE_CODE_OAUTH_TOKEN` pattern) is a smaller change but doesn't fully close the gap — env vars are still readable by the same in-container LLM process (`process.env`), and per §3, env vars are frozen at container create time, so a reconnect/token-refresh would need the same one-shot-ops-update handling BYO already has to work around.
- Either fix should also stop double-embedding `client_secret` per tenant — the `gws` CLI's own token-refresh flow needs it, but it could be resolved from the shared platform KV secret rather than duplicated onto every share.

### 5c. Sentry

`send_default_pii=False` is set (`base.py:618-620`) — no request bodies, cookies, emails, or client IPs attach to events by default. `before_send`/`before_send_log` scrub BYO paste bodies as a documented second line of defense (`base.py:621-624`) mirroring `RedactBYOPasteBody`. **These hooks do not scrub the Telegram-token-in-URL pattern** — not a live exposure today only because `SENTRY_LOGS_LEVEL=WARNING` keeps `httpx` INFO chatter below the forwarding threshold (§5a); if that default is ever changed the token would flow to Sentry too, uncaught by the existing scrub hooks.

### 5d. In-container redaction (OpenClaw runtime)

`redact-stdout.js` (`Dockerfile.openclaw:91-101`, `runtime/openclaw/redact-stdout.js`) wraps `process.stdout`/`stderr` writes to mask known JSON content shapes and drop non-operational prose — a stdout-layer backstop distinct from the Django-side log filters, required because OpenClaw's gateway fast path skips its own console-capture redaction. This is tenant-**content** redaction (PII), not specifically a secrets filter, but it sits in the same log-egress path and is the closest thing to a Telegram-token-style backstop the container side has; it was not built to target credential-shaped strings.

---

## 6. Secret lifecycle on deprovision

`deprovision_tenant` (`apps/orchestrator/services.py:880-955`) explicitly deletes exactly two things:

1. The OpenRouter sub-key itself (`delete_sub_key`, `services.py:896-905`, best-effort — failure logs and relies on `sweep_orphan_openrouter_keys` to reap it later).
2. The OpenRouter KV secret (`_delete_secret_from_kv(tenant.openrouter_key_secret_name)`, `services.py:912-922`).

Everything else is **not** deleted by this path:

| Secret | Deleted on deprovision? | Notes |
|---|---|---|
| `tenant-<uuid>-internal-key` (KV) | **No** | `delete_managed_identity` (`azure_client.py:733-746`) only calls `user_assigned_identities.delete` — it does not touch Key Vault. The secret is orphaned in `kv-nbhd-prod` indefinitely. |
| `Tenant.internal_api_key` (Postgres) | Effectively yes | The `Tenant` row itself is marked `DELETED` and its FK-owning `User` cascade-deletes it eventually, but the KV copy above still survives independently. |
| BYO token (`<prefix>-byo-<provider>-<mode>`) | Not in this function | Deleted only via the explicit BYO-disconnect path (`apps/byo_models/services.py:156`), which a deprovisioning flow doesn't necessarily invoke first. |
| OAuth tokens (`<prefix>-<provider>-token`, Google/Sautai) | **No** | No call to a KV-delete for these in `deprovision_tenant`; only the explicit integration-disconnect path removes them, and (§5b) only the file-share copy is cleaned up there — the KV copy's deletion depends on that same disconnect flow having run. |
| `gws-credentials.json` on file share | Yes, indirectly | `delete_tenant_file_share` (`services.py:929`) removes the whole share, so this goes with it regardless of whether GWS was explicitly disconnected first. |

Net effect: a deprovisioned tenant leaves at least one, and up to several, orphaned Key Vault secrets behind — `tenant-<uuid>-internal-key` unconditionally, plus any OAuth/BYO secret the tenant never explicitly disconnected before deletion. None of these are dangerous in isolation (the identity that could read them, `mi-nbhd-<prefix>`, is deleted in the same flow, and the tenant row/container are gone so there's no live consumer), but they are permanent unaccounted-for entries in the vault with no sweep job analogous to `sweep_orphan_openrouter_keys` for the other secret types, and no accounting for someone auditing "does every KV secret map to a live tenant."

---

## 7. Supply chain

### Python (`requirements.txt`, 575 lines)

- **Fully pinned, lockfile-style.** Generated by `pip-compile --output-file=requirements.txt requirements.in` (header comment, `requirements.txt:1-4`); every one of the 167+ direct/transitive entries checked is an exact `==` pin (`django==6.0.7`, `djangorestframework==3.17.1`, `torch==2.13.0`, `litellm==1.91.0`, `dj-stripe==2.11.0`, `python-telegram-bot==22.8`, `psycopg[binary]==3.3.4`, `cryptography==46.0.5`, `requests==2.32.5`, `urllib3==2.6.3`, `pillow==12.1.1`, `certifi==2026.2.25`). No floating `>=`/unbounded ranges found. This is a real positive — reproducible builds, and CI's secret-scan + test suite runs against the exact pins that ship.
- **`python-telegram-bot==22.8` is an unused dependency.** All Telegram traffic goes through raw `httpx` (`poller.py:14`, confirmed no `import telegram` anywhere under `apps/`, and documented as deliberate in the poller's own module docstring — "no `python-telegram-bot`, all Telegram calls are raw httpx"). The package (and its transitive deps) still ships in every Django image build for no functional benefit — pure supply-chain surface with no offsetting value. Low-effort removal.
- **PII ML stack dominates image size and dependency graph.** `torch==2.13.0` (CPU wheel, installed from the PyTorch CPU index specifically to dodge the ~1 GB CUDA bundle — `Dockerfile:14-24`) + `transformers` (pinned via Dependabot `ignore` at all update levels — major/minor/patch — per `.github/dependabot.yml`, because minor bumps have broken prod imports three times: PR #447, #652, and the model swap in #695) + the DeBERTa-v3 model weights (~554 MB, `Dockerfile.pii-model`). This is the single largest chunk of both the image (2.56 GB total) and the dependency surface, and it's the one area where Dependabot is **fully muted** by policy — meaning `transformers` will not receive automated security-patch PRs at all, only manual bumps.
- **No CI-enforced vulnerability scan.** `.github/workflows/*.yml` has no `pip-audit`, `safety check`, `bandit`, `trivy`, or `snyk` step. The only supply-chain signal is Dependabot's own GitHub-native vulnerability alerts + its daily PR cadence for `pip` at repo root and `npm` at `/frontend` (`.github/dependabot.yml:1-15`), auto-merged for patch/minor via `dependabot-auto-merge.yml` (majors held for manual review, per the infra reference doc). There is no gate that blocks a merge or deploy on a known-CVE dependency being present — detection is Dependabot-alert-driven and reactive, not enforced in the pipeline that ships to prod.

### Frontend (`frontend/package.json`, resolved via `package-lock.json`)

- ~20 direct dependencies (Tailwind, TanStack Query, TipTap editor suite, Next.js, Phaser, React, Recharts, remark/react-markdown). All declared with caret (`^`) ranges; the committed lockfile pins exact resolved versions (`next` resolves to `16.2.10`, `react`/`react-dom` to `19.2.7` per `package-lock.json`), so CI installs are reproducible even though the manifest itself floats within a major.
- Dependabot majors are explicitly held for `next`, `tailwindcss`, `typescript`, `eslint`, `eslint-config-next`, `react`, `react-dom` (`.github/dependabot.yml:9-15`) — deliberate, but combined with no CI vuln-scan gate (above), a major-version security fix for any of these sits in an open PR indefinitely unless someone manually reviews and merges it.
- `@tiptap/core` is additionally pinned via an `overrides` block (`^3.25.0`) to work around a documented `ERESOLVE` lockfile conflict — a process constraint, not a security one, noted here only because it's a case where the lockfile can't be freely regenerated (`npm install` from scratch on a clean lockfile can fail) without care.
- No `npm audit`/`audit-ci` step in `frontend-test` (CI job list, `ci-cd.yml`) — same reactive-only posture as the Python side.

### Container base images

| Image | Base | Runs as | Cite |
|---|---|---|---|
| `django` (control plane) | `python:3.12-slim` | **root — no `USER` directive anywhere in `Dockerfile`** | `Dockerfile:1-38` |
| `nbhd-openclaw` (per-tenant runtime, the one that runs LLM-driven, prompt-injectable, multi-tenant workloads) | `node:22-bookworm-slim` | **non-root — `USER node`** (`Dockerfile.openclaw:91`), after an explicit `chown -R node:node` on the app dirs (`:89-90`) | `Dockerfile.openclaw:89-91` |
| `pii-model` | `python:3.12-slim` (build stage) → `FROM scratch` (final) | N/A — final layer is weights only, no runtime, no shell | `Dockerfile.pii-model:15,32` |

The Django image is the one exception to non-root hygiene, and it's arguably the higher-value target of the two runtime images (it holds `DATABASE_URL`, `ADMIN_DATABASE_URL` with RLS-bypass privileges, the storage account key, and the provisioner's Azure SDK credentials at various points in the request lifecycle) even though it's not the one exposed to LLM-driven/prompt-injectable input the way `oc-*` containers are. A container escape or dependency-RCE against the Django image runs as root inside that container by default; adding `USER` + matching `chown` (the same pattern already used in `Dockerfile.openclaw`) is a low-effort hardening step with no known functional blocker (the OpenClaw image proves the pattern works in this same build system).

---

## Findings

- **[high] [open] Telegram bot token leaks into Log Analytics in plaintext on every Telegram API call.** No `httpx` logger-level override and no token-redaction log filter exist on `main` (`config/settings/production.py:125-149`; `apps/router/logging_filters.py` does not exist in this repo). The token rides in the URL path of every `getUpdates`/`sendMessage`/`sendPhoto`/etc. call and `httpx` logs full request URLs at `INFO`, which the root logger forwards to stdout → Container Apps Log Analytics unfiltered. One shared token, fleet-wide impact if read by anyone with log access. Fix: port/land the `RedactTelegramToken` filter pattern (mirror `RedactBYOPasteBody`'s wiring) and add it to `LOGGING["filters"]`/the `console` handler; treat the already-logged history as exposed and rotate the bot token at BotFather as a separate, disruptive, operator-scheduled action.
- **[high] [open] Google refresh token + platform OAuth client secret written in plaintext to the per-tenant SMB share.** `apps/integrations/services.py:637-703`, called from `connect_integration:781`. Grants durable `gmail.modify` + full `calendar` + `drive.file` + `tasks` access, readable by the LLM-driven container process itself (the exact prompt-injection threat model) and by anything with share access; also duplicates the platform-wide `GOOGLE_OAUTH_CLIENT_SECRET` onto every connected tenant's share. Bypasses the `_put_share_file` sanitize chokepoint ([invariant #2](../agents/invariants.md)). Deleted on explicit disconnect, so exposure is bounded to "while connected," not forever. Fix direction: short-lived access-token-on-demand via a runtime callback (mirrors the BYO Claude CLI wrapper pattern) rather than a persisted refresh token on-share.
- **[med] [open] `tenant-<uuid>-internal-key` (and any un-disconnected OAuth/BYO secret) is never deleted from Key Vault on deprovision.** `deprovision_tenant` (`apps/orchestrator/services.py:880-955`) only cleans up the OpenRouter sub-key/secret; `delete_managed_identity` only deletes the Azure identity resource, not KV contents. Low individual risk (the reading identity is deleted in the same flow) but there's no sweep job (unlike `sweep_orphan_openrouter_keys`) and no way to audit "every KV secret maps to a live tenant." Fix: extend `deprovision_tenant` to delete the per-tenant internal-key secret and any still-connected OAuth/BYO secrets, or add an equivalent orphan-secret sweeper.
- **[med] [by-design, needs monitoring] `python-telegram-bot`, `transformers` (Dependabot-muted), and the ~1.1 GB PII/torch stack dominate the Python dependency surface with no CI-enforced vulnerability scan** (no `pip-audit`/`safety`/`bandit`/`trivy` step in any workflow; frontend has no `npm audit` either). Detection is entirely Dependabot-alert-driven, which is reactive and, for `transformers` specifically, fully disabled by policy at every update level. Consider a non-blocking `pip-audit`/`npm audit --audit-level=high` CI step that reports without gating, given the existing majors-held/auto-merge posture already trades off some responsiveness for stability.
- **[low] [open] `python-telegram-bot==22.8` is a pinned but functionally unused dependency** — confirmed no `import telegram` anywhere under `apps/`; all Telegram traffic is raw `httpx`. Pure removable supply-chain surface.
- **[low] [open] `DEPLOY_SECRET` is compared with plain `==` at every call site**, not a constant-time compare, inconsistent with the codebase's own pattern elsewhere (`secrets.compare_digest` for the internal key, `hmac.compare_digest` for PKCE). Sites: `apps/cron/views.py:681,1266,1326,1521`, `apps/journal/extraction_views.py:39`. Low-probability timing side-channel against a single shared, non-rotated secret; trivial fix.
- **[low] [open] Django control-plane image runs as root** — no `USER` directive in `Dockerfile` (contrast `Dockerfile.openclaw:91`, which already does `USER node` after a matching `chown`). Django holds `ADMIN_DATABASE_URL` (RLS-bypass) and the storage account key at points in its lifecycle; adding non-root execution is a low-effort hardening step with a working in-repo precedent to copy.
- **[low] [partially-mitigated] `NBHD_INTERNAL_API_KEY`/`OPENCLAW_GATEWAY_TOKEN` remains a single platform-shared secret provisioned to every container**, even though the internal-auth *validator* no longer accepts it as a fallback (Phase 1d closed that path). It still backs the OpenClaw gateway's own separate auth boundary. No rotation automation found; a leak requires a fleet-wide rotation + restart wave rather than a single-tenant one. (Already flagged in the infra reference doc; repeated here as the highest-blast-radius rotatable secret after the four shared LLM keys.)
- **[low] [by-design] Two Key Vault access styles coexist by design and are correctly scoped**: Django reads env vars sourced from KV out-of-band (no in-repo `keyvaultref:`), while `oc-*` containers use native Container Apps KV secret references resolved via per-secret-RBAC managed identity. This split is intentional and each half is internally consistent — noted for completeness, not as a defect. The genuine gap is the fallback-to-plaintext behavior in `_build_container_secret` (`azure_client.py:71-96`) when the KV reference can't be built, already flagged [med] in the infra reference doc and restated in §2 above — that one *is* an open risk, not by-design.
