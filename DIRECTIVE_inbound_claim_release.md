# DIRECTIVE — inbound claim lease: a message that never reached a durable row must not be lost

> **STATUS: PARKED (2026-08-28).** Two codex adversarial reviews (r1 on v1, r2 on this v2) both returned
> RETHINK: any behavioural fix needs a fenced lease (ownership token, a completion stamp on every branch
> that never queues, reclaim-by-outcome for edited messages / agent callbacks / LINE postbacks, a
> synchronous LINE claim before the 200) or a durable inbox (raw event stored as the claim — conflicts
> with "redact now, don't persist"). Production evidence (Log Analytics, 30 d): `Unhandled error
> processing update` 0, `Error handling LINE event` 0, `skipping duplicate` 0. Decision: ship
> observability only (`inbound_lost_infra` / `inbound_lost_poison` / `inbound_ack_unconfirmed`) and
> revisit this design the day a marker fires. The r2 review's "durable inbox" section is the recommended
> starting point then. The text below is v2 as reviewed, kept verbatim as the record.

Author: Fable, 2026-08-28. **v2** — v1 (best-effort claim deletion + JSON handoff probe) was RETHINK'd by
codex critique r1: a delete cannot succeed while the DB is the thing that failed; `PendingMessage` is not
the only durable handoff (`BufferedMessage` on hibernation); Telegram ids are stored as JSON numbers;
whole-handler retries repeat non-idempotent side effects; 502/503 are ambiguous; replays burn the rate
limit; the poller's backoff resets to 1 s. All accepted. Status: v2 → critique r2 → implement on
`fix/inbound-claim-release`.

## 1. The seam (unchanged from v1, one correction)

Every channel claims the provider event id first (`inbound_dedup.claim_inbound_event`, invariant 3) and
later writes a durable row: `PendingMessage` (`pending_queue.enqueue_message_for_tenant`) for warm
tenants, **or** `BufferedMessage` (`wake_on_message.handle_hibernated_message`) for hibernated ones. A
failure between the claim and that row keeps the claim and loses the message; the claim then makes every
provider replay a "duplicate".

| Channel | Claim | Loses the message when… | Why the provider's retry doesn't help today |
|---|---|---|---|
| Telegram poller (LIVE) | `poller.py:813` | infra error (`OperationalError`/`InterfaceError`) after the claim, before the durable row | `:820-829` treats it as poison → offset advances → Telegram acks; a restart replay is "duplicate" |
| LINE (LIVE) | `line_webhook.py:942` | same, inside the per-event daemon thread | `_handle_event` swallows (`:962`); the view already returned 200, LINE never redelivers |
| Telegram webhook (LATENT) | `views.py:348` | same; plus `forward_to_openclaw` returning `None` for a **connection-establishment failure** | view acks 200 (`:619`); duplicate sighting acks 200 (`:350`) |

Out of scope, recorded as follow-ups (§7): in-memory held messages in the poller's container-update
prompt paths (`poller.py:1107,1119`, `container_update:no` pop-before-forward), background enqueue
failures in `_delayed_forward` / `container_update:yes` threads, and anything after a durable row exists
(drain caps + reaper + apology already cover it).

## 2. Mechanism — claim lease + handoff stamp (no deletes, no JSON probes)

### 2.1 Model + migration

`ProcessedInboundEvent` gains one nullable column: `handed_off_at = DateTimeField(null=True, blank=True)`.
`created_at` doubles as the lease stamp (it is already `auto_now_add`; a reclaim re-stamps it — the 3-day
prune uses it too, which is harmless). Generate `apps/router/migrations/00xx_processedinboundevent_handed_off_at`
and a tenants relock migration depending on it (pattern: `0159_relock_after_appchatmessage_redaction_receipt`
depends on `router.0034`); run `apps.tenants.test_public_schema_lockdown`.

### 2.2 `inbound_dedup.py` API

```python
class ClaimResult(str, Enum): FRESH, RECLAIMED, LIVE, HANDED_OFF, ERROR

def claim_inbound_event_detailed(event_key, *, stale_after: timedelta | None) -> ClaimResult
def claim_inbound_event(event_key) -> bool          # unchanged wrapper: FRESH/RECLAIMED/ERROR → True
def mark_inbound_handoff(event_key) -> None         # UPDATE handed_off_at=now(); call INSIDE the durable txn
```

`claim_inbound_event_detailed`:
- `get_or_create` → created ⇒ `FRESH`.
- exists and `handed_off_at` set ⇒ `HANDED_OFF` (true duplicate).
- exists, not handed off, `stale_after is None` ⇒ `LIVE` (never reclaim: callback/postback/follow/`/start`).
- exists, not handed off, `created_at >= now - stale_after` ⇒ `LIVE` (someone is processing it right now).
- exists, not handed off, older than `stale_after` ⇒ conditional `UPDATE … SET created_at=now() WHERE
  event_key=… AND handed_off_at IS NULL AND created_at < now()-stale_after`; rows==1 ⇒ `RECLAIMED`
  (WARNING `inbound_claim_reclaimed key=… age=…`), rows==0 ⇒ `LIVE` (lost the race).
- any DB error ⇒ `ERROR` (fail-open: process; the durable write will fail the same way and enter the
  retry path — this preserves the module's "never silently lose" contract).

Per-channel `stale_after`: Telegram (poller + webhook) `20 s`; LINE `120 s` (voice transcription runs
before the handoff). Only **message** events use a `stale_after`; every other event type passes `None`.

### 2.3 Durable handoffs become atomic and stamp the claim

- `enqueue_message_for_tenant(..., claim_key: str | None = None)`: wrap `PendingMessage.objects.create`
  + the `first_message_at` conditional update + `mark_inbound_handoff(claim_key)` (when given) in ONE
  `transaction.atomic()`; publish the drain via `transaction.on_commit` **always** (outside an atomic
  block Django runs it immediately, so ordinary callers keep today's behaviour; the existing
  `defer_publish_until_commit` flag becomes redundant — keep it accepted, no-op, for the dropped-turn
  caller). No transport inside the transaction (invariant: transactions).
- `handle_hibernated_message(..., claim_key: str | None = None)`: wrap `BufferedMessage.objects.create`
  + `last_message_at` update + `mark_inbound_handoff` in ONE `transaction.atomic()`. The wake call stays
  outside. Because the stamp commits with the row, a reclaim can never create a second buffer for the
  same event (r1 P0 "stuck buffer").
- All three channels pass their claim key (`f"tg:{update_id}"` / `f"line:{webhookEventId}"`) through to
  whichever handoff they reach. Telegram `update_id` stays an int everywhere — nothing looks it up in JSON
  any more.

### 2.4 Telegram poller (`poller.py`)

- `_process_update`: use `claim_inbound_event_detailed(key, stale_after=20 s if message-update else None)`.
  `HANDED_OFF`/`LIVE` ⇒ skip + advance (as today). `FRESH`/`RECLAIMED`/`ERROR` ⇒ handle.
- Replace the single `except Exception` with:
  - `except (OperationalError, InterfaceError)`: `self._infra_failures[update_id] += 1`. If the count
    ≤ `INFRA_RETRY_CAP = 5`: log WARNING `inbound_infra_retry update_id=… attempt=n`, do **not** advance
    the offset, and re-raise. If > cap: log ERROR `inbound_lost_infra_cap update_id=… tenant=…`, advance
    the offset, drop the counter (claim stays un-handed-off as evidence).
  - `except Exception` (poison): unchanged behaviour + one structured ERROR line `inbound_lost_poison
    update_id=… tenant=…`.
  On success, drop the counter for that update_id.
- Poll loop (`~104-131`): remove the `self._backoff = 1` that runs **before** the batch is processed (keep
  the one after). In the generic `except Exception` sleep `max(self._backoff, TG_CLAIM_STALE_SECONDS)`
  when the exception is an infra error, so the re-fetch arrives after the lease is stale and reclaims;
  exponential growth is unchanged.
- Rate limit (`is_rate_limited`) stays where it is; the replay cost is bounded: ≤ 5 attempts ≥ 20 s apart.
- Side effects repeated on a reclaim for a **message** update: typing indicator, `last_message_at`,
  transcription (cost), photo upload (possible orphan file), PII/provisional writes (idempotent by event).
  Accepted. Callback / `/start` / edited updates never reclaim.

### 2.5 LINE (`line_webhook.py:_handle_event`)

- Move the initial `set_rls_context` inside the retry scope (today its failure is swallowed at `:928-933`).
- `claim_inbound_event_detailed(key, stale_after=120 s if event.type == "message" else None)`.
- For message events only: on `(OperationalError, InterfaceError)` from `_handle_message` →
  `connection.close()`, `set_rls_context(service_role=True)`, call `_handle_message(event)` **once more**.
  Second infra failure ⇒ ERROR `inbound_lost_infra channel=line event=…` (claim remains un-handed-off;
  visible + reclaimable if a redelivery ever arrives). Poison ⇒ unchanged swallow + `inbound_lost_poison`.
- Other event types: unchanged.
- `finally: connection.close()` at the end of the thread (thread-local connection hygiene).
- Repeated pre-handoff side effects on retry: loading indicator (harmless), transcription (cost). Accepted.
  Hibernation buffering is now atomic + stamped (§2.3), so it cannot double-buffer.

### 2.6 Telegram webhook (`views.py`, `services.py`) — latent in prod, covered for invariant §4

- `forward_to_openclaw(..., raise_on_connect_failure: bool = False)`: when True, `httpx.ConnectError`
  (connection could not be established — the only definite non-delivery) raises
  `OpenClawNotDeliveredError(RuntimeError)`. Timeouts, `RemoteProtocolError`, 404/502/503 and every
  other status keep returning `None` (ambiguous; the agent may already be replying via bot token).
  Automations (`apps/automations/services.py:_dispatch_to_openclaw`) don't pass the flag → untouched.
- View, message updates only: `claim_inbound_event_detailed(key, stale_after=20 s)`:
  `HANDED_OFF` ⇒ 200 "ok"; `LIVE` ⇒ **503** (a concurrent/early redelivery of an in-progress update —
  tell Telegram to try later, it will, and by then the stamp exists ⇒ 200); `FRESH`/`RECLAIMED` ⇒ process.
  Non-message updates keep today's 200-on-duplicate.
- After the claim: `except OpenClawNotDeliveredError` and `except (OperationalError, InterfaceError)` ⇒
  return **503**. Nothing is deleted; Telegram's retries land on the `LIVE`→503 / stale→`RECLAIMED` path.
  Telegram retries non-2xx deliveries with the same `update_id` (Bot API `setWebhook`/`Update`).
- The rate limiter runs after the claim; a rate-limited retry returns 429 with the claim `LIVE`; the next
  retry ≥ 20 s later reclaims. Bounded by Telegram's own retry schedule. Acceptable for a latent path.

## 3. Tests (TDD — red first)

- `inbound_dedup`: FRESH / HANDED_OFF / LIVE (young) / LIVE (`stale_after=None`) / RECLAIMED (old, not
  handed off) / RECLAIMED loses race ⇒ LIVE / ERROR on DB failure; `mark_inbound_handoff` sets the stamp.
- `pending_queue`: enqueue with `claim_key` stamps the claim in the same transaction — force the
  `first_message_at` update to raise and assert **no** `PendingMessage` row and **no** stamp; drain publish
  happens after commit.
- `wake_on_message`: same atomicity for `BufferedMessage`; a reclaim after a failed buffer write creates
  exactly one buffer and one wake.
- Poller: infra error before handoff ⇒ offset not advanced, counter=1, exception propagates; loop sleeps
  ≥ 20 s and the re-fetch reclaims (freeze time) and enqueues once; 6th infra failure ⇒ offset advances,
  `inbound_lost_infra_cap` logged; poison ⇒ unchanged + `inbound_lost_poison`; callback update never
  reclaims; backoff no longer resets before the batch.
- LINE: infra on first attempt, success on retry ⇒ one `PendingMessage`, one `connection.close`; infra
  twice ⇒ `inbound_lost_infra`, no row, claim un-handed-off; follow/postback not retried; RLS-setup
  failure enters the retry.
- Webhook: `ConnectError` ⇒ 503, claim un-handed-off; timeout ⇒ 200 unchanged; 502/503 from container ⇒
  200 unchanged; infra after claim ⇒ 503; redelivery while `LIVE` ⇒ 503; after stamp ⇒ 200; after
  20 s un-handed-off ⇒ processed once; automations still get `None` on `ConnectError`.
- Existing suites stay green: `apps.router.test_inbound_dedup`, `test_poller`, `tests_line`,
  `test_services`, `test_dropped_turn_retry`, `test_pending_queue*`, `test_wake_on_message*`, `tests`,
  `apps.automations.tests`, `apps.tenants.test_public_schema_lockdown`.

## 4. Fences and gates

Files: `apps/router/models.py` + migration, `apps/tenants/migrations/<relock>`, `apps/router/inbound_dedup.py`,
`apps/router/pending_queue.py` (atomic + `claim_key` only), `apps/router/wake_on_message.py` (same),
`apps/router/poller.py`, `apps/router/line_webhook.py`, `apps/router/views.py`, `apps/router/services.py`,
router/automations test modules (new focused modules allowed). Gates: targeted tests, `ruff check` +
`ruff format`, `makemigrations --check`, lockdown test; orchestrator runs `make docker-gate`.

## 5. Invariants touched

§3 claim-first: preserved (claim is still the first write). §4 all channels: poller, LINE, webhook, and
both durable handoffs (warm + hibernated). Transactions: durable write + stamp in one short atomic block,
publish on commit, no transport inside. QStash-only: unchanged.

## 6. Non-goals

No deletes of claim rows. No JSON payload lookups. No retry of non-message events. No retry beyond
"reconnect + once" (LINE) / "re-fetch, cap 5" (poller). No change to drain/reaper. LINE stays async.

## 7. Follow-ups (separate PRs, recorded here so they are not lost)

1. `_delayed_forward` / `container_update:yes` threads: enqueue immediately instead of sleeping 15 s in a
   daemon thread — the durable drain already handles a restarting container; then the claimed update
   always reaches a durable row before `_process_update` returns.
2. `container_update:no`: don't pop the held message before the enqueue succeeds.
3. Telegram/webhook rate-limit accounting keyed by `update_id` if the webhook ever goes live.
