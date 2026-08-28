# Real product E2E harness

Read this before running the dedicated synthetic tenant through production chat or PII flows. The harness is intentionally narrower than the public API: `scripts/e2e/nbhd_e2e.py` selects one of two fixed HTTPS production hosts, a checked-in tenant/account pair, fixed fixture content, and a disposable non-main thread. The installed skill can invoke only `scripts/e2e/nbhd_e2e_skill.py`, a runtime command allowlist that does not expose global host flags.

## Privacy ladder

Use the least content-bearing surface that can prove the behavior:

1. Prefer closed status/source/error enums, normalized ISO timestamps, counts, and boolean presence fields. Unknown server strings become `other`; unexpected types fail closed.
2. Inspect `reply_text` only in memory to prove it is non-empty, then discard it. Never print it.
3. Inspect redaction mappings only in memory. The smoke may compare mapping values with the two checked-in literals, `Evelyn Testwell` and `+1 202-555-0147`; it never logs them.
4. `pii count` retrieves the content-bearing entity registry because no count-only endpoint exists, discards every entry, and prints only the number.
5. Raw logs and tenant conversation content are outside this harness. `logs` is `UNAVAILABLE` until the tenant-log-digest lane is merged, deployed, and its Azure RBAC is verified.

Every test message starts with `[NBHD E2E SYNTHETIC]`. Output is metadata-only. Server cursors, arbitrary message IDs, raw errors/reasons, and non-fixture placeholders are never printed. Tokens stay in memory or macOS Keychain.

## What may be automated

- Password login, JWT refresh, and one retry after a 401. Access tokens live for 15 minutes and refresh tokens for 60 days.
- Requests to `https://api.hoodunited.org` or the fixed legacy Container Apps host. Redirects are disabled, and every 3xx is a hard error without reading `Location`.
- The mandatory `GET /api/v1/tenants/me/` gate after login, stored-token authentication, and refresh. The returned tenant ID must match `scripts/e2e/allowed-tenants.json`, `is_synthetic` must be exactly true, and `is_eval_sink` must be exactly false. Missing fields fail closed and name the `feat/chat-redaction-receipt` server contract.
- Fixed synthetic wake/chat turns, adaptive polling, metadata-only history and receipts, and PII list/keep/stop/count for the allowlisted tenant.
- Creation and deletion of one disposable non-main thread for each command that sends a turn.

## What must not be automated

- Arbitrary URLs, localhost/development mode, tenant IDs, account emails, or message text; generic HTTP or management-command passthrough.
- Signup, onboarding, provisioning, production tenant enumeration, or claims that a current PAT is least-privilege for chat/PII.
- Stripe operations, account deletion, `make deprovision`, Azure resource deletion, fleet-wide retire operations, or any `--all` command.
- Token, password, reply text, redaction value, relationship, notes, raw log sample, or error-signature output.

Thread deletion removes its control-plane chat rows but does not prove OpenClaw memory removal. PII stop retires mappings; removing a denylist key would not restore them. There is no public full-reset operation.

## One-time setup

1. Provision a dedicated tenant through the normal operator flow. Set `is_synthetic=True`, `is_eval_sink=False`, `model_tier="starter"`, `is_budget_exempt=False`, `purchased_credit=0`, and a small explicit `monthly_cost_budget`. Do not attach Stripe IDs.
2. Confirm `/api/v1/tenants/me/` exposes `is_synthetic=true` and `is_eval_sink=false`; every command fails closed if either assertion is unavailable.
3. Replace both placeholders in `scripts/e2e/allowed-tenants.json` with exactly `{"tenant_id":"<canonical UUID4>","email":"<dedicated account email>"}`. Keep it an owner-controlled regular file; symlinks, extra keys, non-UUID4 IDs, invalid emails, and group/world-writable files are refused.
4. From the repository root, run `.venv/bin/python scripts/e2e/nbhd_e2e.py login`. Enter the password only at the hidden prompt. The email comes only from the allowlist. The password is never stored; the JWT pair uses one atomic JSON item in Keychain service `org.nbhd.e2e`, account `credentials`. A read of the former `access`/`refresh` items migrates them once, verifies the atomic copy, then deletes both legacy items. Every `security` subprocess is capped at 30 seconds and by the command's remaining deadline.
5. Run `.venv/bin/python scripts/e2e/nbhd_e2e.py smoke`. Add `--follow-up` only when intentionally exercising the optional post-denylist check.

The first smoke changes durable PII fixture state by stopping the name from being hidden. Repeating the full smoke is not guaranteed to recreate the original two-redaction condition; reset requires an operator-reviewed procedure that does not exist in the public harness.

## Rate limits and polling

Runs are sequential. Login is limited to 30/minute/IP and 10/minute/email; chat send is 300/hour/user. One absolute monotonic deadline covers authentication, HTTP timeouts, `Retry-After`, and polling. A retry delay that cannot fit before the deadline is rejected. Disposable-thread deletion receives a separate bounded 15-second cleanup deadline so an exhausted poll budget cannot suppress cleanup.

Reply polling mirrors iOS: every 1.5 seconds until 180 seconds, every 5 seconds until 300 seconds, then every 15 seconds to a 900-second ceiling. A terminal reply must be `status=ready`, `source=tenant`, have an empty error, and contain non-empty reply text in memory.

## Wake behavior

There is no subscriber wake endpoint. Normal smoke lets its first assertion turn wake the tenant. The explicit `wake` command checks `hibernated_at`; when set, it sends a fixed probe in a disposable thread and waits. A cold iOS-path wake may retry the drain after about 20 seconds, schedule buffered delivery after about 45 seconds, and resume crons after about 60 seconds. `/health/` proves only Django process/build liveness, not tenant readiness.

## Commands

```text
login
wake
chat send --wait
chat history [--since CURSOR] [--limit N]
receipts MESSAGE_ID
pii list
pii count
pii keep                       # discovers current fixed-fixture bindings itself
pii stop "Evelyn Testwell"
smoke [--follow-up]
logs                         # UNAVAILABLE
```
