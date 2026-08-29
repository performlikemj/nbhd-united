# NBHD United — Security Overview & Findings Register

Entry point for security review. It states the threat model, then consolidates every finding from the five analyses in this directory into one prioritized register.

**Audit method.** A multi-agent documentation sweep (2026-07-09, against current `main`): ten subsystem-mapping passes → five security passes (isolation, authN/Z, PII egress, secrets/supply-chain, input/injection). Findings marked **★ verified** were independently confirmed by the orchestrator against source and/or the live production database. This is a code + live-config review, not a penetration test — no exploit was executed against production.

## What this platform must protect

| Asset | Where it lives | Worst case if it leaks |
|---|---|---|
| One tenant's private life data (journal, finance, fitness, messages) | Postgres (per-tenant rows), the `oc-*` file share | Cross-tenant exposure of intimate personal data |
| The PII reversal key (`pii_entity_map`: placeholder → real name/email) | `tenants` row, plaintext JSON | De-anonymizes every redacted record fleet-wide |
| Third-party account access (Google refresh token, OAuth client secret) | Per-tenant file share, plaintext | Durable takeover of a user's Gmail/Calendar/Drive |
| Bearer secrets that *are* identity (`internal_api_key`, `DEPLOY_SECRET`) | Key Vault, container env, DB | Full per-tenant runtime control; fleet-wide destructive ops |
| Shared channel + LLM credentials (Telegram token, OpenRouter keys) | Key Vault, container env | Fleet-wide messaging / model-billing compromise |

## Trust boundaries

```
Untrusted user ──chan──▶ Django control plane ──internal-key──▶ oc-* container (LLM-driven)
   (Telegram/LINE/            │                                      │
    iOS/web)                  ├─ JWT/PAT (console API)               ├─ mounted file share (AGENTS/USER.md, creds)
                              ├─ webhook signatures (Stripe/TG/LINE) ├─ LLM egress (OpenRouter ZDR; BYO parked)
                              └─ Postgres as app_user               └─ tools (gated + policy deny-list)
```

Four boundaries carry the platform:
1. **Console API** — JWT / PAT auth, then app-layer tenant filtering. *(RLS is mostly OFF — see below.)*
2. **Internal runtime** (`container → Django`) — a per-tenant **bearer secret** (`X-NBHD-Internal-Key`). Handlers run `service_role=True`, so RLS is *no backstop here*.
3. **Webhooks** — cryptographic signature / secret verification (Stripe HMAC, Telegram secret-token, LINE HMAC). **Solid.**
4. **LLM egress** — model inference leaves through OpenRouter ZDR routes, enforced per request in code plus the account setting; raw-audio STT is the disclosed redaction exception.

## The load-bearing fact every auditor needs

**Database-level tenant isolation is deliberately disabled.** `startup.sh` runs `manage.py disable_rls` on every boot, turning RLS off on **162 of 165 tables** (verified live). Only three cross-tenant Friends tables (`shared_lessons`, `lesson_share_grants`, `friend_messages`) keep FORCE-RLS. Consequences:

- For all non-Friends tenant data, **isolation is 100% application-layer ORM filters** — one query missing its `tenant=` filter leaks across tenants with no database net and no CI guard.
- The runtime role is **verified** (live `pg_stat_activity`): Django serves requests as **`app_user` via the pooler**, and `app_user` is non-BYPASSRLS and does not own the tables. So the 3-table FORCE-RLS backstop **does bind** for normal app queries — the in-repo comments claiming "Django connects as `postgres` (BYPASSRLS)" are **stale** and should be corrected. (`check_friends_rls` would corroborate; the connection-role read already settles it.)
- **Why RLS is disabled** isn't an oversight: `app_user` is non-BYPASSRLS and the general tables carry no policies, so "RLS on + no policy" would *deny the app access to its own tables*. `disable_rls` strips it each boot so the app can function; isolation lives in the query layer. The Supabase anon Data API is blocked by a `REVOKE ALL` from `anon`/`authenticated` (verified: zero privileges), independent of RLS — so disabling RLS re-exposes nothing.
- On the internal-key/agent surface, the backstop binds nowhere (every handler sets `service_role=True`).

The real, working isolation controls are: the Supabase anon-Data-API zero-grant lockdown, the single CI-enforced `apps/friends/access.py` cross-tenant chokepoint, and disciplined app-layer filtering. Full detail: [multi-tenant-isolation.md](multi-tenant-isolation.md).

## Findings register

Severity = impact × likelihood. Status: `open` / `partially-mitigated` / `by-design`. IDs are stable handles for remediation tracking. Full analysis behind each is in the linked doc.

### High

| ID | Finding | Status | Ref |
|---|---|---|---|
| SEC-1 ★ | Google **refresh token + platform OAuth client_secret written plaintext** to the per-tenant share (`integrations/services.py:637-703`); readable by the LLM-driven container itself (prompt-injection exfil) — grants `gmail.modify`/calendar/drive. Bypasses the sanitize chokepoint. | open | [secrets](secrets-identity-supply-chain.md), [injection](input-handling-and-injection.md) |
| SEC-2 ★ | **Telegram bot token streams into Log Analytics in plaintext** — no `RedactTelegramToken` filter on `main`, httpx logs the token-bearing URL at INFO. Fix is staged unmerged on `fix/site-publishing-reliability`; channel is being decommissioned. Rotate the token; history is exposed. | open | [secrets](secrets-identity-supply-chain.md) |
| SEC-3 ★ | **PAT scopes are cosmetic** — enforced on only 3 of ~90 `IsAuthenticated` endpoints (`PersonalAccessTokenAuthentication` is a default auth class). A "sessions:write" token works at billing, delete-account, and BYO-credential endpoints — a de-facto full-account credential. | open | [authn-authz](authn-authz-and-api-surface.md) |
| SEC-4 ★ | Onboarding's first `USER.md` write now uses checked mint-redaction and skips unconfirmed writes (`72dad31c`, `0b6bc9f9`). | closed | [pii-egress](pii-and-llm-egress.md) |
| SEC-5 ★ | **No DB isolation net on 162/165 tables** (`disable_rls` at boot). A missing `tenant=` filter leaks cross-tenant with no backstop and no CI guard. | open (by-design posture, under-defended) | [isolation](multi-tenant-isolation.md), [data-model](../reference/data-model.md) |
| SEC-6 ★ | **No SSRF egress controls** in generated container config (`config_generator.py` has no `ssrf`/`allowPrivateNetwork`); the 2026-02 interceptor/NSG plan was never built. Protection rests on an unverified OpenClaw upstream default. | open | [injection](input-handling-and-injection.md) |
| SEC-7 | **Action-gating is not a capability check** — approval is a text string in the model's context, not a code-level binding to the gated tool call. Low blast radius *only* because destructive GWS skills aren't wired yet. | partially-mitigated | [injection](input-handling-and-injection.md) |
| SEC-8 | **`pii_entity_map` / `pii_denylist` are plaintext** — the reversal key for every placeholder fleet-wide. Tracked as encryption-directive Phase 4; the crypto substrate (`apps/crypto`) does not exist yet. | open (tracked) | [pii-egress](pii-and-llm-egress.md) |
| SEC-9 ★ | **In-repo comments (`0059`, `0106`, `access.py`) claim Django connects as `postgres`/BYPASSRLS — verified false**; it connects as non-BYPASSRLS `app_user`, so the friends backstop actually binds. Now a doc-correctness issue, not an unknown: fix the stale comments. | open (downgraded) | [isolation](multi-tenant-isolation.md) |

### Medium

| ID | Finding | Status | Ref |
|---|---|---|---|
| SEC-10 | Internal-key auth is enforced **by convention** (first line of ~150 handlers); all current handlers verified correct, but nothing structurally prevents a future one shipping open (RLS is no backstop there). Add a CI AST check. | open | [authn-authz](authn-authz-and-api-surface.md) |
| SEC-11 | Per-tenant Key Vault secrets (`internal-key`, OAuth/BYO) are **never deleted on deprovision** — no sweeper, no "every secret maps to a live tenant" audit. | open | [secrets](secrets-identity-supply-chain.md) |
| SEC-12 | **No CI vulnerability-scan gate** (no `pip-audit`/`npm audit`); `transformers` is Dependabot-muted at every level. | open | [secrets](secrets-identity-supply-chain.md) |
| SEC-13 ★ | `report_content` (`friends/circles.py:267`) has **no visibility check** — an existence oracle over neighbors' `SharedLesson` ids; breaks the module's re-verify-party pattern. | open | [isolation](multi-tenant-isolation.md) |
| SEC-14 | **Fail-open redaction** (`redact_text` swallows all exceptions → forwards the *original* text) with **no alerting** on load-error / exception rate. | open | [pii-egress](pii-and-llm-egress.md) |
| SEC-15 | Siri Tier-2 and meditation compose now redact model-bound context and add placeholder legends; only the Siri owner-facing reply is rehydrated (`6b141d5d`, `4a134707`, `984b5dc4`). | closed | [pii-egress](pii-and-llm-egress.md) |
| SEC-16 | The SEC-3 PAT-scope gap **exposes PII endpoints** (`pii-review-queue/` returns real span values to any authenticated PAT). | open | [pii-egress](pii-and-llm-egress.md) |
| SEC-17 | `azure_client._build_container_secret` **falls back to a plaintext secret** when the Key Vault reference can't be built. | open | [secrets](secrets-identity-supply-chain.md), [infra](../reference/infrastructure-and-deployment.md) |
| SEC-18 | `PlatformIssueLog.detail/.summary` accepts agent free text; "no PII" is convention-only, not run through `redact_text`. | open | [pii-egress](pii-and-llm-egress.md) |
| SEC-19 | The GWS credential write is **hand-rolled**, bypassing the `_put_share_file` single-writer sanitize chokepoint (invariant #2). | open | [injection](input-handling-and-injection.md) |

### Low / consistency (summarized)

`DEPLOY_SECRET` compared with plain `==` (~20 sites, not constant-time); Django control-plane Docker image **runs as root** (OpenClaw image already uses `USER node`); `python-telegram-bot` pinned but **unused** (removable); two internal-auth header conventions + a gate handler that defaults the tenant-id header to the URL value; chart/meditation assets rely on UUID-obscurity with no expiry; no automated cross-tenant IDOR fuzz coverage; `NBHD_INTERNAL_API_KEY`/gateway token is one shared secret with no rotation automation; BYO transcripts rest on the shared-key share (CMK track); binary share writes bypass sanitize (no guard against a future binary→text path); `friends_callbacks.py` hand-rolls a cross-tenant query outside the CI-guarded chokepoint; `test_public_schema_lockdown` asserts against the test DB (never runs `disable_rls`), so it certifies a precondition, not prod steady-state. Details in the per-area docs.

## What is demonstrably solid (verified)

So the register reads in proportion: much of the security architecture is genuinely well-built.

- **Webhook authenticity** — Stripe/Telegram/LINE all verify signatures before touching the body.
- **The PKCE web→app token exchange** (`tenants/oauth_views.py`) — single-use locked codes, constant-time comparisons throughout; cite as the reference pattern.
- **BYO credential secrets** — write-only end to end; no serializer or log path returns them.
- **No SQL injection** — no raw/f-string SQL built from request or model input.
- **No frontend XSS** — static export, no `dangerouslySetInnerHTML`, TipTap StarterKit with no raw-HTML extension.
- **Cross-tenant messaging closed** — `nbhd_send_to_user` has no recipient parameter; routing is server-side.
- **Upload ingress** — image/PDF paths sniff magic bytes (not client MIME) with pre- and post-decode size caps.
- **The Friends cross-tenant chokepoint** — one audited accessor, CI-enforced, IDOR-defeated-by-construction (opaque ids + re-verify-party), correct on every path except SEC-13.
- **Model egress is sealed to OpenRouter ZDR** per request for chat/embeddings and by all-endpoints-ZDR model choice for STT; BYO is parked and non-ZDR.

## Action items (do these first)

1. **Reconcile the stale "Django is BYPASSRLS" comments** (`0059`, `0106`, `apps/friends/access.py`) — the runtime role is verified `app_user` (non-BYPASSRLS), so the friends backstop binds and the comments mislead. Add the pending `friend_sky_memberships` (`sky`) FORCE-RLS backstop to `RLS_KEEP_ENABLED` + a policy (settles SEC-5/SEC-9).
2. **Stop persisting the Google refresh token on the share** (SEC-1) — move to short-lived on-demand tokens.
3. **Default-deny unscoped PAT** on sensitive views, or remove the scopes UI (SEC-3, SEC-16).
4. **Rotate the Telegram bot token and land the log filter** — or complete the channel decommission (SEC-2).

See [`../IMPROVEMENTS.md`](../IMPROVEMENTS.md) for the full remediation + modernization roadmap.
