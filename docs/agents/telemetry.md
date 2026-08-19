# Tool-contract telemetry

Read before adding telemetry to a tool, or before querying it to chase a drift. The debugging ladder that governs *when* to use this lives in `docs/agents/debugging.md`; this doc is the mechanism.

The point: drifts should be found because a number moved, not because someone tripped over them in conversation.

## The event

`ToolContractEvent` — `apps/platform_logs/models.py`, table `tool_contract_events`.

| Field | What it holds |
|---|---|
| `namespace` | Call-site namespace; selects the detail allowlist. `runtime` = generic capture. |
| `tool_name` | Tool / endpoint name. For generic capture this is the URL name (`core-runtime-summary`). |
| `tenant_id` | Bare UUID, nullable — **not** a FK, so events outlive tenant deletion and the insert pays no constraint check. |
| `outcome` | `accepted` · `rejected` · `normalized` · `error` |
| `reason_code` | Short slug (`http_400`, `weekday_coerced`). Empty for a clean accept. |
| `detail` | JSONB, allowlisted scalar flags only. |
| `duration_ms` | What the calling container waited for. |
| `created_at` | |

Indexed on `(tool_name, outcome, -created_at)` for rate queries, `(-created_at)` for window scans and the purge, and `(tenant_id, -created_at)` for per-tenant views.

## The allowlist invariant — content-free by construction

`emit_tool_event` (`apps/platform_logs/telemetry.py`) is the **only** sanctioned writer. Creating rows directly bypasses the guarantee. It enforces, structurally rather than by convention:

- **Allowlist.** A detail key must appear in `DETAIL_ALLOWLIST[namespace]` or `COMMON_DETAIL_KEYS`. Everything else is dropped — including all keys from a namespace nobody registered.
- **Code shape.** String values must match `SAFE_TOKEN`: no spaces, no sentence punctuation. This — not the length cap — is what stops prose riding in under a legitimately allowlisted key.
- **Length.** Surviving strings are capped at 64 characters.
- **No nesting.** Dicts and lists are dropped whole; that is what "no nested free text" means in practice.
- **Counted losses.** Drops and truncations are recorded as `dropped_keys` / `truncated_keys` so a call site losing data shows up as a number instead of as silence.
- **Fail-open.** Any exception inside emission is caught and logged. Telemetry never breaks or slows the call it observes.

`tool_name` and `reason_code` get the same code-shape treatment; prose in either is refused (`invalid_tool_name`, `""`) and logged as a warning.

**Adding a flag for a new fix wave:** add the key to that namespace's frozenset in `DETAIL_ALLOWLIST`. An unlisted key is silently dropped at write time — this is deliberate, and it is why you extend the constant rather than "just passing" the field.

## Generic capture — where it hooks and what it misses

`ToolTelemetryMiddleware` (`apps/platform_logs/middleware.py`), registered directly inside `RequestTimingMiddleware`. It fires on any request path containing `/runtime/` and emits one event with endpoint, tenant, outcome, and duration — **no per-tool wiring**, and new runtime endpoints are covered the day they are added.

Why here and not at the auth layer: `validate_internal_runtime_request` is the one function every runtime call passes through, but it runs *before* the view, so it cannot see outcome or latency. Its wrapper `_internal_auth_or_401` is copy-pasted into nine modules, with four more inline call sites and `apps/actions` using different headers — hooking "it" means editing fifteen places and still missing the next copy.

Outcome mapping: `<400` → `accepted`, `4xx` → `rejected`, `5xx` → `error`, with `reason_code = http_<status>`. `normalized` is never emitted generically — it is reserved for call-site enrichment where the tool knows it silently fixed the input.

**Not covered:**

- `GateRespondView` (`/api/v1/gate/<action_id>/respond/`) — no `/runtime/` in the path. It is called by Django's own Telegram/LINE poller with a deploy secret, not by a container, so it is not tool traffic.
- Anything failing **before** Django routing (edge 404s, container-level timeouts, a request the gateway never delivered). A tool that times out at the 20s cliff inside OpenClaw never reaches this middleware — absence of events is itself a signal, but it is not proof the tool was called.
- 404s record `tool_name = "unresolved"`. The request path is caller-controlled, so it is deliberately not echoed into the row.
- Console/API traffic is excluded by design; it is not tool traffic and would distort the rates.

## Querying rates

```sql
-- Per-tool per-day accept/reject/error, last 7 days
SELECT date_trunc('day', created_at) AS day, tool_name,
       count(*) FILTER (WHERE outcome = 'accepted')  AS ok,
       count(*) FILTER (WHERE outcome = 'rejected')  AS rejected,
       count(*) FILTER (WHERE outcome = 'error')     AS errors,
       round(100.0 * count(*) FILTER (WHERE outcome = 'error') / count(*), 1) AS err_pct
FROM tool_contract_events
WHERE created_at > now() - interval '7 days'
GROUP BY day, tool_name
ORDER BY err_pct DESC, day DESC;

-- Which reason codes is one tool rejecting on?
SELECT reason_code, count(*)
FROM tool_contract_events
WHERE tool_name = 'runtime-fuel-log' AND outcome = 'rejected'
  AND created_at > now() - interval '7 days'
GROUP BY reason_code ORDER BY 2 DESC;
```

Live control-plane DB is Supabase **us-west-1** `dljqtpunnobyztampxus` (see debugging.md). No join back to tenant content is ever needed for these.

## Commands

```bash
# Dead-tool detection — per-tool calls + error rate; flags 100%-error tools. Report-only.
python manage.py report_tool_health --days 7 --min-calls 5 [--namespace runtime] [--tenant <uuid>]

# Retention purge — default 90 days, batched.
python manage.py purge_tool_events --older-than-days 90 [--batch-size 5000] [--dry-run]
```

Scheduling is QStash, as always — trigger the purge the way the other sweeps are triggered. Do not build new scheduling infra for it.

## Phase map

Phase 1 (this) is the foundation: event, emitter, generic capture, dead-tool report, retention, docs. Phase 2 wires alerts to the personal-OC channel and an admin view; Phase 3 adds a scheduled canary conversation eval; Phase 4 ships per-fix enrichment flags alongside each audit fix. See `CONTINUITY_tool_telemetry.md`.
