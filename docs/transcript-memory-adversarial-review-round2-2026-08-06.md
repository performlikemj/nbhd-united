# Adversarial re-review round 2: v2 directive (codex, 2026-08-06)

Closure audit of the 14 v1 findings + 10 new findings. Verdict UNSOUND — but the findings are now implementation mechanics with named existing patterns to adopt, not concept refutations.

VERDICT: UNSOUND

Reviewed the worktree v2 against the locally available `origin/main@7a9f761a2df3`. Read-only review; no files changed.

## Closure audit

| # | v1 finding | Status | v2 assessment |
|---:|---|---|---|
| 1 | Raw iOS/on-device text could enter the index | **PARTIAL** | Forwarded iOS turns now have a proposed capture seam, but the redactor is fail-open, on-device turns bypass `_drain_ios_batch`, and the Telegram webhook also captures raw text. The “only redacted text” invariant is not enforced. |
| 2 | “Any past conversation” exceeded actual retention | **CLOSED** | “Memory has a birthday,” forward-only capture, and explicit per-channel backfill limits honestly answer this. |
| 3 | Span deletion lacked stable identity and rechunking | **PARTIAL** | Event rows, membership FKs, versions, and neighborhood rechunking are the right shape. Missing: source FK/event key, transactional invalidation outbox, old-version retirement, and deletion of the chunk row/vector—not merely its join rows. |
| 4 | FTS and encrypted chunk text were incompatible | **OPEN** | v2 never defines encryption, blind FTS, key destruction, or encrypted reads for either `TranscriptEvent.text` or chunk text. Placeholder-space plaintext is not an answer to the at-rest finding. |
| 5 | Entity-note expansion leaked dossiers to OpenAI | **PARTIAL** | Prohibiting note embedding closes the provider leak. Still missing bounded note fields/lexemes and handling for sensitive or adversarial note prose used in FTS expansion. |
| 6 | RLS was not the real tenant boundary | **CLOSED** | Mandatory tenant predicates, tenant binding, decoy tests, and query-plan gates directly answer the logical isolation problem. |
| 7 | Drain scheduling could duplicate delivered replies | **PARTIAL** | The proposed ordering acknowledges the problem, but contradicts the current call structure and replaces duplication risk with permanent capture-loss windows unless a durable outbox is added. |
| 8 | Flag/plugin rollout could expose tools or crash old images | **PARTIAL** | Rollout order and capability gating are named, but no concrete manifest-capability predicate or backend authorization rule is specified. The hibernated-wake proof remains absent. |
| 9 | Tool routing under-called recall and treated chat as truth | **CLOSED** | The recommended description is adopted, typed-store precedence is explicit, and AGENTS/rules updates are required. |
| 10 | Pipeline reuse, latency, and cost were unsupported | **PARTIAL** | Batching and telemetry are improvements. There is still no source-count/token measurement, hybrid/fusion design, ANN benchmark, query plan, or substantiation for §4’s “pennies” and “<1 GB.” |
| 11 | Entity lookup/remaps could choose the wrong person | **PARTIAL** | Placeholder extraction and alias sets address merged aliases, but not two distinct same-named people. Deterministic refusal or disambiguation is still required. |
| 12 | Retrieved transcripts enabled prompt injection and bulk exfiltration | **CLOSED** | Role labels, historical-data framing, excerpt/top-k caps, daily limits, and no pagination materially answer the finding. The daily limit still needs a concrete value. |
| 13 | memory-core coexistence was unresolved | **PARTIAL** | Mutual exclusion is a sound recommendation, but remains an MJ decision rather than a directive invariant. |
| 14 | Rollout lacked safety/value measurement | **PARTIAL** | The telemetry and eval battery are substantially better. Still missing explicit MRR, language coverage, NER-miss/fail-open cases, encrypted-read indexing, and pre-rollout query-plan/backfill benchmarks. |

## New findings

### 1. [BLOCKER] The central privacy invariant is still false

For queued iOS turns, `_drain_ios_batch` structurally possesses a redacted candidate:

- `enqueue_tenant_turn` creates `redacted_text` and stores it in `PendingMessage.user_text`: `origin/main:apps/router/chat_views.py:484-553`.
- `_build_batch_chat_content` uses the prepared payload for singleton batches and `row.user_text` for coalesced batches: `pending_queue.py:1626-1675`.
- `_drain_ios_batch` still holds the batch and final model-bound `content`: `pending_queue.py:1861-1951`.

But this is not a safety guarantee. `redact_user_message` returns the original text when policy is disabled or redaction raises: `origin/main:apps/pii/redactor.py:1132-1141`. Consequently, “model-bound” does not imply “placeholder-space.”

Coverage is also incomplete:

- `ChatLocalTurnView.post` stores raw user and on-device assistant text and never enters `_drain_ios_batch`: `chat_views.py:922-1011`.
- Queued Telegram and LINE normally place post-redaction text in `PendingMessage.user_text`: `poller.py:1490-1507,1569-1575`; `line_webhook.py:1375-1386,1431-1443`.
- The Telegram webhook bypasses that path and passes `raw_user_text` directly to `record_conversation_turn`: `apps/router/views.py:355-403`.

Required change: introduce a checked capture API returning an explicit redaction outcome. On failure, skip/quarantine the event—never store the fallback original. Wire it into queued iOS, on-device iOS, Telegram poller, Telegram webhook, and LINE.

### 2. [BLOCKER] “Delivery marks first, ledger second, index third” cannot be implemented beside the current capture calls

Current ordering is:

1. Model call and channel delivery/reply persistence inside `_drain_*_batch`.
2. Conversation capture inside the Telegram/LINE helpers.
3. Return to the outer drain.
4. Mark `PendingMessage` rows delivered.
5. Hard-delete the queue rows.

Evidence: `origin/main:apps/router/pending_queue.py:1039-1084,1678-1755,1790-1849,1950-1965`.

Therefore:

- Adding `TranscriptEvent` “beside” `_capture_conversation_turn` writes it before the queue delivery mark, contrary to v2.
- Moving it after the mark requires the helpers to return assistant text and delivery metadata; they currently return only a boolean.
- If ledger writing then fails open, the queue is hard-deleted and Telegram/LINE have no durable repair source.
- If the worker crashes after external delivery but before the DB mark, the existing duplicate-model/reply window remains.
- LINE is worse: relay exceptions are swallowed before capture, so an assistant reply can be recorded despite failed delivery: `pending_queue.py:1744-1754`.

This needs a durable turn/result outbox, not three best-effort calls. Persist the model result, delivery state, transcript events, and index-outbox record under one stable turn key before deleting the queue row. External delivery needs an explicit `SENT/FAILED/AMBIGUOUS` state or provider idempotency.

### 3. [BLOCKER] The at-rest design remains undefined

The proposed event and chunk schemas contain plaintext `text`, but no ciphertext sidecar, blind index, or encrypted-read contract.

That conflicts with the existing encryption design:

- `DocumentChunk` already has `text_enc`; its vector is an explicitly disclosed residual: `origin/main:apps/journal/models.py:620-635`.
- The encryption directive requires a per-tenant keyed blind FTS index and encrypted chunk text: `origin/main:docs/encryption-at-rest-directive.md:122-131`.
- It explicitly warns that embeddings can reconstruct chunk-sized text and survive key purge/backups: `docs/encryption-at-rest-directive.md:225-230`.

D4(b)’s “marginal exposure zero” is false across storage, retention, volume, and source class, even if the provider endpoint is identical. Transcript events add indefinite conversational and assistant-reply plaintext plus additional vectors.

### 4. [MAJOR] The idempotency key is not a valid event identity

`(tenant, channel, thread_key, provider_msg_id|content_hash)` collapses legitimate repeated messages such as two separate “yes” turns and identical assistant replies. A content hash is a version/fingerprint, not identity.

Provider IDs are also not carried through the proposed seam:

- `PendingMessage` has no provider-message field: `origin/main:apps/router/models.py:90-166`.
- Telegram has a stable `update_id` at ingress but does not put it in the queue payload: `poller.py:769-800`.
- LINE reads `message.id`, especially for audio, but does not propagate it to the queue: `line_webhook.py:1020-1089,1431-1443`.

Use a stable `source_event_id`, `role`, `revision`, and `turn_id`. The existing `PendingMessage.id` and iOS `client_msg_id` can provide internal identity; provider identifiers should be carried when available. Raw LINE/Telegram user IDs should be replaced by an internal FK or keyed opaque identifier for indefinite storage.

### 5. [MAJOR] FK membership does not itself remove stale chunk text or vectors

Deleting a `TranscriptEvent` cascades its join rows, not the parent chunk containing materialized text, `tsv`, and embedding. Likewise, writing an edit with a new hash does not retire the old event automatically.

“Enqueue neighborhood rechunk” must be transactional with invalidation—normally an outbox committed alongside the edit/delete. Otherwise publish failure leaves exactly the ghosts v2 claims cannot exist.

### 6. [MAJOR] The cited retry helper is real, but too narrow to mirror as a worker architecture

`encrypt_chat_history.Command._update_with_retry` exists. It catches `OperationalError`/`InterfaceError`, closes the connection, restores the tenant service-role GUC, and retries one `.update()`: `origin/main:apps/orchestrator/management/commands/encrypt_chat_history.py:159-178`.

It does not cover:

- failed reads or checkpoint acquisition;
- multi-row chunk replacement transactions;
- external embedding calls;
- thread-level concurrency or algorithm-version races;
- dead-lettering, queue caps, or rechunk coalescing.

So the citation is genuine, but v2 overgeneralizes it.

### 7. [MAJOR] Rechunk storms and event ordering remain unspecified

There is no maximum invalidation radius, per-thread coalescing key, queue depth, retry ceiling, or version fence. A sequence of edits can repeatedly rebuild overlapping windows.

“Per-thread high-water mark” also needs a defined composite identity and ordering cursor—at least `(tenant, channel, thread_key)` plus an immutable sequence. `occurred_at` alone can tie or arrive late; creation-ID watermarks can miss backdated events. User/assistant pairing requires a stable `turn_id`.

### 8. [MAJOR] Hibernated-image rollout is plausible but unproven

The current wake path does refresh a stale image and synchronize `openclaw_version`: `origin/main:apps/orchestrator/hibernation.py:452-508`. It then schedules config application after the image update: `hibernation.py:533-564`.

However, `nbhd-journal-tools` currently has `additionalProperties:false` and only two accepted config keys: `origin/main:runtime/openclaw/plugins/nbhd-journal-tools/openclaw.plugin.json:56-69`.

V2 needs a concrete gate analogous to the existing `*_manifest_ok` pattern, plus:

- tool registration gated on both `chat_recall_enabled` and verified image capability;
- the runtime endpoint independently rejecting flag-off tenants;
- wake tests proving no new config reaches an old manifest;
- rollback clearing capability before rolling the image back.

### 9. [MAJOR] Disablement and backfill semantics are internally incomplete

V2 says `TranscriptEvent` defaults to “keep,” but disabling purges only chunks after 30 days. It does not say whether event capture continues, whether events are also purged, or how re-enable handles the disabled interval.

Historical iOS backfill is unsafe until the redactor offers a fail-closed outcome. Sampling QA cannot detect every NER miss after durable vectors have been produced. Forward-only should remain mandatory until a canary pass has deterministic quarantine, deletion lineage, and measurable false-negative thresholds.

### 10. [MAJOR] §4’s scale claim still precedes evidence

Batching improves provider throughput, but does not establish cost or latency. Current code performs one embedding request with a 10-second timeout and exact cosine ordering: `origin/main:apps/lessons/services.py:31-47`; `apps/router/poller.py:1156-1179`.

Before implementation, the directive still needs actual event counts, language/token histograms, batch limits, FTS/vector candidate sizes, fusion rules, storage amplification, `EXPLAIN ANALYZE`, and deletion/rechunk benchmarks. Post-rollout telemetry is not a substitute for a pre-rollout capacity gate.

## Single riskiest remaining thing

The riskiest remaining issue is still silent raw-text admission: a fail-open redactor, an omitted on-device/webhook seam, or unsafe historical backfill can produce indefinite `TranscriptEvent` plaintext, FTS derivatives, embeddings, and backups that survive deletion or key destruction. V2 changes the intended source table, but it does not yet make “only placeholder-space enters the index” mechanically true.
CODEX_RUN_DONE_0
