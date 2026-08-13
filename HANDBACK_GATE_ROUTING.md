# Datebook Gate Routing Handback

Date: 2026-08-12
Branch: `fix/datebook-gate-channel-routing`
Starting head: `d7d6e65cbf2f03108a36f8126de084618af20d49`

## Outcome

No stop-on-contradiction condition was found. The shipped backend contains the
consumer datebook gate surface, generic app notification path, and shared
row-locked approval resolver described in the directive. The defect was an
origin-attribution plumbing gap, and it is fixed without an iOS change or a
migration.

Datebook create gates now select their delivery surface from the originating
request turn:

- iOS/app -> app review sheet plus the existing generic `datebook_gate` APNs
  wake; never Telegram or LINE.
- Telegram -> the existing Telegram sender and existing button payloads.
- LINE -> the existing LINE sender and existing button payloads.
- Missing origin -> the pre-existing Telegram-then-LINE-then-app compatibility
  fallback remains in place for legacy/background callers.
- Explicit app origin with no reachable push token -> remains pending and
  discoverable through the review-sheet endpoint; there is no cross-channel
  fallback.

## Investigation findings and root cause

### 1. Request turn -> gate -> notification

The router already knows the originating channel. Its per-turn marker names
`ios`/app, Telegram, or LINE and is documented as authoritative at
`apps/router/services.py:53-64` and `apps/router/services.py:73-95`. The three
main runtime POST paths are at `apps/router/pending_queue.py:2474-2482`,
`apps/router/pending_queue.py:2599-2611`, and
`apps/router/pending_queue.py:2732-2739`; the app PDF follow-up path is at
`apps/router/tasks.py:127-137`.

Before this fix those POSTs supplied `X-Channel`, but pinned OpenClaw did not
use that header to create tool context. The fleet pin is
`Dockerfile.openclaw:9,39-40`. I inspected the official
`openclaw@2026.5.28` npm tarball (read-only, outside the repo): its OpenAI HTTP
handler enables message-channel header parsing, the resolver reads only
`x-openclaw-message-channel`, and plugin tool factories receive trusted
`toolContext.messageChannel`. Thus the channel was available at Django ingress
but not at datebook gate creation.

The datebook plugin then compounded the gap: its create request did not include
any channel. The fixed seam is now visible at
`runtime/openclaw/plugins/nbhd-datebook-tools/index.js:122-127` (normalization),
`:209-225` (runtime request body), and `:360-398` / `:400-438` (per-turn tool
factories).

The internal runtime view formerly accepted no origin. It now validates and
normalizes the trusted value at `apps/datebook/runtime_views.py:241-265` and
passes it to the gate at `apps/datebook/runtime_views.py:298-306`. Gate creation
creates exactly one idempotent `PendingAction` and reserves a command UUID at
`apps/datebook/gate.py:141-193`, then forwards the origin to delivery at
`apps/datebook/gate.py:195-201`.

The direct production symptom is explained by the old final selector. With no
origin, the compatibility branch chooses any linked Telegram account first,
then LINE, then an app installation (`apps/actions/messaging.py:426-448`). MJ's
tenant has Telegram linked, so the pending rows were stamped Telegram. The new
explicit-origin branch is authoritative at
`apps/actions/messaging.py:391-424`; `send_gate_confirmation` consumes it at
`apps/actions/messaging.py:451-505`.

Root cause: the router had turn attribution, but used a header OpenClaw did not
recognize; the datebook tools were static definitions that did not receive the
trusted per-turn tool context; and the internal create API/gate accepted no
origin. Delivery therefore always entered the legacy linked-account fallback,
which correctly but undesirably selected Telegram for this tenant.

### 2. Existing B2c app-channel pieces

The consumer endpoints already exist at `apps/datebook/urls.py:21-25`:

- `PendingGateActionsView` tenant-scopes and lists the oldest unexpired pending
  datebook actions at `apps/datebook/views.py:166-186` (the iOS sheet's polling
  surface).
- `RespondGateActionView` checks ownership/type and delegates to the shared
  resolver at `apps/datebook/views.py:189-214`.
- `GateRespondView.resolve_action` performs the one `transaction.atomic` +
  `select_for_update` transition for all channels at
  `apps/actions/views.py:300-356`. No second approval pipeline was added.

App delivery atomically claims the existing tracking fields as
`platform_channel='app'` plus a synthetic idempotency message id, then attempts
one generic, installation-targeted APNs wake at
`apps/actions/messaging.py:319-369`. The payload is only
`{"type":"datebook_gate","action_id":...}`; review text and approval actions
are absent. Push exceptions are non-fatal (`apps/actions/messaging.py:370-377`),
so the already-claimed pending row remains visible to the sheet on next open.
There is no explicit undeliverable branch that crosses an app-originated gate
to Telegram/LINE, so none was invented.

`DeviceCommand.notified_at` is a separate, post-approval notification claim
(`apps/datebook/models.py:510-514`, `apps/datebook/notify.py:16-43`) and sends
`datebook_command`, not `datebook_gate`.

### 3. Why one ask produced three pending rows

Verdict: three agent tool executions, not server-side fan-out.

Each create tool execution makes one internal `request-create` POST, using its
tool-call id as `request_id` (`runtime/openclaw/plugins/nbhd-datebook-tools/index.js:209-224`).
The server checks that request id before and inside a tenant row lock and creates
at most one pending row (`apps/datebook/gate.py:156-193`). The database also
enforces uniqueness on `(tenant, datebook_request_id)` at
`apps/actions/models.py:95-100`. One action can carry 1-5 requested items, and
the eventual command enforces the same item cap at
`apps/datebook/models.py:476-524`.

Therefore three distinct pending rows/request ids in two minutes require three
distinct tool executions (for example repeated model calls or repeated turns),
not a server fan-out of one request. Per instruction, no secondary behavior was
changed.

### 4. `datebook_device_commands` and RLS

There are two independent reasons an admin-role SELECT can return zero rows:

1. A pending gate's `datebook_command_id` is a reserved UUID, not proof that a
   `datebook_device_commands` row exists. The UUID is assigned while the
   `PendingAction` is created (`apps/datebook/gate.py:160-188`). The actual
   `DeviceCommand` is created only after approval at
   `apps/datebook/gate.py:70-115`. All three observed rows were still pending
   with `responded_at=NULL`, so their reserved ids should not yet resolve to
   command rows even for a bypass role.
2. The public-schema lockdown drops all public-table policies and states that
   only owner/BYPASSRLS roles can read without policies
   (`apps/tenants/migrations/0059_lock_down_public_schema_rls.py:9-24,29-54`).
   Datebook tables are created later, then the relock enables RLS across the
   newly created public tables without adding a policy
   (`apps/tenants/migrations/0153_relock_after_datebook_b1.py:5-20,27-35`). A
   role merely named/used as "admin" but lacking ownership or BYPASSRLS gets
   PostgreSQL's default-deny result. True `postgres`, `service_role`, or
   `supabase_admin` bypass roles can read rows, but reason 1 still applies.

### 5. The observed 18.9-second tool latency

The code provides a credible explanation. Telegram confirmation is sent
synchronously inside the runtime `request-create` path. The primary Telegram
POST has a 10-second timeout and a non-200 response triggers a second plain-text
POST with another 10-second timeout (`apps/actions/messaging.py:67-101`). After
the create response, the plugin may also poll approval status for up to 10
seconds within an overall 20-second request budget
(`runtime/openclaw/plugins/nbhd-datebook-tools/index.js:7-8,192-206`). A measured
18.9 seconds is therefore consistent with synchronous Telegram network time
plus bounded approval polling. The exact split is UNCONFIRMED because production
spans/hosts were deliberately not accessed.

## Implementation

- Added the OpenClaw-recognized `X-OpenClaw-Message-Channel` header to existing
  iOS, Telegram, and LINE runtime turns while retaining `X-Channel` for
  compatibility (`apps/router/pending_queue.py:2474-2480,2599-2605,2732-2737`;
  `apps/router/tasks.py:127-135`).
- Converted only the two datebook create tools to per-turn factories, normalized
  `ios` to backend `app`, and forwarded `originating_channel`; reads remain
  unchanged (`runtime/openclaw/plugins/nbhd-datebook-tools/index.js:122-127,209-225,360-438`).
- Added strict internal API validation/normalization and threaded origin through
  gate creation (`apps/datebook/runtime_views.py:241-265,298-306`;
  `apps/datebook/gate.py:141-150,195-201`).
- Made explicit origin authoritative in the existing dispatcher. The no-origin
  compatibility order and Telegram/LINE message bodies/buttons are unchanged
  (`apps/actions/messaging.py:391-448,451-505`).
- Added regressions for runtime normalization/rejection, app/Telegram/LINE
  selection, zero Telegram/LINE calls for app origin, no-reachable-push
  discoverability, all router header paths, and plugin tool-context propagation
  (`apps/datebook/test_b2a.py:212-245`, `apps/datebook/test_b2c.py:265-330`,
  `runtime/openclaw/plugins/nbhd-datebook-tools/originating-channel.test.js:28-63`).

## Verification

Before implementation:

- Focused Django gate/datebook/actions suites: 57 tests, PASS.
- Datebook plugin Node suite: 2 tests, PASS.

After implementation:

- Same focused Django suites: 60 tests, PASS (+3).
- Datebook plugin Node suite: 3 tests, PASS (+1).
- Touched router suites: 216 tests, PASS.
- Ruff format on all touched Python files: unchanged/clean.
- Ruff check on all touched Python files: PASS.
- `git diff --check`: PASS.
- Full required `make docker-gate`: PASS.
  - Backend: Python 3.12 + pgvector/PostgreSQL 16; migration drift, Ruff,
    formatting, secret scan, migrations, system checks, 7,816 tests (2 skipped),
    config validator, and security audit all passed.
  - Frontend: Node 22 install, ESLint (0 errors; 4 existing warnings), and Next.js
    production build passed.

Full-gate output tail:

```text
○  (Static)  prerendered as static content
●  (SSG)     prerendered as static HTML (uses generateStaticParams)

=== FRONTEND LEG: PASS ===

=== DOCKER CI-PARITY GATE: PASS ===
```

## Deviations / caveats

- No requested behavior was omitted. No migration, iOS change, version bump,
  production access, real APNs/Telegram/LINE send, push, PR, or deploy occurred.
- Exact attribution of the 18.9-second production wall time is intentionally
  reported as plausible rather than proven because mocks-only/no-production
  constraints preclude the missing timing spans.
- The full gate reported npm audit metadata (5 dependency vulnerabilities) and
  four pre-existing frontend lint warnings; neither failed the repository gate
  and neither is related to this backend routing fix.
