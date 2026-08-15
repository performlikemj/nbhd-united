# Handback: request-create hang

## Outcome

`request-create` no longer performs neural PII detection on its synchronous gate-creation path, and every PostgreSQL statement/row-lock wait in that path now has a local database budget. A budget failure returns a typed, retriable HTTP 503 that states nothing was created. The OpenClaw plugin no longer promises that an approval will appear after an unconfirmed 20-second timeout.

## Reproduced root cause

The blocking call was recursive NER authoring of `PendingAction.action_payload` while the tenant row lock was held.

- The prior latency regression test used a tenant with `layer1_placeholder_writes=False`, so it made **0** detector calls.
- With `layer1_placeholder_writes=True`, the production-shaped reminder containing zoned `due` plus absolute `alarm` made **13** calls through `apps.pii.redactor._detect_pii` and **13** through `apps.pii.authoring._detect_pii`: **26 neural detector passes** for one gate.
- Replacing the first detector with a blocking mock stopped the exact Django-client `request-create` call until the mock was released. This models the cold model/NER hop that was absent from the old timer test.
- The due-plus-alarm shape was valid. It increased recursive string-leaf fanout; its timezone parsing was finite and was not the hang.

The tenant's recent OpenClaw restart was therefore coincidental except that it made a cold first request more likely to encounter the expensive detector path.

## What was ruled out

- Duplicate detection is a capped scan of 20 indexed `PendingAction` candidates; it did not account for the stall.
- Destination-default resolution is ordinary bounded database work.
- `request-create` does not schedule a QStash sweep on this path.
- The hourly config apply does not retain the tenant row lock across its external config upload.
- Due/alarm validation and `parse_datetime` contain no retry loop or swallowed exception loop.
- PII owner rehydration is an in-memory placeholder map operation, not a network/decryption hop.

## Fix

### Remove the blocking cause

Datebook gate authoring now requests `defer_detection=True` for the runtime writer:

- Active known values are still replaced deterministically before persistence (for example, `Alice` becomes `[PERSON_1]`).
- Neural redaction, residual detection, and live Redis outcome telemetry do not run on request-create.
- The receipt is stamped `unconfirmed / detector-deferred`, which is already an eligible state for the bounded PII repair sweep.
- The mode is rejected for owner/background writers, limiting the escape hatch to runtime-authored durable work.

Runtime writers already use `MINT_NEVER`; neural detection could classify unknown residuals but could not mint new placeholders. Deferral changes when that classification occurs, while preserving the same deterministic known-value masking on the write.

### Bounded worst case

`datebook_create_db_budget()` wraps request-create database work with PostgreSQL transaction-local limits:

- `lock_timeout = 2000ms`
- `statement_timeout = 5000ms`

The budget covers internal auth/tenant lookup, runtime-write activity, idempotency lookup, the tenant `select_for_update`, duplicate/default queries, gate insertion, transport state saves, and undeliverable expiry. An `OperationalError` becomes:

```json
{
  "state": "request_temporarily_unavailable",
  "retriable": true,
  "created": false,
  "guidance": "Nothing was created yet. Calendar & Reminders is temporarily busy; retry this request later."
}
```

For app-originated requests, `platform_channel=app` and `delivery_state=available` are now persisted in the bounded insert, removing the formerly separate generic app-surface save.

The real-PostgreSQL contention regression uses a second connection/thread to hold the tenant row lock. With a 100ms test lock budget, request-create returned the exact typed 503 in under 1 second and created no `PendingAction`.

## Plugin narration

- A client-side `request_still_processing` timeout now says **nothing was created in Apple Calendar or Reminders yet** and that the server did not confirm whether a gate was recorded.
- It explicitly tells the assistant not to re-call automatically and not to promise that an approval will appear shortly.
- The typed server 503 is rendered as structured retriable guidance with `created: false`; other runtime HTTP failures retain the repository's canonical error-transport construction.

## Regression coverage

- Exact zoned-due plus absolute-alarm request with Layer 1 enabled and both detector entry points replaced by blocking mocks: response `202` in `<0.2s`, **0 detector calls**, known name placeholdered, repair receipt present.
- Real PostgreSQL tenant-row contention: typed `503` in `<1s`, no action created.
- Deferred-authoring unit test: known-value masking, repair eligibility, and no neural/cache calls.
- App-surface test: delivery state persisted in the bounded insert and no second sender save.
- Plugin tests: honest timeout language, stable logical request ID, and typed bounded-server failure.

## Verification

- Focused backend: **41/41 passed**.
- Datebook plugin Node suite: **7/7 passed**.
- Broader `apps.datebook apps.actions apps.pii`: **617/617 passed**.
- Canonical plugin throw drift guard: **1/1 passed** after preserving the shared transport contract.
- Full `make docker-gate`: **PASS**.
  - Ruff lint: pass.
  - Ruff format: **1,469 files already formatted**.
  - Secret scan: pass.
  - Migration drift and Django system check: pass.
  - Backend: **7,854/7,854 passed in 571.763s; 2 skipped**.
  - Config validator: pass.
  - Security audit: pass.
  - Frontend lint: **0 errors, 4 pre-existing warnings**.
  - Frontend production build/static export: pass, **43/43 pages generated**.

Gate tail:

```text
=== BACKEND LEG: PASS ===
=== FRONTEND LEG: PASS ===
=== DOCKER CI-PARITY GATE: PASS ===
```
