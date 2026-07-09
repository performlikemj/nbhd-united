# Authentication, Authorization & API Trust Boundaries

Audit of every trust boundary in the NBHD United control plane: how a caller
proves who it is, what it's then allowed to touch, and where those two things
can drift apart. Builds on the endpoint inventory in
[`../reference/api-surface.md`](../reference/api-surface.md) — that document
is the "what exists"; this one is the "is it actually enforced." Platform
invariants referenced below live in
[`../agents/invariants.md`](../agents/invariants.md).

Audience: security auditor. All claims below were verified by reading the
cited source, not inferred from docstrings.

---

## 1. Trust boundary inventory

| # | Boundary | Mechanism | Verified enforcement point |
|---|---|---|---|
| 1 | Console API (web/iOS user) | JWT (SimpleJWT + `pw_iat` force-logout) or PAT (`pat_…` bearer, SHA-256 lookup) | `apps/tenants/authentication.py:18` (JWT), `:97` (PAT) |
| 2 | Container → Django (internal runtime) | Per-tenant shared secret, `X-NBHD-Internal-Key` + `X-NBHD-Tenant-Id` | `apps/integrations/internal_auth.py:123` |
| 3 | Django's own poller → gate-respond | Single static `DEPLOY_SECRET`, `X-Deploy-Secret` | `apps/actions/views.py:301` |
| 4 | CI/CD + operators → cron ops | `DEPLOY_SECRET` or QStash signature | `apps/cron/views.py` (per-endpoint, §5) |
| 5 | Upstash QStash → cron trigger | `Upstash-Signature`, SDK-verified | `apps/cron/qstash_verify.py:10` |
| 6 | Stripe → webhook | HMAC (`stripe.Webhook.construct_event`) | `apps/billing/views.py:135` |
| 7 | Telegram → webhook | Shared secret, `hmac.compare_digest` | `apps/router/views.py:185` |
| 8 | LINE → webhook | HMAC-SHA256, `hmac.compare_digest` | `apps/router/line_webhook.py:64,77` |
| 9 | Web SPA → app PKCE handoff | Single-use code + S256 PKCE, constant-time compares | `apps/tenants/oauth_views.py:92` |
| 10 | Public unauthenticated | UUID-obscurity (charts/audio) or signed token (promo/unsubscribe/OAuth state/invite) | §6 |

---

## 2. Console API: JWT / PAT (`apps/tenants/authentication.py`)

- **JWT** (`JWTAuthenticationWithRLS`, `:18`) wraps SimpleJWT. Every access
  and refresh token carries a `pw_iat` claim = `password_last_changed_at` at
  mint time (`:74-87`). On every request the claim is compared against the
  live column; a mismatch raises `AuthenticationFailed("password_rotated")`.
  This means password rotation invalidates **every** outstanding JWT without
  a token-blacklist table — verified correct, including the legacy-token path
  (`pw_iat=0`, `:31-34`) which only rejects if a rotation has since occurred.
  On success, `set_rls_context(tenant_id, user_id)` is called (`:92`) — RLS
  is a real second line of defense here (see §7), unlike the internal-key
  surface.
- **PAT** (`PersonalAccessTokenAuthentication`, `:97`) reads
  `Authorization: Bearer pat_<secret>`, SHA-256-hashes it, and looks up
  `PersonalAccessToken.token_hash` (exact match, not constant-time — but this
  is a DB unique-index lookup, not a comparison against a fixed value, so
  there's no meaningful timing oracle). Checks `is_valid` (not revoked, not
  expired), sets RLS the same way as JWT, and throttles the `last_used_at`
  write to once/minute. Stashes `request.auth_pat = pat` for scope checks.

### 2a. PAT scopes are declared but almost never enforced — **[high][open]**

`PersonalAccessToken.scopes` (`apps/tenants/pat_models.py:53`) is a
user-visible JSONField (shown in the token-creation UI,
`apps/tenants/pat_views.py:90`), restricted at creation to
`ALLOWED_PAT_SCOPES = {"sessions:write", "sessions:read"}`
(`apps/tenants/permissions.py:12`). The enforcement class `HasPATScope`
(`permissions.py:15`) is explicit that **JWT requests bypass scope checks
entirely** (`:24-29`, "JWT requests pass through") — by design, since a JWT
represents full session-level access. That's correct for JWT. The problem is
where `HasPATScope`/`HasSessionsWriteScope`/`HasSessionsReadScope` are
actually wired in:

```
$ grep -rn "HasSessionsWriteScope\|HasSessionsReadScope\|HasPATScope" apps --include="*.py"
apps/journal/session_views.py:85   permission_classes = [HasSessionsWriteScope]
apps/journal/session_views.py:148  permission_classes = [HasSessionsReadScope]
apps/journal/session_views.py:200  permission_classes = [HasSessionsReadScope]
```

Three views, all in one file (the YardTalk session-push endpoints). Every
other `IsAuthenticated` console view — confirmed by direct read:
`DeleteAccountView` (`apps/tenants/views.py:648`), billing `/portal/`,
`/checkout/`, `/credits/checkout/` (`apps/billing/views.py:202,261,429`),
`BYOCredentialListView`/`BYOCredentialDetailView` (`apps/byo_models/views.py:65,161`),
plus every fuel/finance/core/insights/journal/automations/lessons/friends
console endpoint in §2 of the API surface doc — uses plain `IsAuthenticated`,
which a PAT satisfies identically to a JWT. **A PAT minted (and shown to the
user) as "YardTalk session push access" is in practice a full-account bearer
credential**: it can open the Stripe billing portal, paste/delete BYO model
credentials, and call `DeleteAccountView` to delete the account. The `scopes`
field creates a UI expectation of least-privilege that the API does not
back up outside of three routes.

**Recommendation:** either (a) make `HasPATScope`-style enforcement the
default for PAT-authenticated requests (deny unless the view opts in a scope
the PAT holds), or (b) if PATs are meant to be full-account credentials,
remove the `scopes` UI/field so users aren't told they're issuing a
narrowly-scoped token.

---

## 3. Internal runtime endpoints (container → Django)

Confirms the ground truth in the API-surface doc: `validate_internal_runtime_request`
(`apps/integrations/internal_auth.py:123`) is the single chokepoint, per-tenant
key only (global fallback removed 2026-06-22, `:15-23`), constant-time compared
(`secrets.compare_digest`, `:179`), and every failure/success is audit-logged
with `key_provenance`/`outcome` (`:59-94`).

### 3a. Coverage audit — every `AllowAny` runtime handler calls the validator

Grepped every file carrying `permission_classes = [AllowAny]` outside test
code (17 files) and cross-checked each view class against a call to
`_internal_auth_or_401` / `validate_internal_runtime_request`. Verified by
counting HTTP-method handlers (`get`/`post`/`put`/`patch`/`delete`) per class
against auth-check call sites within that class's line range, and manually
re-reading every "the auth call is inside a shared helper" case, e.g.
`RuntimeWorkoutDetailView` (`apps/fuel/runtime_views.py:215`) — `patch` and
`delete` both route through `_get_workout()` (`:220`), which calls the
validator first (`:221`) before touching the DB. **No handler found that
skips the check.** Coverage by module:

| Module | Views | Auth pattern |
|---|---|---|
| `apps/integrations/runtime_views.py` | ~65 view classes, ~85 HTTP methods (the largest surface: goals/tasks, Gmail, Calendar, journal, lessons, workspaces, Reddit, cron typed patterns) | `_internal_auth_or_401` (`:141`), first line of every method |
| `apps/fuel/runtime_views.py` | 13 view classes | `_internal_auth_or_401` (`:82`), incl. shared-helper pattern above |
| `apps/insights/runtime_views.py` | 11 view classes | `if err := _internal_auth_or_401(...)` (`:65`) |
| `apps/finance/runtime_views.py` | 7 view classes | `_internal_auth_or_401` (`:33`) |
| `apps/core/runtime_views.py` | 4 view classes | `_internal_auth_or_401` (`:38`) |
| `apps/tenants/runtime_views.py` | 4 view classes | `_internal_auth_or_401` (`:36`) |
| `apps/cron/runtime_views.py` | 1 view class | `_internal_auth_or_401` (`:30`) |
| `apps/journal/runtime_purpose_views.py` | 6 view classes | `_internal_auth_or_401` (`:36`) |
| `apps/router/chat_views.py` (`ChatProgressEventView`) | 1 | inline `validate_internal_runtime_request` call (`:938`) |
| `apps/platform_logs/views.py` (`PlatformIssueReportView`) | 1 | inline `validate_internal_runtime_request` call (`:51`) |
| `apps/common/query_view.py` (`BaseQueryView`) | base class for finance/fuel/journal/insights `query/` endpoints | **centralized** — `_auth()` runs inside `post()` (`:127-130`) before `execute()`; subclasses cannot forget the check because they never implement `post()` |
| `apps/actions/views.py` (gate) | `GateRequestView`, `GatePollView` | separate helper `_validate_internal_auth` (`:47`), see §3b |

`BaseQueryView` (`apps/common/query_view.py:114`) is the strongest pattern on
the surface: because the auth call lives in the base class's `post()` and
subclasses only override `execute()`, a new query endpoint **cannot**
structurally skip the check the way a hand-rolled `APIView.post()` could.
Every other module relies on each handler remembering to call
`_internal_auth_or_401` as its first statement — verified present everywhere
today, but this is a **manual convention, not a structural guarantee**. A
future handler that forgets the call is `AllowAny` + `authentication_classes
= []`, i.e. **fully open**, and nothing short of code review catches it. The
regression-test-pinned invariants pattern used elsewhere in this codebase
(e.g. `test_memorysearch_disabled_and_denied`, invariants.md §1) has no
analogue here — there is no test that asserts "every `AllowAny` runtime view
calls the validator."

**Recommendation [med][open]:** add a lint/test guard (a simple AST or regex
scan over `apps/**/runtime_views.py` failing CI if an `AllowAny` class body
lacks an auth-helper call) so this stays true by construction, not by audit.

### 3b. Two-header-convention inconsistency, and a masked default — **[low][partially-mitigated]**

Confirmed both conventions exist exactly as flagged in the API-surface doc:

- Standard: `X-NBHD-Internal-Key` / `X-NBHD-Tenant-Id`, missing header → `""`
  → `validate_internal_runtime_request` raises `OUTCOME_MISSING_TENANT` /
  `OUTCOME_MISSING_KEY`, view returns **401**.
- Actions-gate (`apps/actions/views.py:47-56`): `X-Internal-Key` /
  `X-Tenant-Id`, view returns **403**. More importantly, the tenant-id header
  read is:

  ```python
  provided_tenant_id=request.headers.get("X-Tenant-Id", str(tenant_id)),
  ```

  (`apps/actions/views.py:52`) — if the container omits `X-Tenant-Id`
  entirely, the default is the **URL's own** `tenant_id`, which is exactly
  what `expected_tenant_id` will be compared against. This can never trigger
  `OUTCOME_MISSING_TENANT`, and it means the tenant-mismatch check is
  structurally dead for this call path (every request "passes" the tenant
  check regardless of whether the header was sent — the key check is doing
  100% of the work instead of the intended two-factor URL+header agreement).
  Not independently exploitable — the attacker still needs the correct
  per-tenant key — but it's a real deviation from the two-header design
  described in `internal_auth.py`'s own docstring, and it silently defeats an
  audit signal (a client that stops sending the tenant header would not be
  caught). `GatePollView` uses the same helper. Every other module's
  `_internal_auth_or_401` defaults the header to `""` (`apps/integrations/runtime_views.py:145`
  et al.), which fails closed correctly.

**Recommendation:** change `apps/actions/views.py:52`'s default to `""` to
match every other module, and standardize the header name / status code
(`401` vs `403`) while touching the file.

---

## 4. Action gating — deploy-secret leg (`GateRespondView`)

`POST /api/v1/gate/<action_id>/respond` (`apps/actions/views.py:288`) is
called by Django's own Telegram/LINE button-callback handler, not a tenant
container, so it uses `X-Deploy-Secret` instead of the per-tenant key:

```python
provided = request.headers.get("X-Deploy-Secret", "")
if provided != deploy_secret:
```

(`:307-308`) — a **plain string `!=`**, not `hmac.compare_digest`. Same
pattern repeats across essentially every `DEPLOY_SECRET` check in
`apps/cron/views.py` (18 occurrences: lines 681, 881, 946, 1013, 1063, 1107,
1147, 1189, 1227, 1266, 1326, 1521, 1596, 1643, 1706, 1810, 1922) and
`apps/journal/extraction_views.py:39`. By contrast, the webhook and OAuth-code
paths in this codebase consistently use `hmac.compare_digest` (Telegram
`apps/router/views.py:185`, LINE `apps/router/line_webhook.py:77`, PKCE
`apps/tenants/oauth_views.py:131,145`) — this file group is the outlier.

**Severity assessment:** `DEPLOY_SECRET` is a single static, long-lived,
fleet-wide secret already flagged in the API-surface doc as high blast-radius
(it gates `bump-all-pending-configs`, `backfill-welcomes`,
`register-system-crons`, and the gate approve/deny callback). A network-level
timing attack against `!=` over HTTPS is impractical in practice (jitter
dominates), but given how many destructive endpoints share this one secret
and how cheap the fix is, this is worth closing.

**Recommendation [low][open]:** replace `provided != deploy_secret` /
`provided == deploy_secret` with `hmac.compare_digest(provided, deploy_secret)`
across `apps/cron/views.py`, `apps/actions/views.py:308`, and
`apps/journal/extraction_views.py:39`.

---

## 5. Cron / ops surface

Confirmed against `apps/cron/views.py`: every mutating cron endpoint checks
either QStash signature (`verify_qstash_signature`, real crypto via the
Upstash SDK) or `DEPLOY_SECRET` (see §4 for the comparison-safety caveat), a
few accept either. `trigger-debug/` and `list_tasks` are gated on
`settings.DEBUG` (`views.py:376,473`) rather than a secret — confirmed dead
in prod since `DEBUG=False` is enforced there; this is a correct pattern but
worth a regression test pinning `DEBUG=False` in the prod settings module if
one doesn't already exist, since it's the entire access control for those two
routes.

No new findings beyond what `../reference/api-surface.md` §7 already
documents; this section exists to record that the claims were verified
in-code, not just described.

---

## 6. Unauthenticated public endpoints

Verified each:

- **Chart PNGs / meditation audio** (`apps/router/views.py:403,507`) — no
  auth, access control is `tenant_id` (a UUID, high entropy) + a filename
  regex (`^[\w-]+\.png$` / `^[\w-]+\.(mp3|ogg)$`) that also serves as a
  path-traversal guard. **[low][by-design]** — acceptable for this content
  class (rendered chart images, meditation audio; not PII-bearing secrets),
  but note there's no expiry: a URL that leaks (referrer header, shared
  screenshot with URL visible, proxy log) is valid forever. If this content
  is ever extended to something more sensitive, switch to signed/expiring
  URLs rather than reusing the obscurity model.
- **OAuth / Composio callbacks** (`apps/integrations/views.py:285,296,378`) —
  `AllowAny`, authorization carried by `django.core.signing.dumps(...,
  salt="oauth")` / `.loads(..., max_age=OAUTH_STATE_MAX_AGE_SECONDS)`
  (`:112,118`). Django's `signing` module HMAC-signs with `SECRET_KEY` and
  enforces the `max_age` — correct use. **[low][by-design]**.
- **Friend invite preview** (`InviteDetailView`,
  `apps/friends/views.py:123` → `invite_metadata`,
  `apps/friends/services.py:388-400`) — confirmed the response body is
  exactly `{inviter_display_name, inviter_handle, inviter_hue, valid}`, no
  tenant id, email, or any other field. **[low][by-design]**.
- **Promo redeem / unsubscribe** — HMAC token in the URL is the entire
  authorization; confirmed `redeem_promo` (`apps/tenants/promo_views.py:44`)
  calls `verify_promo_token` before any side effect and collapses distinct
  failure reasons to opaque `status=` redirect codes without leaking which
  step failed except via server-side logs. **[low][by-design]**.
- **PKCE exchange** (`ExchangeView`, `apps/tenants/oauth_views.py:92`) — this
  is the standout: single-use authorization code (row-locked
  `select_for_update`, `consumed_at` re-checked inside the lock, `:110-128`),
  redirect-URI binding and PKCE verifier both checked with
  `hmac.compare_digest` (`:131,145`), and the PKCE branch deliberately runs
  `compare_digest` even when the verifier can't be base64-decoded (`:141-144`)
  so a malformed verifier is timing-indistinguishable from a valid-shape
  mismatch. Every failure path returns an identical generic `400
  invalid_grant` (`:47-54`). **No findings — this is a model implementation**;
  worth using as the internal reference for how token-exchange endpoints on
  this platform should be built.

---

## 7. RLS as a backstop — confirmed *not* effective on the internal-runtime surface

Verified the mechanism the API-surface doc asserts: `set_rls_context(...,
service_role=True)` (`apps/tenants/middleware.py:21-45`) sets the Postgres
session GUC `app.service_role = 'true'`. Checked the actual RLS policy SQL
(`apps/friends/migrations/0008_friends_rls_backstop.py:44-47`):

```python
_GUC_SERVICE = "coalesce(current_setting('app.service_role', true), '') = 'true'"
```

used as an unconditional OR-branch in every SELECT policy on the friends
tables (`shared_lessons`, etc., `:95-100`) — i.e. `service_role=True`
**bypasses tenant scoping entirely** for any table whose policy includes this
clause, by design ("service_role for trusted background work"). Every
internal-runtime handler sets `service_role=True` right after auth succeeds
(`_internal_auth_or_401`, e.g. `apps/tenants/runtime_views.py:48`). This
confirms the API-surface doc's claim precisely: **on the internal-runtime
surface, the per-tenant key check is the only tenant-isolation control.**
RLS, which is a real second line of defense for the JWT/PAT console surface
(§2), provides no protection here — a handler that skipped
`_internal_auth_or_401` (see §3a) would not be caught by RLS, because the
handler itself sets `service_role=True` on success and queries would run
fully unscoped if that gate were bypassed. This raises the stakes of the §3a
recommendation (structural CI guard) — it's the only realistic backstop for
"a future handler ships without the auth call."

Separately, `apps/tenants/migrations/0059_lock_down_public_schema_rls.py`
confirms only `postgres`/`service_role`/`supabase_admin` (DB roles, not to
be confused with the `app.service_role` GUC) are `BYPASSRLS`-capable at the
Postgres level, and `anon`/`authenticated` Supabase API roles have all
`public.*` privileges revoked — this is the outer perimeter and is unrelated
to the `app_user` GUC-based policies discussed above.

---

## 8. IDOR spot-check on console endpoints

Sampled object-lookup patterns across `TenantViewSet`, `LessonViewSet`,
journal document/lifecycle views, `AutomationListCreateView`/`AutomationDetailView`,
and PAT management views. Every `.objects.get(...)` / `.objects.filter(...)`
against a caller-supplied id includes an explicit tenant or user scope:

- `TenantViewSet.get_queryset` — `Tenant.objects.filter(id=self.request.user.tenant.id)` (`apps/tenants/views.py:33`)
- `LessonViewSet.get_queryset` — `Lesson.objects.filter(tenant=self.request.user.tenant)` (`apps/lessons/views.py:62`); the one bare `.get(id=target_id, ...)` found (`views.py:651`, the `connect` action) also carries `tenant=self.request.user.tenant` in the same call
- `journal/lifecycle_views.py` — every `Task`/`Goal` lookup is `.filter(tenant=tenant, id=...)`
- `PATRevokeView` — `PersonalAccessToken.objects.get(id=token_id, user=request.user)` (`pat_views.py:106`)
- `BYOCredentialDetailView.delete` — `BYOCredential.objects.get(id=cred_id, tenant=tenant)` (`apps/byo_models/views.py:169`)

No unscoped lookup found in the sample. This is not exhaustive across the
full console surface (§2b of the API-surface doc lists ~90 endpoints across 9
pillar apps); the pattern is consistent enough across pillars that a targeted
IDOR fuzz pass (authenticated as tenant A, iterate tenant-B object ids across
each `<id>/` route) would be higher-value than further manual sampling.
**[low][open]** — recommend an automated cross-tenant-id fuzz test as a
follow-up rather than continued manual review.

---

## 9. BYO credential secret handling — confirmed write-only

`apps/byo_models/views.py` has no separate serializer module — responses are
built by the module-level `_serialize()` helper (`:50-59`), which emits only
`id, provider, mode, status, last_verified_at, last_error, created_at`. The
`token` field is read from `request.data` (`:90`) and passed directly into
`upsert_credential(...)` without ever being assigned to a variable that
appears in a log call — confirmed no log statement in this file interpolates
`token`, `request.data`, or `request.body` (module docstring's claim,
`:14-17`, verified against source). `BYOCredentialListView.get` returns
`_serialize()` output only, so the secret is never round-tripped to the
client after creation. A defensive logging filter
(`apps.byo_models.logging_filters.RedactBYOPasteBody`) scrubs the paste path
as belt-and-suspenders. **No finding — confirmed as designed.**

---

## Findings

- **[high][open]** PAT `scopes` are enforced on only 3 of the ~90+
  `IsAuthenticated` console endpoints (`apps/journal/session_views.py:85,148,200`,
  via `HasSessionsWriteScope`/`HasSessionsReadScope`,
  `apps/tenants/permissions.py:34-39`). Everywhere else — billing portal/
  checkout, delete-account, BYO credential management, fuel/finance/journal/
  friends data — a PAT (nominally scoped to `sessions:write`/`sessions:read`
  in the token-creation UI) is accepted by plain `IsAuthenticated` exactly
  like a full JWT session. A leaked "YardTalk push" PAT is a full-account
  bearer credential in practice. Fix: default-deny unscoped PAT access on
  sensitive views, or drop the scopes UI if PATs are intentionally
  full-access. See §2a.

- **[med][open]** The internal-runtime auth check
  (`validate_internal_runtime_request`) is enforced by convention (each
  handler must remember to call it as its first line) everywhere except
  `apps/common/query_view.py`'s `BaseQueryView`, where it's structurally
  centralized in the base class. Verified present on every current `AllowAny`
  handler across 8+ modules (~150 routes), but nothing prevents a future
  handler from shipping without it — and per §7, RLS provides zero backstop
  on this surface if that happens (handlers set `service_role=True` on
  success, bypassing tenant scoping). Fix: add a CI-enforced structural check
  (AST/regex scan) that every `AllowAny` view in `*/runtime_views.py`
  contains an auth-helper call, mirroring how `invariants.md` §1 is pinned by
  a regression test. See §3a.

- **[low][open]** `DEPLOY_SECRET` is compared with plain `==`/`!=` string
  comparison (not `hmac.compare_digest`) in 20 call sites across
  `apps/cron/views.py`, `apps/actions/views.py:308`, and
  `apps/journal/extraction_views.py:39` — inconsistent with the
  `compare_digest` discipline used everywhere else in the codebase (webhooks,
  PKCE exchange, per-tenant internal key). Low practical exploitability over
  HTTPS, but cheap to fix given this one secret gates fleet-wide destructive
  operations. See §4.

- **[low][partially-mitigated]** `apps/actions/views.py:52`'s
  `_validate_internal_auth` defaults a missing `X-Tenant-Id` header to the
  URL's own `tenant_id` instead of `""`, silently defeating the
  tenant-mismatch check for `GateRequestView`/`GatePollView` (the per-tenant
  key check still gates access, so this isn't independently exploitable, but
  it deviates from the two-factor URL+header design and from every other
  module's `_internal_auth_or_401`, which defaults to `""` and fails closed).
  See §3b.

- **[low][open]** No automated cross-tenant IDOR fuzz coverage exists for the
  console API. Manual sampling across `TenantViewSet`, `LessonViewSet`,
  journal, PAT, and BYO-credential views found every object lookup correctly
  tenant/user-scoped, but this was not exhaustive across the ~90-endpoint
  console surface. See §8.

- **[low][by-design]** Chart/meditation-audio endpoints
  (`apps/router/views.py:403,507`) use permanent UUID-obscurity with no
  expiry — acceptable for this content class today; revisit if this pattern
  is ever reused for more sensitive content. See §6.

- **[by-design, confirmed]** `PersonalAccessTokenAuthentication` uses a
  DB-index hash lookup rather than a fixed-value comparison, so the lack of
  `compare_digest` there is not a timing concern. See §2.

- **[by-design, confirmed]** BYO credential secrets are write-only end to
  end — no serializer or log path was found that returns or logs the raw
  token. See §9.

- **[by-design, confirmed]** RLS `service_role` GUC bypass is intentional
  platform architecture for trusted background work, correctly gated by the
  internal-key check being the sole real control on that surface (not a
  defense-in-depth layer). See §7.

- **[by-design, confirmed]** The PKCE web→app exchange
  (`apps/tenants/oauth_views.py`) is a model implementation: single-use
  locked codes, constant-time redirect/PKCE comparisons including the
  malformed-verifier branch, and uniform generic error responses. No finding;
  cited as the reference pattern for future token-exchange work. See §6.
