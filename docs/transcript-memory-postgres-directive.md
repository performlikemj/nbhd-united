# DIRECTIVE: Transcript memory on Postgres — the recall leg (v3)

**Status:** v3 — incorporates the round-2 adversarial review (`transcript-memory-adversarial-review-round2-2026-08-06.md`: 3 blockers, 7 majors, closure audit). Decisions #1 (documented-OpenAI embeddings) and #2 (retire memory-core at recall ship) are MJ-RATIFIED 2026-08-06 and now invariants. Under Claude-family review (Opus/Sonnet critics + adversarial verification). Author: Fable, 2026-08-06.
**Siblings:** `assistant-context-continuity-directive.md` (push leg — extend, never contradict), `encryption-at-rest-directive.md` (at-rest contract this version now aligns with).

**One-line summary:** A fail-closed, capture-time transcript ledger (`TranscriptEvent`) written under a durable per-turn transaction, chunked with real identity and transactional invalidation, text encrypted at rest with declared search residuals, searched hybrid FTS+vector, exposed as one provenance-only tool. **The privacy invariant is now mechanical: if redaction cannot be POSITIVELY confirmed, nothing is written — ever.**

## 0. Round-2 findings → v3 responses

| Round-2 finding | v3 response |
|---|---|
| B1: redactor fail-open; on-device iOS + Telegram-webhook seams raw | C1 fail-closed capture API + full seam matrix |
| B2: capture ordering unimplementable beside current drains | C2 durable turn ledger (single transaction, stable turn_id, delivery tri-state) |
| B3: at-rest undefined; "marginal exposure zero" false | C3 adopt `DocumentChunk` `text_enc` pattern + residuals ledger accounting |
| M4: content-hash is not identity | C4 identity = (source_type, source_pk, revision) + turn_id; provider ids plumbed |
| M5: FK joins don't remove chunk text/vectors | C5 chunk-row deletion + same-transaction invalidation outbox |
| M6/M7: worker overclaimed; rechunk storms unbounded | C6 worker spec: serialized per-thread consumer, sequence cursor, radius/queue/retry caps, DLQ |
| M8: hibernated-wake rollout unproven | C7 `*_manifest_ok`-style capability gate + endpoint-side flag rejection + wake test |
| M9: disable/backfill semantics incomplete | C8 full lifecycle semantics; iOS backfill BLOCKED on fail-closed redactor |
| M10: scale claims preceded evidence | C9 measured: canary (heaviest tenant) = 511 iOS msgs total / 338 per 30d / avg 122 chars → exact-scan suffices, no ANN v1 |
| M11: same-name collisions | C10 deterministic refusal/disambiguation; notes prose banned from FTS expansion too |

## 1. Product contract (unchanged from v2 except honesty upgrades)
1. Recall from enablement forward ("memory has a birthday"); backfill only per §C8.
2. Provenance, never truth — typed stores authoritative; tool description verbatim from round-1 finding 9.
3. Privacy floor is mechanical (C1), at-rest residuals are declared, not hand-waved (C3).
4. Excerpts are data: role-labeled, framed historical, never instructions; top-k ≤ 5, ≤ 600 chars, daily cap 40 calls/tenant.
Non-goals v1.0 unchanged (no OC-jsonl mining, no cross-tenant, no send-path reads).

## 2. Architecture v3

### C1 — Fail-closed capture API (the cornerstone)
`capture_transcript_event(...) -> CaptureOutcome` where outcome ∈ {WRITTEN, QUARANTINED, SKIPPED_DISABLED}. The API accepts only `RedactionResult` objects that carry an explicit `confirmed_placeholder_space=True` produced by the redaction engine — `redact_user_message`'s fail-open fallback (returns original on error/disabled: `apps/pii/redactor.py:1132-1141`) can NEVER reach a write. On redaction failure: a **quarantine row** records (tenant, seam, turn_id, occurred_at, reason) with NO text; a repair queue re-attempts redaction from the durable raw source where one exists (iOS `AppChatMessage`); metrics count quarantines; a standing alert fires on quarantine rate > 1%.
**Seam matrix (complete or the invariant is a lie):**
| Seam | Redacted text source | Action |
|---|---|---|
| iOS queued (`_drain_ios_batch`) | `PendingMessage.user_text` (redacted at enqueue, `chat_views.py:484-553`) — but only trusted when the enqueue-time redaction outcome was recorded as confirmed | capture in C2 ledger |
| iOS on-device (`ChatLocalTurnView.post`) | none today (stores raw) | invoke redaction at capture, fail-closed |
| Telegram poller | post-redaction queue text (`poller.py:1490-1507`) | capture in C2 ledger |
| Telegram webhook | RAW today (`views.py:355-403`) | invoke redaction at capture, fail-closed |
| LINE webhook | post-redaction queue text (`line_webhook.py:1375-1386`) | capture in C2 ledger |
| Assistant replies (all channels) | model output is placeholder-space by construction; still passes the confirm gate | capture at delivery-complete (C2) |
| Proactive/cron sends (`ProactiveOutbound`) | same as assistant replies | capture at delivery-complete; recall must see what crons said |
Prerequisite fix folded in: the enqueue path records its redaction outcome alongside `PendingMessage` so the drain can distinguish confirmed-redacted from fallback-raw rows (today it cannot — that distinction is the whole game).

### C2 — Durable turn ledger
One transactional write per completed turn, keyed by stable `turn_id` (iOS `client_msg_id` / `PendingMessage.id` lineage), persisting: model-result reference, **delivery tri-state (SENT / FAILED / AMBIGUOUS)** per channel attempt, the turn's `TranscriptEvent` rows, and an **index-outbox** record — all BEFORE the pending queue row is deleted. Requires the channel delivery helpers to return delivery metadata instead of a bare boolean (small, honest API change), and fixes the LINE relay-exception swallow (`pending_queue.py:1744-1754`) so a failed delivery is never recorded as a delivered reply. Crash between external send and DB mark resolves to AMBIGUOUS → capture proceeds, duplicate-suppression on retry via turn_id idempotency. Indexing consumes the outbox asynchronously; outbox publish failure is repaired by a sweep cron (QStash, colon-free hash keys).

### C3 — At-rest: encrypted text, declared residuals
`TranscriptEvent.text` and chunk text stored as `text_enc` (same envelope as `DocumentChunk.text_enc`, `apps/journal/models.py:620-635`). The searchable derivatives — `tsv` and `embedding` — are computed app-side from placeholder-space plaintext at write time and are **declared disclosed residuals** in the encryption directive's residuals ledger (`encryption-at-rest-directive.md:225-230` already names embeddings as such): they survive key destruction and backups, they index pseudonymized tokens only, and their existence is documented as the price of search. No blind-index in v1.0; the encryption directive's keyed blind-FTS remains the upgrade path and nothing in this schema forecloses it. The v2 claim "marginal exposure zero" is retracted; the honest statement: **recall adds indefinite placeholder-space residuals for conversational text — same class as journal residuals today, new corpus.** MJ has ratified this posture via decision #1's reasoning; it is restated here so nobody discovers it later.

### C4 — Identity
`TranscriptEvent` identity: `(tenant, source_type, source_pk, revision)`; uniqueness constraint there, NOT on content hash (two "yes" turns are two events). `content_hash` is a version fingerprint only. `turn_id` groups user event + assistant reply. Provider ids plumbed where they exist (Telegram `update_id`, LINE `message.id` — added to queue payloads; small plumbing PR), else internal lineage ids. Raw provider user ids never stored on events — internal tenant-scoped opaque keys only.

### C5 — Transactional invalidation
Edit (new revision) or delete of an event: in the SAME transaction — retire affected chunk ROWS (text_enc + tsv + vector gone, not just membership joins), write invalidation-outbox entries for neighborhood rechunk. The rechunk worker consumes the outbox; a repair sweep catches stranded entries. No path exists where a deleted event's text survives in any derivative.

### C6 — Worker spec (right-sized, not overclaimed)
Per-`(tenant, channel, thread_key)` serialized consumption; ordering cursor = event PK sequence (not `occurred_at`); coalescing key per thread; invalidation radius cap ±1 window; queue-depth cap with backpressure metric; retry ceiling → DLQ visible in telemetry; algorithm-version fence (chunks stamped, mixed-version threads rechunked lazily). Connection hygiene mirrors `encrypt_chat_history._update_with_retry` for the reconnect+GUC pattern ONLY — checkpointing, batching, and DLQ are this worker's own (the round-2 caveat is accepted: that helper is a pattern for one failure class, not an architecture).

### C7 — Rollout gates (proven, not hoped)
Capability predicate `recall_manifest_ok` mirroring the existing `*_manifest_ok` pattern: config keys for recall are emitted ONLY when the tenant's image version advertises the capability (manifest `additionalProperties:false` on old images therefore never sees new keys — the wake crash-loop is structurally impossible, and a wake test proves it). Tool registration requires flag AND capability; the Django endpoint independently rejects flag-off tenants (defense in depth); rollback order: config keys cleared before image rollback. Order: image → config → indexing → tool.

### C8 — Lifecycle semantics (complete)
Flag on: capture begins (birthday recorded, shown to user). Flag off: capture stops immediately, tool hidden same config push; events AND chunks retained 30 days then purged; re-enable within 30d resumes with a documented gap, after 30d = new birthday. Tenant hard-delete cascades everything. **iOS historical backfill is BLOCKED until:** fail-closed redactor exists in prod, quarantine lineage is queryable, and a canary sample run reports a measured NER false-negative rate with an MJ-visible number. Forward-only is mandatory until then (ratified default). Telegram/LINE trailing-35d backfill allowed at enablement (same fail-closed gate).

### C9 — Capacity: measured, and small
Canary = heaviest tenant: **511 iOS messages total, 338/30d, avg 122 chars** (measured 2026-08-06). Fleet-wide corpus is O(10⁴) events; chunks O(10³). Consequences: exact cosine scan is fine (no ANN/HNSW in v1.0 — removes the plan-gate complexity until fleet chunks > 100k); embedding spend is measured pennies; batching still added for backfill politeness. Pre-rollout gate reduced to: `EXPLAIN ANALYZE` on a decoy-populated dataset at 10× current scale + one batch-embed benchmark. Cost table stays in the PR, not re-derived.

### C10 — Entity expansion, bounded and unambiguous
Query expansion: deterministic known-entity redaction of the query → placeholder set → FTS terms are placeholder tokens + canonical display-name lexemes ONLY. Note prose enters NEITHER embeddings NOR FTS terms (v2 banned the former; v3 bans both — notes are dossiers, search terms are labels). Merged placeholders expand as alias sets. Same-name distinct entities: expansion refuses to guess — it includes both alias sets tagged per entity, and the tool result labels which placeholder each hit contains (mirrors the sibling directive's same-name-fusion refusal).

### C11 — Invariants absorbed from ratified decisions
1. Embeddings: documented-OpenAI on placeholder-space text only; move to OpenRouter-ZDR route when verified; entity notes never leave the database for any provider.
2. memory-core: `experimental_memory_core_enabled` flips OFF at recall enablement on canary; the flag path is removed when fleet recall ships. Journal distillation stays.

## 3. Declared tensions for the Claude-family review round
1. Is the C1 seam matrix COMPLETE? (Hunt for ingress paths not listed: friends messages, operator one-off turns, eval harness traffic, migration imports.)
2. C2's turn ledger sits on the hot drain path — bound its latency and its failure blast radius; is AMBIGUOUS handled sanely for every channel?
3. C3 residuals: is "same class as journal residuals" honest, given conversational text is more sensitive than journal distillations?
4. C4 plumbing: does adding provider ids to queue payloads break the QStash payload contracts or dedup anywhere?
5. C8's 30-day retention-after-disable: defensible default, or should off mean purge-now?
6. C9's no-ANN call: right at current scale — what's the regret if a tenant 100×es?

## 4. Review protocol for this version (MJ-directed)
Two independent Claude critics (Opus 5: mechanics C1–C6; Sonnet 5: lifecycle/rollout/ops C7–C11 + code-anchor verification). Fable arbitrates; a third Opus 5 subagent adversarially verifies each surviving finding (refute-or-confirm). Findings and verdicts land in this PR beside the codex rounds.
