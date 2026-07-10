# Multi-Tenant Isolation & Row-Level Security

How NBHD United actually keeps one subscriber's data away from another's — as
implemented today, not as originally designed. Builds on
[`../reference/data-model.md`](../reference/data-model.md) (what data exists,
per-table tenant-scope column, the existing "Tenant isolation & RLS" section
this doc expands on), [`../reference/api-surface.md`](../reference/api-surface.md)
(the full endpoint catalog), [`../agents/invariants.md`](../agents/invariants.md),
and [`authn-authz-and-api-surface.md`](authn-authz-and-api-surface.md) (trust
boundaries for *who* can call an endpoint — this doc is about what happens to
the query once they're in). All claims below were verified against source in
this checkout; DB-role facts are the orchestrator's live `pg_roles` query
against prod, treated as authoritative per the task brief.

> **Verified update (2026-07-09).** A live `pg_stat_activity` read settles §9's
> open question: the Django app serves requests as **`app_user` via the Supavisor
> pooler** — the only application role with runtime connections (`authenticator`
> is PostgREST/Data-API; `postgres`/`supabase_admin` are dashboard + pg_cron).
> Since `app_user` is confirmed non-BYPASSRLS and does not own the tables, the
> three-table `FORCE`-RLS Neighborhood backstop **does bind** for normal
> app queries — the in-repo comments (`0059`, `0106`, `apps/friends/access.py`)
> that say "Django connects as `postgres` (BYPASSRLS)" are **stale**.
>
> **Why RLS is off at all (the answer to "doesn't Django handle the DB?").** Yes —
> and that's exactly why it must be off. `app_user` is non-BYPASSRLS and the
> general tables have RLS *enabled by migrations but carry no policies*. In
> Postgres, "RLS on + no policy" means **deny-all** for a non-owner, non-bypass
> role — so if RLS were left on, Django would be locked out of its own tables.
> `disable_rls` runs every boot to strip it so the app can read/write, and
> tenant isolation is enforced in the query layer instead. Crucially, the
> protection against the Supabase anon Data API is the **`REVOKE ALL` from
> `anon`/`authenticated`** (verified live: those roles hold zero privileges on
> `journal_entries`/`friend_messages`), *not* the RLS-enabled bit — so stripping
> RLS at boot re-exposes nothing. New tables inherit this via
> `ALTER DEFAULT PRIVILEGES … REVOKE ALL … FROM anon` and are auto-disabled by
> the boot sweep; the only manual step is a *new cross-tenant* table that needs
> its own `FORCE`-RLS backstop (e.g. `friend_sky_memberships`, pending per
> `0106`'s own note — it is not yet in `RLS_KEEP_ENABLED`).

---

## 1. The model in one paragraph

Tenant isolation is **application-layer query filtering**, full stop, for
162 of 165 owned tables. Postgres RLS exists in the schema (every table has
`ENABLE ROW LEVEL SECURITY` from the migration history) but is switched back
**off** on every boot for everything except three cross-tenant "Neighborhood"
tables, which carry a real, `FORCE`-scoped policy set. The thing that
actually blocks the Supabase anon/PostgREST Data API from reading tenant data
is not RLS-enabled state at all — it's a one-time `REVOKE ALL` that survives
the RLS toggling. §5–§7 below walk each piece; §9 reconciles this against
what the design doc and in-repo comments claim.

---

## 2. How tenant identity is established and propagated

Three request classes set Postgres session GUCs that RLS policies (where any
apply) key off. The **auth mechanism** for each is [`authn-authz-and-api-surface.md §1–§3`](authn-authz-and-api-surface.md);
this section covers what happens to the *database connection* once auth
succeeds.

`set_rls_context()` / `reset_rls_context()` (`apps/tenants/middleware.py:21-72`)
run `SELECT set_config('app.tenant_id', …, false)` etc. in one round trip
(session-scoped, `false` = persists for the connection's lifetime, not just
the transaction). Three call sites set it:

| Caller | `app.tenant_id` | `app.user_id` | `app.service_role` | Where |
|---|---|---|---|---|
| `JWTAuthenticationWithRLS.authenticate()` | ✓ | ✓ | — | `apps/tenants/authentication.py:89-92` |
| `PersonalAccessTokenAuthentication.authenticate()` | ✓ | ✓ | — | `apps/tenants/authentication.py:133-136` |
| `TenantContextMiddleware.process_request()` | ✓ (redundant w/ above for API calls; catches session-authenticated requests) | ✓ | — | `apps/tenants/middleware.py:83-92` |
| Every internal-key runtime view's `_internal_auth_or_401` helper | ✓ (from URL) | — | ✓ | e.g. `apps/finance/runtime_views.py:46`, `apps/tenants/runtime_views.py:36` (~8 modules, ~150 routes) |
| Webhooks (Stripe/Telegram/LINE) + cron trigger views | — | — | ✓ | `apps/router/views.py:192`, `apps/router/line_webhook.py:877`, `apps/billing/views.py:148`, `apps/cron/views.py:294` (+5 more sites) |

`reset_rls_context()` clears all three GUCs in `process_response`
(`middleware.py:96-101`), but only if this thread actually set something
(`rls_dirty` flag) — an optimization so 404s/anonymous requests don't open a
DB connection just to reset nothing. The reset call is wrapped in a bare
`except Exception: pass` ("Connection may already be closed").

**Doc-vs-reality gap on connection lifetime.** `docs/rls-tenant-isolation.md:50-51`
claims "Django's default `CONN_MAX_AGE=0` also closes connections after each
request as a safety net." Production does the opposite:
`config/settings/production.py:45` sets
`DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=600)`
— persistent, pooled connections reused across requests on the same
worker thread. Practically this is low-risk *today*: the very next
authenticated request on that thread re-sets `app.tenant_id`/`app.user_id`
in `process_request`/`authenticate()` before any tenant-scoped query runs, so
a stale value gets overwritten, not read. The narrower, real exposure is a
request that (a) reuses a connection that still carries a stale GUC from a
prior tenant/service-role context because a `reset_rls_context()` call
silently failed, and (b) itself never calls `set_rls_context()` at all before
querying (an unauthenticated GET, or a webhook path that queries before its
own `set_rls_context(service_role=True)` call). See finding F6.

---

## 3. Application-layer filtering — the isolation mechanism for 162/165 tables

The dominant, verified pattern across pillar apps: resolve the top-level
object with an explicit `tenant=` (or `id=…, tenant=…`) filter derived from
**server-established** identity (JWT's `request.user.tenant`, or the
internal-key view's URL-derived `tenant`), then filter children transitively
through that already-scoped parent — never through a second independent
lookup of the child's own id.

```python
# apps/finance/services.py:277 — resolve_account()
account = FinanceAccount.objects.filter(id=account_id, tenant=tenant, is_active=True).first()

# apps/finance/runtime_views.py:81 — list, using the URL-derived tenant
qs = FinanceAccount.objects.filter(tenant=tenant)

# apps/fuel/runtime_views.py — children scoped transitively through an
# already-tenant-checked `plan`, never a second bare id lookup
Workout.objects.filter(plan=plan)
```

[`authn-authz-and-api-surface.md §8`](authn-authz-and-api-surface.md) spot-checked
the **console** (JWT/PAT) surface with the same conclusion — every sampled
`.objects.get/.filter` on a caller-supplied id carried an explicit
tenant-or-user scope, no unscoped lookup found, but flagged the sample as
non-exhaustive and recommended an automated cross-tenant-id fuzz pass. That
recommendation applies equally to the **internal-key runtime** surface (§3
here is finance/fuel-only spot checks); neither audit is exhaustive across
~90 console + ~150 runtime endpoints, and — this is the structural point —
**this pattern has no CI enforcement**. Nothing fails a build if a future
endpoint does `Model.objects.get(id=request.data["id"])` with no tenant
clause. Contrast with §4, where the same risk class *is* CI-enforced for one
subsystem.

`data-model.md`'s own risk list already flags `LessonConnection` /
`TutoringSession` as carrying **no** tenant column at all (transitive-only
scoping through `Lesson`/star) — not re-derived here, see
[`data-model.md` → Risks](../reference/data-model.md#risks--improvement-opportunities).

---

## 4. The friends cross-tenant accessor — the one audited chokepoint

`apps/friends/access.py` is the **only** app in the schema where a single row
legitimately spans two tenants (`data-model.md`'s `cross-tenant` scope
marker). Design:

- **Confinement.** `SharedLesson`, `FriendMessage`, `SharedGoal`,
  `LessonShareGrant` `.objects` may be touched **only** in `access.py`; so may
  `SkyMembership` (private per-viewer curation — not cross-tenant content, but
  a preference that must stay self-scoped, `access.py:849-947`). `Lesson.objects`
  (the raw, un-scrubbed corpus) may not be touched **anywhere** under
  `apps/friends/`, including `access.py` itself — friend paths read only the
  frozen, PII-scrubbed `SharedLesson` snapshot.
- **Enforcement.** `apps/friends/test_access_chokepoint.py:111-164` — an AST
  walk (not string-grep) over every `.py` under `apps/friends/` (excluding
  migrations/tests) plus two named runtime-view files
  (`apps/integrations/runtime_views.py`, `apps/lessons/views.py`,
  `test_access_chokepoint.py:70-73`), failing the build on any
  `Model.objects` attribute access to a guarded model outside `access.py`.
  Documented evasion: an aliased import (`from .models import SharedLesson as X`)
  is undetected (`test_access_chokepoint.py:32-35`) — not exploited in this
  checkout, just a known limitation of the AST match.
- **Addressing.** Always by opaque `friendship_id`/`thread_id`/`grant_id`,
  never a client-supplied `tenant_id`. The docstring's claim is "IDOR defeated
  by construction" — **verified** below rather than taken on faith.

### 4a. Re-verification at every raw-ID lookup — verified call-by-call

`access.py` exposes several `.get(id=…)`-style lookups with **no** tenant
filter in the query itself (`get_shared_lesson` `:309`, `get_grant` `:409`).
That's only safe if every caller re-verifies the resolved row belongs to (or
is visible to) the caller before acting on it. Traced every call site:

| Call site | Lookup | Re-verification? |
|---|---|---|
| `access.py:554` `adopt_shared_lesson` | `get_shared_lesson(id)` | ✓ — re-checks `shared_star_qs(viewer, owner).filter(id=…).exists()` before adopting (`:565`) |
| `apps/friends/scrub.py:159` | `access.get_shared_lesson(id)` | N/A — internal scrub worker under `backstop_service_context()`, not a caller-facing IDOR surface |
| `apps/friends/services.py:862` `revoke_share` | `access.get_grant(grant_id)` | ✓ — checks `grant.shared_lesson.owner_tenant_id == owner_tenant.id` **and** `source_lesson_id == lesson.id` before revoking (`services.py:863-868`); mismatch → `NotFound`, no-reveal |
| `apps/friends/circles.py:267` `report_content` | `access.get_shared_lesson(target_id)` | **✗ — gap, see F4** |

`assert_neighbors` (`access.py:112-129`) and `assert_participant`
(`access.py:642-659`) are themselves the re-verification primitive for the
`Friendship`/`FriendThread` object graph — both resolve by raw id then
explicitly check the caller is a party, raising `PermissionDenied`/`NotFound`
(no existence leak) otherwise. Confirmed correct.

### 4b. The chokepoint's own boundary — a hand-rolled exception outside it

`access.py`'s module docstring claims "EVERY cross-tenant read in the whole
feature — console, runtime, wormhole, chat, Missions — routes through these
functions. Nothing else hand-rolls a cross-tenant query" (`access.py:1-10`).
That's not quite true: `apps/router/friends_callbacks.py:75,110` (Telegram/LINE
inline-button accept/decline handlers) does
`Friendship.objects.filter(id=friendship_id).first()` directly, then
independently re-implements the party check (`edge.addressee_id != tenant.id`,
`:81,113`) rather than calling `access.assert_neighbors`. This is **not** a
live vulnerability — `Friendship` isn't one of the AST-guarded
`CROSS_TENANT_MODELS` (it's the low-sensitivity "consent atom," not scrubbed
content), and the hand-rolled check is correct — but it does mean:
(a) the docstring overstates coverage, and (b) neither `Friendship` nor
`NeighborProfile` (also queried directly here, `:53`) has *any* chokepoint,
so a **future** hand-rolled query against either, written without the same
care, would not be caught by anything. Low severity given the current code is
safe; worth tightening the docstring and, if this pattern recurs, routing
through `assert_neighbors` for consistency. **[low][open]**

---

## 5. `disable_rls` — what it does and why it's still running

`startup.sh:7-8` runs, on **every container boot**, after migrations and
before gunicorn starts:

```bash
DATABASE_URL="${ADMIN_DATABASE_URL:-$DATABASE_URL}" python manage.py disable_rls || true
```

`apps/tenants/management/commands/disable_rls.py:37-56` selects every table
owned by the connecting (admin/`postgres`) role with `rowsecurity = true` and
runs `ALTER TABLE … DISABLE ROW LEVEL SECURITY` on all of them **except**
`RLS_KEEP_ENABLED = {shared_lessons, lesson_share_grants, friend_messages}`
(`:21-27`). Its own docstring states the reason: "Supabase re-enables RLS on
tables created by migrations" — a platform-level behavior on new tables, not
something this repo's migrations do — "Run this after every `migrate` to
ensure the application can read/write without per-row policies blocking
access" (`:1-5`). That last clause is load-bearing: it only makes sense if
the runtime role does **not** own the tables and is **not** BYPASSRLS — an
RLS-enabled table with zero permissive policies returns zero rows to a
non-owner, non-bypassing role, which would break the app outright. See §9.

**Stale docstring, flagged for correction.** `apps/tenants/migrations/0059_lock_down_public_schema_rls.py:1-6`
states: "Django connects as `postgres` (BYPASSRLS) and bypasses RLS, so
enabling RLS on every public table has no effect on the app… **The matching
`disable_rls` management command and its `startup.sh` invocation are removed
in the same change.**" That removal did not stick, or was reverted: both
`startup.sh:8` and `disable_rls.py` are live in this checkout, and
`disable_rls.py`'s own docstring was subsequently edited for the PR8 friends
exception ("EXCEPTION (PR8 friends DB backstop)…", `disable_rls.py:7-14`) —
i.e. the file has been actively maintained *after* 0059 claimed it was gone.
An auditor who reads only 0059 would wrongly conclude RLS is fully enforced
fleet-wide today. **[med][open]** — correct the 0059 docstring, or restore
whatever change actually removed the `startup.sh` call if that was the
intent.

---

## 6. The three-table FORCE-RLS backstop

`apps/friends/migrations/0008_friends_rls_backstop.py` adds real,
fail-closed policies on the three highest-blast-radius cross-tenant tables —
the only tables `disable_rls` exempts. Verified predicates:

| Table | SELECT visibility (`0008_friends_rls_backstop.py`) |
|---|---|
| `shared_lessons` (`:96-110`) | `service_role` OR `owner_tenant_id = GUC` OR an active grant exists on this snapshot (reach-filtering delegated to the `lesson_share_grants` policy below — non-recursive by construction) |
| `lesson_share_grants` (`:113-122`) | `service_role` OR the GUC tenant is a party to the grant's `friendship` (status=accepted) OR an active member of its `circle` |
| `friend_messages` (`:124-137`) | `service_role` OR the GUC tenant is an active (`left_at IS NULL`) member of the message's thread |

All three **fail closed** on an unset GUC:
`nullif(current_setting('app.tenant_id', true), '')::uuid` → `NULL` on empty
→ no rows (`:35-36`). Writes are permissive (`WITH CHECK (true)`,
`:65-72`) — by design, the accessor + AST chokepoint govern writes; the
backstop's whole job is the read side, "the actual blast radius"
(`0008_friends_rls_backstop.py:19-20`). All three carry `FORCE ROW LEVEL
SECURITY` (`:97,114,125`), which matters if the connecting role also owns
the tables (owners bypass their own non-`FORCE`d RLS).

**The `service_role` clause is an unconditional bypass, not a narrowing.**
`_GUC_SERVICE = "coalesce(current_setting('app.service_role', true), '') = 'true'"`
(`:44`) is OR'd into every one of the three policies above. Any connection
with `app.service_role='true'` sees **every row on every tenant**,
unfiltered — by design, for legitimate background work (`backstop_service_context`,
`access.py:61-86`). Per §2's table, that GUC is set on **every** internal-key
runtime request, not just genuine background jobs — so the friends
agent-facing runtime endpoints (`neighborhood/context/`, `missions/`,
`lessons/<id>/propose-share/`, §3c of `api-surface.md`) get **zero**
protection from this backstop regardless of whether it binds for anyone
else. `apps/friends/access.py`'s Python filters are the only thing standing
between tenants on that surface, full stop — this is independently confirmed
by [`authn-authz-and-api-surface.md §7`](authn-authz-and-api-surface.md).

---

## 7. Public-schema lockdown — closing the Supabase Data API

Distinct problem, distinct fix, easy to conflate with §5–6. On 2026-05-14 the
Supabase anon API key was found able to read `users.email` and Django
password hashes via PostgREST (`docs/tenants/migrations/0058…` /
`memory/project_supabase_public_schema_exposure.md`, cited in
`apps/tenants/test_public_schema_lockdown.py:1-11`) — pre-existing
`tenant_isolation_*`/`service_bypass` policies targeted the Postgres `public`
pseudo-role, which **includes** `anon`.

`apps/tenants/migrations/0059_lock_down_public_schema_rls.py` fixes this with
three operations (`:26-97`): (1) drop every policy owned by the migrating
role on `public.*`, (2) `ENABLE ROW LEVEL SECURITY` on every owned table, and
— the part that actually matters — (3) `REVOKE ALL PRIVILEGES … FROM
anon`/`authenticated` on all tables/sequences/functions **and**
`ALTER DEFAULT PRIVILEGES` so future tables inherit zero grants (`:56-73`).

**This is why `disable_rls` flipping RLS back off every boot doesn't reopen
the hole.** `disable_rls.py`'s SQL is exactly one statement —
`ALTER TABLE … DISABLE ROW LEVEL SECURITY` — it never touches `GRANT`/`REVOKE`.
The anon/authenticated roles have **zero table privileges**, independent of
RLS state; RLS-disabled-but-zero-grants is exactly as closed to PostgREST as
RLS-enabled-with-zero-grants. The load-bearing control for the Data API is
the grant revocation, not the RLS toggle — worth stating plainly since
`docs/rls-tenant-isolation.md` frames RLS as *the* tenant-isolation mechanism
throughout and doesn't mention grants at all.

**Topo-ordering foot-gun (already flagged in `data-model.md`, not
re-derived here).** New tables need their owning app's migration added to a
`relock_after_*` migration's `dependencies` — 18 such migrations exist
(`0066` through `0106`), one per feature that added tables since the
lockdown; a forgotten dependency silently escapes both the RLS-enable and the
grant-revoke. See [`data-model.md` → Risks](../reference/data-model.md#risks--improvement-opportunities)
for the full writeup.

**CI enforcement, and its blind spot.** `apps/tenants/test_public_schema_lockdown.py`
has two guard classes: a static-analysis pass over every migration file
(`PolicyToPublicRole`, `GrantToApiRole`, `DisableRlsStatement` regexes,
`:66-115`) and a runtime pass asserting the **test database's post-migrate**
state has RLS enabled on every owned table and no unsanctioned policies
(`:127-174`). Neither `Makefile` (`test`/`ci-test` targets) nor
`.github/workflows/ci-cd.yml` ever invokes `manage.py disable_rls` around the
test run — confirmed by grep. So `test_rls_enabled_on_owned_public_tables`
(`:154`) is asserting a state (RLS enabled fleet-wide) that is **true only in
CI and in the brief window between `migrate` and `disable_rls` in
production**, not the steady-state production posture described in §1/§5. An
auditor should not read this test's green status as "RLS is enforced in
prod" — it's asserting the anon-lockdown precondition, which is a different
claim. **[low][open]** — a comment in the test file noting this explicitly
would prevent the same misreading a human auditor could make.

---

## 8. `service_role` usage — who gets the blanket bypass

| Caller class | Sets `service_role`? | Tenant GUC also set? |
|---|---|---|
| JWT/PAT (console, iOS/web user) | No | Yes (`app.tenant_id`+`app.user_id`) |
| Internal-key (OpenClaw container → Django, ~150 routes) | **Yes**, unconditionally, on every successful auth | Yes (from URL) |
| Webhooks (Stripe/Telegram/LINE) | Yes | No |
| Cron trigger / ops views (QStash-sig or deploy-secret) | Yes | No |
| `backstop_service_context()` (`access.py:61-86`) — scrub jobs, envelope push, friend-chat push threads | Yes, scoped to the `with` block only; clears only `service_role` on exit, leaves any in-request tenant GUC untouched | n/a |

Net: `service_role=True` is not a narrow "trusted cron" flag — it's set on
the *majority* of non-browser traffic, including every single agent tool
call from every tenant's OpenClaw container. Its only real gate, for the
three FORCE-RLS tables, is that a party check happens somewhere upstream in
Python (the accessor) rather than in the database — same posture as the 162
non-friends tables, just with an extra, mostly-inert layer of policy SQL
underneath for the console path.

---

## 9. Reconciling the doc-vs-reality gap on the runtime DB role

This is the single most consequential open question in the isolation model,
and the evidence is split between two sources that should agree and don't.

**What the friends module's own comments assert:** Django connects as a
**BYPASSRLS superuser** today, making the FORCE-RLS backstop (§6) inert
belt-and-suspenders. Stated in four places, verbatim or near-verbatim:
`access.py:12-19`, `test_access_chokepoint.py:1-9`,
`friends/migrations/0008_friends_rls_backstop.py:5-9`, and
`friends/management/commands/check_friends_rls.py:1-9` (whose own purpose is
to print the live verdict — i.e., even this file's authors were treating
BYPASSRLS as the working assumption, not a confirmed fact).

**What everything else says:** `docs/rls-tenant-isolation.md` describes
`app_user` as the runtime role with RLS *enforced*. `docs/agents/architecture.md:26`
states "Django connects via transaction-mode pooler as role `app_user`
(non-BYPASSRLS; RLS backstop is load-bearing)." `docs/GLOSSARY.md:52-53`
defines `app_user` as "**Non-BYPASSRLS**, so RLS predicates are enforced" and
calls RLS "the load-bearing tenant-isolation backstop." `config/settings/base.py:102`
resolves the runtime connection from `env.db("DATABASE_URL", …)` — the
restricted connection string per `docs/rls-tenant-isolation.md`'s two-connection-string
design, distinct from `ADMIN_DATABASE_URL` (used only for the `migrate` step
in `startup.sh:5`). And — the fact that actually forced this reconciliation
— the orchestrator's live `pg_roles` query against prod confirms `app_user`
is **not** `BYPASSRLS`, while `postgres` (which owns all 165 tables) and
`service_role` are.

**Chaining these together**, using `check_friends_rls.py`'s own documented
verdict logic (`:104-127`): a connection that is (a) non-superuser,
non-BYPASSRLS, and (b) **not the table owner** (`postgres` owns everything,
confirmed) lands in that command's own "BINDS — … every friends read path
must carry `app.tenant_id` or `app.service_role`, or reads fail closed"
branch (`:122-127`) — not the "INERT" branch. If the runtime process really
does connect via `DATABASE_URL`/`app_user` as every non-friends-module doc
claims, **the FORCE-RLS backstop on the three friends tables is likely
already binding for the JWT/PAT console surface today**, contrary to what
every comment inside `apps/friends/` says. This conclusion is independently
reached by [`authn-authz-and-api-surface.md §7`](authn-authz-and-api-surface.md),
which states flatly that RLS "is a real second line of defense... unlike the
internal-key surface" — written by a separate audit pass with no access to
this one's reasoning.

**What would fully close this out:** actually running
`manage.py check_friends_rls` against prod — it prints `current_user`,
`rolbypassrls`, table ownership, and the live policy list in one shot
(`check_friends_rls.py:40-128`). That is explicitly **not** something this
audit ran (per the task brief); it's the one remaining action item. Two
things are true regardless of that command's answer: (1) §6's conclusion
that the backstop provides **zero** protection on the internal-key/agent
surface holds either way, since `service_role=True` bypasses it
unconditionally by design; (2) the `apps/friends/` module's source comments
should be corrected either to remove the stale BYPASSRLS assumption (if the
command confirms non-bypassing) or to explain why production differs from
every other doc's stated role (if it confirms BYPASSRLS) — right now a new
engineer reading `access.py` and a new engineer reading `architecture.md`
walk away with opposite mental models of the same system. **[high][open]**

---

## 10. Cross-tenant leak vectors and their defenses

| # | Vector | Defense today | Residual gap |
|---|---|---|---|
| V1 | ORM query on a non-friends table forgets its `tenant=` filter | None at the DB layer (RLS off on 162/165 tables, §1/§5) — pure code-review discipline; the pattern in §3 is consistent where sampled | **No CI net.** A missing filter ships and leaks silently; nothing fails a build. Highest-leverage fix would be a structural guard (custom queryset/manager requiring explicit tenant scope, or a linter rule) — none exists today. |
| V2 | Internal-key/runtime handler trusts a client-supplied id in the body/query instead of deriving tenant from the validated URL+header | `_internal_auth_or_401` binds `tenant` from the **URL**, and the sampled pillar views (§3) filter children through that tenant-derived object | Enforcement is per-handler convention across ~150 routes in 8+ modules (cataloged in `authn-authz-and-api-surface.md §3a`, which found 100% current compliance but no structural guard against regression) |
| V3 | A friends cross-tenant model (`SharedLesson`/`FriendMessage`/`SharedGoal`/`LessonShareGrant`) queried outside `access.py` | AST CI chokepoint (`test_access_chokepoint.py`), scoped to `apps/friends/**` + 2 named runtime-view files | Aliased-import evasion is a known, undetected gap in the AST matcher (`test_access_chokepoint.py:32-35`); any **other** app importing these models directly is invisible to the test (none found in this checkout — verified by repo-wide grep) |
| V4 | A `Friendship`/`NeighborProfile` cross-tenant-shaped (but not chokepoint-guarded) lookup is hand-rolled outside `apps/friends/` without re-verifying the caller is a party | None structural — `apps/router/friends_callbacks.py:75,113` does this correctly by convention (§4b), but nothing would catch a *future* instance done incorrectly | No AST/CI coverage for `Friendship`/`NeighborProfile` at all — only the 4 chokepoint-guarded models are enforced |
| V5 | A new table lands without RLS-enable + anon/authenticated grant revocation | `relock_after_*` migration chain + `test_public_schema_lockdown.py` static + runtime guards | Topo-ordering foot-gun — a forgotten `relock` dependency escapes both checks (`data-model.md` risk, not re-derived here); see §7 |
| V6 | Stale RLS GUC survives on a reused pooled connection (`CONN_MAX_AGE=600`) into a request that never calls `set_rls_context()` itself | `reset_rls_context()` runs in every `process_response`; the very next authenticated request overwrites tenant/user GUCs before querying | The `except Exception: pass` around the reset (`middleware.py:99-101`) plus the doc's inaccurate `CONN_MAX_AGE=0` claim (§2) mean this path is unverified by any test; low practical severity today because RLS is off almost everywhere it would matter (V1's territory), but becomes a live concern if the FORCE-RLS pattern is ever extended per `data-model.md`'s own recommendation |
| V7 | `service_role=True` (set on ~all non-browser traffic, §8) reaches a code path that queries a friends table without an accessor-equivalent tenant check | N/A today — no such path exists; every friends query, chokepoint-guarded or not, was traced to a re-verification (§4a) or a correct hand-rolled check (§4b) | Structural: nothing prevents a *future* handler in `apps/cron/`, `apps/router/`, etc. from importing a chokepoint-guarded model and querying it under an ambient `service_role=True` connection with no tenant check — same shape as V3/V4 but for code that hasn't been written yet |

---

## Findings

- **[high][open]** The runtime Postgres role's BYPASSRLS status is asserted
  two different ways by two different parts of this repo, and the verified
  live-DB fact (`app_user` non-BYPASSRLS, `postgres` owns all 165 tables)
  sides with the *architecture/glossary* claim, not the *friends-module
  comment* claim — meaning the FORCE-RLS backstop on `shared_lessons`/
  `lesson_share_grants`/`friend_messages` (§6) is plausibly **binding today**
  for the JWT/PAT console surface, contrary to what every comment inside
  `apps/friends/` states. `manage.py check_friends_rls` against prod is the
  one command that settles it; not run in this audit. See §9.

- **[high][open]** Non-friends tenant isolation (162/165 tables) has no
  database-layer net at all: `disable_rls` runs on every boot and turns RLS
  off everywhere except the three friends tables (§5). A single ORM query
  missing its `tenant=` filter leaks cross-tenant data silently, with no CI
  guard to catch it (V1). This is the same conclusion `data-model.md`
  already reaches from the data-catalog side; restated here because it's the
  load-bearing fact behind every other finding in this document.

- **[high][open], but scope-limited by design** The FORCE-RLS friends
  backstop — whichever way §9 resolves — provides **zero** protection on the
  internal-key/agent-runtime surface (~150 routes, including the
  Neighborhood agent-facing endpoints), because every such handler sets
  `app.service_role='true'`, an unconditional OR-bypass in all three
  policies (§6, §8). Isolation there is 100% `apps/friends/access.py`'s
  Python filters, which this audit traced call-by-call and found correctly
  re-verified except one gap (next finding). Independently confirmed by
  `authn-authz-and-api-surface.md §7`.

- **[med][open]** `report_content` (`apps/friends/circles.py:267`) resolves
  a `shared_lesson` report target via `access.get_shared_lesson(target_id)`
  with **no** visibility check — any authenticated tenant can attach a
  moderation report to a `SharedLesson` id belonging to a neighbor they have
  no grant relationship with. Doesn't leak content (the response never
  echoes it, just `{report_id, hidden}`), but is an existence oracle and
  breaks the "re-verify caller is a party" pattern every other raw-id lookup
  in the module follows (§4a). Add a `shared_star_qs`-style visibility check
  before accepting the report.

- **[med][open]** `apps/tenants/migrations/0059_lock_down_public_schema_rls.py`'s
  docstring states `disable_rls` and its `startup.sh` invocation "are
  removed in the same change" — false in this checkout (`startup.sh:8` calls
  it; `disable_rls.py` was actively edited after 0059 for the PR8 friends
  exception). An auditor reading only 0059 draws the wrong conclusion about
  production RLS state. Correct the docstring. See §5.

- **[med][open]** `docs/rls-tenant-isolation.md:50-51` claims
  `CONN_MAX_AGE=0` as a per-request connection-close safety net; production
  actually sets `CONN_MAX_AGE=600` (`config/settings/production.py:45`),
  intentionally, for pooler-connection-cost reasons unrelated to RLS. The
  safety-net claim is simply wrong and should be removed or corrected; the
  actual mitigation for stale-GUC-on-reused-connection is the
  every-request `set_rls_context()` call overwriting prior state, which is
  real but different from what the doc describes. See §2, V6.

- **[low][open]** `access.py`'s docstring overclaims: "Nothing else
  hand-rolls a cross-tenant query." `apps/router/friends_callbacks.py`
  queries `Friendship`/`NeighborProfile` directly (correctly re-verified,
  not currently exploitable) outside both the accessor and the AST
  chokepoint's scope, since neither model is chokepoint-guarded. No CI net
  exists for a future, less-careful instance of the same pattern. See §4b, V4.

- **[low][open]** `test_public_schema_lockdown.py`'s runtime guard asserts
  "RLS enabled on every owned public table" against the **test** database,
  which never runs `disable_rls` (confirmed absent from `Makefile` and
  `ci-cd.yml`). Its green status certifies the anon-lockdown precondition,
  not production's actual steady-state RLS posture (which is RLS-off on 162
  tables). Risk of auditor/engineer misreading; a one-line comment in the
  test would prevent it. See §7.

- **[low][by-design]** The AST chokepoint's own documented blind spot — an
  aliased import (`from .models import SharedLesson as X`) would evade
  detection (`test_access_chokepoint.py:32-35`). Not exploited in this
  checkout; noted for completeness since it bears on how much weight the
  "CI-enforced" claim in `data-model.md` and elsewhere should carry.

- **[low][by-design]** `apps/friends/migrations/0008_friends_rls_backstop.py`'s
  non-recursive policy composition (`shared_lessons` delegates reach-checking
  to `lesson_share_grants`'s own RLS rather than re-deriving it) was verified
  correct under standard Postgres RLS subquery semantics — the `EXISTS`
  subquery against `lesson_share_grants` is itself subject to that table's
  RLS policy for the querying role, so the composition doesn't accidentally
  widen visibility. No finding; noted because it's the kind of pattern that
  looks like a bug on first read.
