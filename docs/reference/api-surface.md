# API Surface / Endpoint Catalog

Authoritative map of every HTTP endpoint exposed by the NBHD United Django control
plane. Built for a new senior engineer *and* a security auditor: the **Auth /
permission** column is the trust boundary for each row and is the thing to read
first. All paths are rooted at the Django app (`config/urls.py`). Line citations
point at the *view* that handles the request, not the URL registration, unless
noted.

Source of truth for the URL tree: `config/urls.py:9`.

---

## 1. Auth primitives (read this first)

Four distinct trust mechanisms guard this surface. Every table below tags each row
with one of these.

| Tag | Mechanism | Where enforced | Notes |
|---|---|---|---|
| **JWT/PAT** | DRF default: `PersonalAccessTokenAuthentication` then `JWTAuthenticationWithRLS`; default permission `IsAuthenticated` | `config/settings/base.py:135`; classes in `apps/tenants/authentication.py:18` (JWT) + `:97` (PAT) | Tenant is derived from `request.user.tenant`; both auth classes call `set_rls_context(tenant_id, user_id)` so Postgres RLS scopes every query. JWT carries a `pw_iat` claim → password rotation force-logs-out all sessions (`authentication.py:66`). PAT = `Authorization: Bearer pat_<secret>`, SHA-256 looked up in `PersonalAccessToken`. |
| **Internal-key** | `X-NBHD-Internal-Key` + `X-NBHD-Tenant-Id` headers, constant-time compared against `Tenant.internal_api_key` | `apps/integrations/internal_auth.py:123` (`validate_internal_runtime_request`) | **The container→Django trust model.** Per-tenant shared secret; each `oc-*` container sources its own key from its own Key Vault secret. A key leaked from container A cannot authenticate as tenant B (the URL `tenant_id` is passed as `expected_tenant_id` and must equal the header). Views set `permission_classes=[AllowAny]` + `authentication_classes=[]` and call the validator manually, returning 401. The former shared-global-key fallback was removed 2026-06-22 (Phase 1d). |
| **QStash-sig** | `Upstash-Signature` header verified with `QSTASH_CURRENT_SIGNING_KEY` | `apps/cron/qstash_verify.py:10` | Proves the scheduler (Upstash QStash) sent the request. Used by cron task-execution endpoints. |
| **Deploy-secret** | `X-Deploy-Secret` header == `settings.DEPLOY_SECRET` | inline in each cron/gate view | Shared secret for CI/CD- and poller-initiated ops (fleet rollouts, gate button callbacks). |
| **Webhook-sig** | Provider HMAC signature (Stripe / Telegram / LINE) | inline per webhook | See §4. |
| **Signed-token** | HMAC/`django.signing` token carried *in the URL* is the entire authorization | per view | Promo redeem, email unsubscribe, OAuth `state`, friend-invite preview. `AllowAny` by design. |
| **UUID-obscurity** | No auth; access control is an unguessable UUID + strict filename regex | `apps/router/views.py` | Chart images, meditation audio. See §5. |

> **Header inconsistency (flag):** the *actions gate* endpoints (§3d) read
> `X-Internal-Key` / `X-Tenant-Id` (no `X-NBHD-` prefix) and return **403**, while
> every other internal route reads `X-NBHD-Internal-Key` / `X-NBHD-Tenant-Id` and
> returns **401**. Same validator underneath, two header spellings. See
> `apps/actions/views.py:47` vs `apps/integrations/runtime_views.py:141`.

---

## 2. Public console API (JWT/PAT)

Everything the web/iOS console calls. All rows are **JWT/PAT authenticated,
`IsAuthenticated`, tenant-scoped via `request.user.tenant` + RLS** unless the row
says otherwise. Grouped by mount point.

### 2a. Tenant / account / settings — `/api/v1/tenants/` (`apps/tenants/urls.py:39`)

| Method + Path | View (path:line) | Auth | Tenant-scoped | Notes |
|---|---|---|---|---|
| `* /` (+ `<pk>/`) | `TenantViewSet` `apps/tenants/views.py` (router `urls.py:37`) | JWT/PAT | Yes | DefaultRouter CRUD over the caller's tenant. `byo-credentials/` is mounted *before* this router (`config/urls.py:25`) so the catch-all `<pk>/` doesn't swallow it. |
| `POST /onboard/` | `OnboardTenantView` `views.py:49` | JWT/PAT | Yes | Provision a container for the logged-in user. |
| `GET/PUT /profile/` | `ProfileView` `views.py:120` | JWT/PAT | Yes | |
| `GET /provisioning-status/` | `ProvisioningStatusView` `views.py:152` | JWT/PAT | Yes | |
| `POST /retry-provisioning/` | `RetryProvisioningView` | JWT/PAT | Yes | |
| `GET /personas/` | `PersonaListView` | JWT/PAT | Yes | |
| `POST /preferences/` | `UpdatePreferencesView` | JWT/PAT | Yes | |
| `POST /refresh-config/` | `RefreshConfigView` | JWT/PAT | Yes | Bumps the tenant's OpenClaw config. |
| `GET/POST /settings/entity-registry/` , `…/<placeholder>/` | `EntityRegistryListView` / `EntityRegistryItemView` | JWT/PAT | Yes | Per-tenant PII placeholder registry (privacy). |
| `GET/POST /settings/pii-denylist/` , `…/bulk/` , `…/<key>/` | `PIIDenylistListView` / `PIIDenylistBulkView` / `PIIDenylistItemView` | JWT/PAT | Yes | `bulk/` registered before `<key>/` (declaration-order match). |
| `POST /telegram/generate-link/` , `/telegram/unlink/` , `GET /telegram/status/` | `apps/tenants/telegram_views.py:24/49/62` | JWT/PAT | Yes | `@permission_classes([IsAuthenticated])`. |
| `POST /line/generate-link/` , `/line/unlink/` , `GET /line/status/` , `PATCH /line/preferred-channel/` | `apps/tenants/line_views.py:23/48/58/77` | JWT/PAT | Yes | |
| `GET/POST /heartbeat/` | `HeartbeatConfigView` `views.py:337` | JWT/PAT | Yes | |
| `POST /delete-account/` , `/cancel-deletion/` | `DeleteAccountView` / `CancelDeletionView` | JWT/PAT | Yes | |
| `GET/PUT /settings/preferred-model/` | `PreferredModelView` `views.py:445` | JWT/PAT | Yes | Tier-gated model switch. |
| `GET/PUT /settings/task-model-preferences/` | `TaskModelPreferencesView` | JWT/PAT | Yes | |
| `GET /settings/available-models/` | `AvailableModelsView` | JWT/PAT | Yes | |
| `GET /promos/redeem/` | `redeem_promo` `apps/tenants/promo_views.py:45` | **Signed-token** (`AllowAny`) | via token | HMAC token in `?token=` carries authz (`promo_signing.verify_promo_token`). |
| `GET/POST /unsubscribe/<token>/` | `unsubscribe` `apps/tenants/unsubscribe_views.py` | **Signed-token** (`csrf_exempt`) | via token | RFC 8058 List-Unsubscribe-Post; HMAC token in path. Invalid token → 404 without leaking existence. |
| `runtime/<tenant_id>/…` (welcomes, agenda, commitments, preferred-model) | see §3b | **Internal-key** | URL | These sit inside `tenants/urls.py` but are container→Django. |

### 2b. Feature-pillar console reads/writes (all JWT/PAT, tenant-scoped)

Each pillar app mounts a **consumer** surface (JWT/PAT) and a **runtime** surface
(internal-key, catalogued in §3). Consumer rows below.

| Mount | Endpoints (all `IsAuthenticated`) | View module |
|---|---|---|
| `/api/v1/billing/` (`apps/billing/urls.py`) | `POST /portal/` `views.py:202`, `POST /checkout/` `:261`, `GET /credits/` `:332`, `POST /credits/checkout/` `:429`, `GET /usage/summary/`, `/usage/daily/`, `/usage/transparency/`, `GET/PUT /donation-preference/` (`usage_views.py`) | `apps/billing/views.py`, `usage_views.py` |
| `/api/v1/automations/` (`apps/automations/urls.py`) | `GET/POST /`, `GET /runs/`, `GET /<id>/`, `POST /<id>/pause/`, `/resume/`, `/run/`, `GET /<id>/runs/` | `apps/automations/views.py` |
| `/api/v1/journal/` (`apps/journal/urls.py`) | Documents (`documents/`, `today/`, `tree/`, `status/`), typed lifecycle (`tasks/`, `goals/` + `complete`/`reopen`/`achieve`/`abandon`), North Star (`purposes/`), legacy journal (`/`, `<entry_id>/`, `daily/<date>/…`, `memory/`, `templates/`, `reviews/`) | `apps/journal/{document,lifecycle,purpose,views}.py` |
| `/api/v1/lessons/` (`apps/lessons/urls.py`) | `LessonViewSet` DefaultRouter CRUD | `apps/lessons/views.py` |
| `/api/v1/dashboard/` (`apps/dashboard/urls.py`) | `GET /`, `/usage/`, `/horizons/` | `apps/dashboard/views.py` |
| `/api/v1/finance/` (`apps/finance/urls.py`) | `settings/`, `restart/`, `dashboard/`, `accounts/` (+`<id>/`), `transactions/`, `payoff-plans/`, `snapshots/` | `apps/finance/views.py` |
| `/api/v1/fuel/` (`apps/fuel/urls.py`) | `settings/`, `healthkit/sync/`, `profile/`, `workouts/` (+`<id>/` + skip/complete/duplicate/edit-lock), `calendar/`, `overview/`, `progress/`, `weekly-summary/`, `templates/`, `prs/`, `goals/`, `body-weight/`, `resting-hr/`, `sleep/`, `plans/` | `apps/fuel/views.py` |
| `/api/v1/core/` (`apps/core/urls.py`) | `settings/`, `restart/`, `profile/`, `compose/`, `sessions/` (+`<id>/`) | `apps/core/views.py` |
| `/api/v1/insights/` (`apps/insights/urls.py`) | `history/`, `snapshots/<id>/`, `compare/`, `baseline/`, `insights/` (+ record/confirm/refute), `signals/`, `voice-prefs/` | `apps/insights/views.py` |
| `/api/v1/cron-jobs/` (`apps/cron/tenant_urls.py`) | `GET/POST /`, `bulk-delete/`, `bulk-update-foreground/`, `pending-at/` (+`<name>/`), `<job_name>/`, `<job_name>/toggle/` | `apps/cron/tenant_views.py`, `pending_at_views.py` |
| `/api/v1/workspaces/` (`apps/journal/workspace_urls.py`) | `GET/POST /`, `switch/`, `<slug>/` | `apps/journal/workspace_views.py` |
| `/api/v1/sessions/` (`apps/journal/session_urls.py`) | `GET /`, `create/`, `<session_id>/` | `apps/journal/session_views.py` |
| `/api/v1/tenants/byo-credentials/` (`apps/byo_models/urls.py`) | `GET/POST /`, `GET/PUT/DELETE /<cred_id>/` | `apps/byo_models/views.py` — user-supplied LLM API keys; verify write-only handling of secrets. |

### 2c. Rich-client chat ingress — `/api/v1/chat/` (`apps/router/chat_urls.py`)

| Method + Path | View (path:line) | Auth | Tenant-scoped | Notes |
|---|---|---|---|---|
| `POST /messages/` | `ChatMessageView` `apps/router/chat_views.py:433` | JWT/PAT | Yes | iOS/web message ingress; routes *through* the tenant container. |
| `POST /read/` | `ChatReadView` `:709` | JWT/PAT | Yes | |
| `GET /context/` | `ChatContextView` `:542` | JWT/PAT | Yes | |
| `POST /turns/` | `ChatLocalTurnView` `:604` | JWT/PAT | Yes | Records an on-device (Core AI) turn. |
| `GET /messages/<client_msg_id>/` | `ChatMessageDetailView` `:694` | JWT/PAT | Yes | |
| `GET /threads/` | `ChatThreadListView` `:375` | JWT/PAT | Yes | |
| `GET /threads/<thread_id>/messages/` | `ChatThreadMessagesView` `:403` | JWT/PAT | Yes | |

### 2d. iOS platform endpoints (all JWT/PAT)

| Method + Path | View (path:line) | Notes |
|---|---|---|
| `GET /api/v1/siri/status/` , `POST /api/v1/siri/respond/` | `SiriQuickStatusView` `apps/router/siri_views.py:114`, `SiriRespondView` `:151` | Reuses the user JWT — **no dedicated Siri scope**. |
| `POST /api/v1/push/register/` , `POST /api/v1/push/test/` | `PushRegisterView` `apps/router/push_views.py:91`, `PushTestView` `:135` | APNs device-token registration. |
| `GET /api/v1/coreai/model/manifest/` | `CoreAIModelManifestView` `apps/router/coreai_views.py:26` | On-device model manifest; `IsAuthenticated`. |

### 2e. Neighborhood (Friends) console — `/api/v1/friends/` (`apps/friends/urls.py`)

Base class `FriendsView` sets `IsAuthenticated` (`apps/friends/views.py:26`); all
rows JWT/PAT + tenant-scoped **except** the one flagged.

| Group | Endpoints |
|---|---|
| Neighborhood/home | `GET /`, `/home/`, `/blocked/`, `/consent/`, `/profile/` |
| Waves (friend requests) | `POST /waves/`, `…/<friendship_id>/accept|decline|block/` |
| Shares (lesson sharing) | `/shares/preview/`, `/shares/pending/`, `…/<id>/approve|reject/`, `…/<id>/adopt/`, `/absorbed/`, `…/<id>/purge/` |
| 1:1 chat | `/threads/`, `…/<thread_id>/messages|read|membership/` |
| Missions | `/missions/` (+`<id>/`, join/leave/updates/tasks), `/mission-actions/` (+approve/reject) |
| Circles | `/circles/` (+ join, `<id>/`, members/leave/remove/invite-code) |
| Moderation | `POST /report/` |
| Wormholes/warp | `/wormholes/`, `<friendship_id>/galaxy|visited|unblock/`, `<friendship_id>/` (unfriend) |
| Invites | `POST /invites/`, `POST /invites/<token>/claim/`, **`GET /invites/<token>/` → `InviteDetailView` `views.py:123` = `AllowAny`** (public invite preview: inviter identity only, nothing private) |

---

## 3. Internal runtime endpoints (container → Django) — **key finding**

**Trust model.** These are called by per-tenant OpenClaw containers (and their
plugins), never by end users directly. Django does **not** use a network boundary
or mTLS here — the container app is internet-reachable. Authentication is a
**per-tenant shared secret**:

1. Container sends `X-NBHD-Internal-Key: <secret>` + `X-NBHD-Tenant-Id: <uuid>`.
2. View is `permission_classes=[AllowAny]`, `authentication_classes=[]`, and calls
   `validate_internal_runtime_request(...)` manually
   (`apps/integrations/internal_auth.py:123`).
3. The validator constant-time-compares the key against
   `Tenant.internal_api_key` for the tenant named in the **URL** `tenant_id`
   (passed as `expected_tenant_id`), and requires the header tenant-id to equal
   it. On success it sets RLS with `service_role=True` and returns the request to
   the handler.

Consequences an auditor should note:
- The key **is** the tenant's identity. There is no proof the caller is *that
  tenant's container* beyond possession of the secret; any party with the
  per-tenant key and tenant-id can call every runtime endpoint for that tenant.
- Scope is per-tenant: a key for tenant A cannot act on tenant B (URL/header/DB
  triple must agree). No cross-tenant escalation via a single leaked key.
- A tenant with an empty `internal_api_key` is rejected outright (no global
  fallback since Phase 1d, `internal_auth.py:164`).
- These handlers run with `service_role=True` RLS — RLS is *not* a second line of
  defense here; the key check is the only gate. A bug that skips the manual
  validator call (it is not enforced by a permission class) would fully expose
  that endpoint.

The internal surface is large (~150 routes). Catalogued by mount below; every row
is **Internal-key auth, tenant from URL**, unless noted. Representative view-line
citations given per group.

### 3a. Directly mounted internal routes (`config/urls.py`)

| Method + Path | View (path:line) | Notes |
|---|---|---|
| `POST /api/v1/internal/runtime/<tenant_id>/usage/report/` | `RuntimeUsageReportView` `apps/integrations/runtime_views.py` | Also re-mounted at `/api/v1/integrations/runtime/<tenant_id>/usage/report/`. |
| `POST /api/v1/internal/runtime/<tenant_id>/byo/error/` | `RuntimeBYOErrorReportView` `apps/integrations/runtime_views.py` | Flips `BYOCredential.status=error`. |
| `POST /api/v1/internal/runtime/<tenant_id>/chat/progress/` | `ChatProgressEventView` `apps/router/chat_views.py:755` | Agent activity stream (waking/thinking/tool/composing). Internal-key. |
| `/api/v1/internal/runtime/<tenant_id>/gate/…` | see §3d | Action gating (different header names). |
| `/api/v1/gate/<action_id>/respond/` | see §3d | Deploy-secret, not internal-key. |

### 3b. `/api/v1/tenants/runtime/<tenant_id>/…` (`apps/tenants/runtime_views.py`)

Helper `_internal_auth_or_401` at `runtime_views.py:36`.

| Method + Path | View (path:line) |
|---|---|
| `POST /welcomes/<feature>/` | `RuntimeWelcomeMarkView` `:64` |
| `POST /agenda/<kind>/<item_id>/` | `RuntimeAgendaEngagementView` `:115` |
| `POST /commitments/` | `RuntimeCommitmentRecordView` `:204` |
| `GET/PUT /preferred-model/` | `RuntimePreferredModelView` `:290` — reuses the consumer tier gate so the agent cannot upgrade itself past the tier ceiling |

### 3c. `/api/v1/integrations/runtime/<tenant_id>/…` (`apps/integrations/runtime_views.py`)

The largest runtime surface. Helper `_internal_auth_or_401` at `:141`. All
internal-key, tenant-from-URL. Grouped:

| Group | Endpoints (method varies GET/POST/PATCH) |
|---|---|
| Typed goals/tasks | `goals/` (+`<id>/`, achieve/abandon), `tasks/` (+`<id>/`, complete/skip/defer) |
| Grounding | `current-status/` (as-of-now snapshot for cron/proactive) |
| Google | `gmail/messages/` (+`<message_id>/`), `google-calendar/events/`, `google-calendar/freebusy/` |
| Journal/memory | `journal-entries/`, `weekly-reviews/`, `daily-note/` (+append), `long-term-memory/`, `journal-context/`, `journal/search/`, `memory-sync/`, `document/` (+append) |
| YardTalk sessions | `sessions/pending/`, `sessions/<id>/mark-processed/` |
| Lessons | `lessons/`, `lessons/search/`, `lessons/pending/` |
| Neighborhood (agent-facing) | `lessons/<id>/propose-share/`, `neighborhood/context/`, `missions/`, `missions/<id>/propose-task/`, `constellation/notes/` |
| Reconcile | `reconcile/scan/` |
| Reporting | `usage/report/`, `byo/error/`, `platform-issue/report/` (→ `apps/platform_logs/views.py:PlatformIssueReportView`) |
| Profile | `profile/` (agent-initiated tz/display_name/language) |
| Workspaces | `workspaces/`, `workspaces/switch/`, `workspaces/<slug>/` |
| Cron delivery | `send-to-user/` (→ `apps/router/cron_delivery.py:CronDeliveryView`; agent sends a message to the user via Django) |
| Cron typed patterns | `cron-phase2-summary/`, `crons/pure_reminder/`, `crons/quote_user_intent/`, `crons/domain_summary/`, `crons/<name>/pattern_context/`, `crons/<name>/validate_outbound/`, `crons/<name>/grounding/` |
| Reddit | `reddit/connect|complete|status|disconnect|tool/` |
| `IntegrationViewSet` | mounted at `/api/v1/integrations/` root — see §2b/§6 note (ReadOnly, `IsAuthenticated`) |

Journal North-Star runtime companions live under `/api/v1/journal/runtime/<tenant_id>/…`
(`apps/journal/urls.py:94`): `query/`, `purposes/` (+ propose/`<id>/`/confirm/retire/link-goal),
each internal-key.

### 3d. Action gating (destructive-action approval) — **different auth**

| Method + Path | View (path:line) | Auth | Notes |
|---|---|---|---|
| `POST /api/v1/internal/runtime/<tenant_id>/gate/request/` | `GateRequestView` `apps/actions/views.py:96` | **Internal-key** via `X-Internal-Key`/`X-Tenant-Id` (`_validate_internal_auth` `:47`), returns **403** | Agent requests approval for a destructive action. |
| `GET /api/v1/internal/runtime/<tenant_id>/gate/<action_id>/poll/` | `GatePollView` `views.py:225` | Internal-key (`X-Internal-Key`) | Agent polls for approve/deny. |
| `POST /api/v1/gate/<action_id>/respond/` | `GateRespondView` `views.py:288` | **Deploy-secret** (`X-Deploy-Secret` == `DEPLOY_SECRET`), 403 on mismatch | Called by Django's *own* poller from the Telegram/LINE approve button. `permission_classes=[AllowAny]` + manual check. |

> `apps/actions/urls.py` also declares `request/` `<id>/poll/` `<id>/respond/` but
> the live mounts are the split `runtime_urls.py` (gate) + `respond_urls.py`
> (respond) from `config/urls.py:59` / `:64`.

### 3e. Other pillar runtime surfaces (internal-key, tenant-from-URL)

| Mount | Endpoints | Helper |
|---|---|---|
| `/api/v1/finance/runtime/<tenant_id>/` (`apps/finance/runtime_views.py:71`) | `accounts/` (+archive/unarchive), `transactions/`, `balance/`, `payoff/calculate/`, `summary/`, `query/` | manual validator, `AllowAny` |
| `/api/v1/fuel/runtime/<tenant_id>/` (`apps/fuel/runtime_views.py:111`) | `log/`, `workouts/<id>/` (+skip/complete), `workouts/swap/`, `audit/`, `summary/`, `body-weight/`, `profile/`, `sleep/`, `plans/` (+`<id>/`) | manual validator |
| `/api/v1/core/runtime/<tenant_id>/` (`apps/core/runtime_views.py:62`) | `summary/`, `profile/`, `meditation/` (+`<id>/`) | manual validator |
| `/api/v1/insights/runtime/<tenant_id>/` (`apps/insights/runtime_views.py:100`) | `history/`, `snapshots/<id>/`, `compare/`, `baseline/`, `insights/` (+record/confirm/refute), `signals/`, `voice-prefs/` (+set), `yesterdays-signals/` | manual validator |
| `/api/cron/runtime/<tenant_id>/container-started/` (`apps/cron/runtime_views.py:53`) | `container-started/` | `_internal_auth_or_401` `:30` (X-NBHD headers) |
| `/api/v1/platform-logs/…` note | `PlatformIssueReportView` (`apps/platform_logs/views.py`) is reached via the integrations runtime mount `platform-issue/report/` and `apps/platform_logs/urls.py` (`<tenant_id>/report/`) | internal-key |

---

## 4. Webhooks (third-party → Django)

All three verify provider authenticity by signature/secret and are `csrf_exempt`.
None use JWT; each grants `set_rls_context(service_role=True)` after verifying.

| Method + Path | View (path:line) | Verification | Notes |
|---|---|---|---|
| `POST /stripe/…` | `djstripe.urls` namespace (`config/urls.py:80`) + local `stripe_webhook` `apps/billing/views.py:129` | **Stripe HMAC**: `stripe.Webhook.construct_event(payload, Stripe-Signature, DJSTRIPE_WEBHOOK_SECRET)`; bad sig → 400 (`views.py:135`) | Drives provisioning/tier changes and prepaid-credit grants. Credit grant idempotent on `event_id`. Reads event fields defensively so a synthetic event can't 500. |
| `POST /api/v1/telegram/webhook/` | `telegram_webhook` `apps/router/views.py:172` | **Shared secret**: `X-Telegram-Bot-Api-Secret-Token` `hmac.compare_digest` == `TELEGRAM_WEBHOOK_SECRET`; missing config → 503, bad → 403 (`:184`) | Single shared-bot webhook; resolves `chat_id`→container and forwards. Idempotency via `claim_inbound_event("tg:<update_id>")`. |
| `POST /api/v1/line/webhook/` | `LineWebhookView` `apps/router/line_webhook.py:793` | **LINE HMAC-SHA256**: `X-Line-Signature` over raw body with channel secret, `hmac.compare_digest` (`_verify_signature` `:64`); missing/invalid → rejected (`:798`) | Plain Django `View` (not DRF). |

---

## 5. Unauthenticated public endpoints

| Method + Path | View (path:line) | Access control | Notes |
|---|---|---|---|
| `GET /health/` | `health` `config/health.py:19` | None (by design) | Liveness probe for the CI deploy gate + load balancer. |
| `GET /admin/` | `django.contrib.admin` (`config/urls.py:12`) | Django admin session/login | Staff-only; standard Django admin auth, separate from the JWT surface. |
| `GET /api/v1/charts/<tenant_id>/<filename>` | `serve_chart_image` `apps/router/views.py:403` | **UUID-obscurity**: no auth; `filename` must match `^[\w-]+\.png$` (path-traversal guard `:415`) | Serves chart PNGs for LINE image messages. Anyone with the exact tenant-UUID + filename can fetch. |
| `GET /api/v1/meditations/<tenant_id>/<filename>` | `serve_meditation_audio` `apps/router/views.py:507` | **UUID-obscurity**: no auth; `filename` must match `^[\w-]+\.(mp3\|ogg)$` (`:520`) | Core-pillar audio; same unguessable-filename model. |
| `GET/POST /api/v1/tenants/promos/redeem/`, `…/unsubscribe/<token>/` | §2a | Signed HMAC token in URL | Unauthenticated but token-gated. |
| `GET /api/v1/integrations/callback/<provider>/` | `OAuthCallbackView` `apps/integrations/views.py:284` | `AllowAny`, `authentication_classes=[]`; authz via signed `state` (`_load_oauth_state`, `signing.loads(..., salt="oauth")` + cache nonce) | OAuth redirect target. |
| `GET /api/v1/integrations/composio-callback/<provider>/` | `ComposioCallbackView` `apps/integrations/views.py:296` | `AllowAny`; signed `state` | Composio OAuth redirect target. |
| `GET /api/v1/friends/invites/<token>/` | `InviteDetailView` `apps/friends/views.py:123` | `AllowAny` | Public invite preview (inviter identity only). |

Auth/account endpoints that are unauthenticated *by design* are in §6.

---

## 6. Auth / account — `/api/v1/auth/` (`apps/tenants/auth_urls.py`)

| Method + Path | View (path:line) | Auth | Notes |
|---|---|---|---|
| `POST /signup/` | `SignupView` `apps/tenants/auth_views.py:199` | `AllowAny` | Creates user + tenant. |
| `POST /login/` | `ThrottledLoginView` `auth_views.py:41` | `AllowAny` (SimpleJWT) + **rate-limit** `LoginIpThrottle` + `LoginEmailThrottle` (`:51`) | Issues access+refresh with `pw_iat` claim. |
| `POST /refresh/` | `rest_framework_simplejwt.TokenRefreshView` (`auth_urls.py:18`) | `AllowAny` (valid refresh token) | |
| `POST /logout/` | `LogoutView` `auth_views.py:260` | `IsAuthenticated` | |
| `GET /me/` | `MeView` `auth_views.py:283` | `IsAuthenticated` | |
| `POST /password-reset/request/` | `PasswordResetRequestView` `auth_views.py:102` | `AllowAny` | Emails a reset token. |
| `POST /password-reset/confirm/` | `PasswordResetConfirmView` `auth_views.py:133` | `AllowAny` | Rotating the password force-logs-out all JWTs (via `pw_iat`). |
| `GET /tokens/` , `POST /tokens/create/` , `DELETE /tokens/<token_id>/` | `PATListView`/`PATCreateView`/`PATRevokeView` `apps/tenants/pat_views.py:44/54/99` | `IsAuthenticated` | Manage Personal Access Tokens (the `pat_…` bearer credentials). |
| `POST /authorize/` | `AuthorizeBeginView` `apps/tenants/oauth_views.py:57` | `IsAuthenticated` | Web→app PKCE handoff begin (iOS "Create an account"). |
| `POST /exchange/` | `ExchangeView` `oauth_views.py:92` | `AllowAny` | App swaps PKCE code+verifier for tokens. |

---

## 7. Cron / ops — `/api/cron/` and `/api/v1/cron/` (`apps/cron/urls.py`)

Both prefixes mount the same module (`config/urls.py:78`/`:79`). These are *not*
user endpoints; they are invoked by the QStash scheduler or by CI/CD + operators.
Two auth modes are used, and several endpoints accept **either**. All are
`csrf_exempt` + `require_POST` unless noted.

| Method + Path | View (path:line) | Auth |
|---|---|---|
| `POST /trigger/<task_name>/` | `trigger_task` `apps/cron/views.py:253` | **QStash-sig** (`verify_qstash_signature`, 401 on fail `:262`); dispatches via `TASK_MAP`, validates args against the task signature |
| `POST /trigger-debug/<task_name>/` | `trigger_task_debug` `views.py:369` | **DEBUG-gated**: 403 unless `settings.DEBUG` (`:376`) — *skips* signature check. Safe in prod (DEBUG=False). |
| `GET /tasks/` | `list_tasks` `views.py:467` | **DEBUG-gated** (403 in prod, `:473`) |
| `POST /apply-pending-configs/` | `apply_pending_configs` `views.py:481` | QStash-sig |
| `POST /expire-trials/` | `expire_trials` `views.py:788` | QStash-sig |
| `POST /restart-tenant-container/` | `restart_tenant_container` `views.py:693` | QStash-sig |
| `POST /force-reseed-crons/` , `/run-health-check/` , `/broadcast-message/` , `/dedup-crons/` | `views.py:643/1558/1304/1367` | **QStash-sig OR Deploy-secret** (accepts either) |
| `POST /bump-all-pending-configs/` | `bump_all_pending_configs` `views.py:843` | Deploy-secret |
| `POST /backfill-welcomes/` | `backfill_welcomes` `views.py:908` | Deploy-secret |
| `POST /register-system-crons/` | `register_system_crons` `views.py:973` | Deploy-secret |
| `POST /run-update-cron-prompts/` , `/run-backfill-lesson-embeddings/` , `/run-rewrite-lessons-actionable/` , `/run-reseed-lessons/` , `/verify-gateway-tools/` | `views.py:*` | Deploy-secret |
| `POST /rollout-byo-image-bump/` , `/rollout-byo-persona-refresh/` , `/rollout-atomic-bump/` | `views.py:1675/1741/1820` | Deploy-secret (one-shot fleet ops; ci-cd.yml is canonical caller) |
| `GET /atomic-bump-status/` , `/admin-health/` | `atomic_bump_status` `views.py:1951`, `admin_health_status` `views.py:1630` | Deploy-secret (`X-Deploy-Secret`) |

---

## Risks & improvement opportunities

- **[high] Internal-key is a bearer secret with no caller binding.** Any holder of
  a tenant's `internal_api_key` + tenant-id can drive that tenant's *entire*
  runtime surface (finance writes, task/goal mutation, memory, cron delivery,
  message send-to-user), and the handler runs as `service_role=True` so RLS
  offers no backstop. The gate is possession-only — no mTLS, no network isolation,
  no request signing. A leaked container env var or Key Vault mis-scope = full
  per-tenant runtime compromise. Consider request signing (HMAC over method+path+body)
  or short-lived tokens so a static leaked key can't be replayed indefinitely.
  (`apps/integrations/internal_auth.py:123`)

- **[high] The internal-key check is manual, not enforced by the framework.** Every
  runtime view is `permission_classes=[AllowAny]` and must remember to call
  `_internal_auth_or_401` / `_validate_internal_auth` as the first line. There are
  ~150 such handlers across 8 modules; a single new endpoint that forgets the call
  ships as fully unauthenticated with `service_role` RLS. A shared
  `BasePermission` (or DRF authentication class) would make the default safe
  instead of the default open. (`apps/*/runtime_views.py`)

- **[med] Two inconsistent internal-auth conventions.** The actions-gate endpoints
  use `X-Internal-Key`/`X-Tenant-Id` and return 403; everything else uses
  `X-NBHD-Internal-Key`/`X-NBHD-Tenant-Id` and returns 401. Same secret, two
  spellings, two status codes — easy to get wrong when copying a handler, and the
  gate path defaults the tenant-id header to the URL value (weaker cross-check).
  Unify on one header set + status. (`apps/actions/views.py:47`)

- **[med] Chart/meditation files rely on UUID-obscurity alone.** `serve_chart_image`
  / `serve_meditation_audio` are unauthenticated; anyone who learns a
  tenant-UUID + filename (e.g. via a forwarded LINE image URL, logs, or referrer
  leakage) can fetch that tenant's asset. The traversal regex is sound, but there
  is no per-request authorization or expiry. Consider signed, expiring URLs.
  (`apps/router/views.py:403`, `:507`)

- **[med] Cron ops surface trusts a single static `DEPLOY_SECRET` for
  destructive fleet actions** (container restarts, broadcast-message to all users,
  BYO image rollouts, atomic bumps). A leak grants fleet-wide control-plane
  operations. It never rotates automatically and is shared across CI + poller +
  operators. Scope-narrow (separate secrets per operation class) and/or move to
  short-lived signed requests. (`apps/cron/views.py`)

- **[med] `send-to-user` and `broadcast-message` are outbound-message primitives**
  reachable respectively via a per-tenant internal key and the deploy secret.
  Compromise of either lets an attacker send arbitrary messages *as the assistant*
  to the user (per-tenant) or the whole fleet (broadcast). Worth an explicit
  content/rate audit and alerting. (`apps/router/cron_delivery.py`, `apps/cron/views.py:1304`)

- **[low] Siri endpoints reuse the raw user JWT with no dedicated scope.** A token
  minted for the app has the same authority when replayed against `/api/v1/siri/`.
  Acceptable given it's the same user, but a scoped/attenuated token would reduce
  blast radius of a leaked device token. (`apps/router/siri_views.py`)

- **[low] `trigger-debug` / `tasks` are DEBUG-gated, not secret-gated.** Safe as
  long as production never runs with `DEBUG=True`; a misconfiguration would expose
  unsigned task execution. A belt-and-suspenders secret check would remove the
  dependency on one settings flag. (`apps/cron/views.py:369`, `:467`)

- **[low] BYO credential endpoints accept user-supplied LLM API keys.** Confirm the
  detail/list serializers are strictly write-only for the secret and never echo it
  back (not verified in this pass). (`apps/byo_models/views.py`)
