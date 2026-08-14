# Handback: Datebook request-create latency

## Outcome

- Branch anchor: `fix/datebook-create-latency` at merge `dc38a5b0` (PR #1454).
- Production was not accessed. All diagnosis and reproduction used repository history, Django's test client, and instrumented mocks.
- The protected concurrent-work files `apps/datebook/envelope.py` and `runtime/openclaw/plugins/nbhd-automation-tools/*` were not changed.

## Measured root cause

PR #1454 added this create-path sequence:

```text
POST request-create
  -> request_datebook_action()
  -> transaction.atomic() commits
  -> transaction.on_commit(_schedule_gate_changed callback)
  -> notify_datebook_gate_changed()
  -> _push_to_user_devices()
  -> APNs
  -> HTTP response
```

Django runs `on_commit` callbacks synchronously in the committing thread. The new callback therefore held the request open while APNs ran. APNs created a fresh HTTP/2 client for each environment, used a 10-second timeout, and sent device tokens serially. One invalidation can consequently accumulate several transport waits. That path is consistent with the observed 54,257 ms response when APNs or matching stale device tokens are unhealthy.

The merged PR does **not** send two APNs notifications during app-originated create. It moved the prior app-confirmation wake to the new gate-change invalidation: the refactored app confirmation sender only marks the durable app review surface available, while the new `datebook_gate_changed` callback performs the blocking APNs send. Approval can schedule both the device-command and gate-change notifications. No request-create QStash publish and no new sender retry loop were found.

Instrumented local mock measurements:

- Synchronous control with a 500 ms fake push: **0.514784 s** request time.
- Fixed dispatch with the same fake push still pending: **0.014423 s** request time.
- Permanent regression test lets the fake transport hang for up to 2 seconds and requires the real `POST` to return `202` in **< 0.2 s**.

The local test intentionally scales the wait down; reproducing the exact external 54.257-second duration would violate the mocks-only/no-production constraint. It reproduces the causal request-boundary behavior directly.

## What changed

### Off-request-path notifications

- `datebook_gate_changed` dispatch now starts a fail-soft daemon thread from the `on_commit` callback.
- Approved `DeviceCommand` notification uses the same off-path dispatcher.
- Background workers receive immutable IDs, establish/query their own Django connection context, and close old connections before and after work.
- `NBHD_DISABLE_BACKGROUND_THREADS=True` preserves the repository's synchronous test seam.
- APNs now has split short bounds: 2 s connect, 3 s read/write, and 1 s pool wait. These limit background resource occupancy; request latency no longer depends on them.

### Honest plugin timeout and stable retry identity

- An aborted runtime request is tagged as `runtime_timeout`.
- Create converts it into a thrown, model-facing `request_still_processing` error: “The Calendar & Reminders request is still being processed. DO NOT re-call this tool; the approval will appear shortly.” It is no longer returned as an `ok`-shaped tool result.
- The plugin caches a stable request ID for two minutes using a SHA-256 key over tenant, session, origin, command type, and canonicalized input. An exact retry with a new tool-call ID therefore reuses the original request ID whenever the runtime exposes the same session/logical input.

### Duplicate-gate guard verdict

Implemented. It composes cleanly with existing request-ID idempotency:

1. Existing `(tenant, request_id)` replay remains the first check.
2. Under the existing tenant row lock, a different request ID is rejected only when a pending gate from the last two minutes has the exact logical signature.
3. The signature is command type, sorted normalized titles, and UTC target minute. Title normalization is Unicode NFKC + case-folding + whitespace collapse.
4. The response is typed `409` state `duplicate_request`, includes `existing_action_id`, and says not to create another gate.

Different target minutes, expired/non-pending gates, and asks outside the two-minute window remain allowed. The tenant lock serializes concurrent creates, so a retry that arrives while the first request is still committing cannot create a race duplicate.

## First-call parameter omission verdict

No plugin-side schema regression exists. The original static create tools advertised `required: ["items", "direct_user_originated"]`; the per-turn factory change in `0114b50d0` retained that required array. The pinned OpenClaw Node 22 E2E captures the schema actually advertised by the runtime and passes while asserting both fields. The three first-call omissions are therefore recorded as model behavior; no schema workaround was added.

## Verification

- Focused datebook plugin error/origin tests: **5/5 PASS**.
- Focused latency, approval UX, and APNs Python tests: **31/31 PASS** in 1.534 s.
- Broader datebook/orchestrator schema/APNs Python tests: **109/109 PASS** in 5.212 s.
- Shared OpenClaw plugin error-transport tests: **133/133 PASS**.
- Datebook plugin directory tests: **5/5 PASS**.
- Django OpenClaw plugin bridge tests: **14/14 PASS**.
- Pinned OpenClaw Node 22 schema E2E: **1/1 PASS**.
- `ruff check`, `ruff format --check`, `git diff --check`, and migration drift check: **PASS**.
- Full `make docker-gate`: **PASS**.
  - Backend: Ruff, format, secret scan, migration drift, migrations, Django checks, **7,843 tests PASS** in 679.688 s (**2 skipped**), config validator, and security audit.
  - Frontend: install, ESLint (**0 errors; 4 existing warnings**), TypeScript/static build, and 43 generated pages.

## Gate tail

```text
Ran 7843 tests in 679.688s
OK (skipped=2)
Config validator: PASS
Security audit: PASS
=== BACKEND LEG: PASS ===
=== FRONTEND LEG: PASS ===
=== DOCKER CI-PARITY GATE: PASS ===
```
