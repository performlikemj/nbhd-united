# DIRECTIVE: Transcript memory on Postgres — the recall leg (v4)

**Status:** v4 — implementation-ready pending final MJ sign-off. Absorbs review round 3 (`transcript-memory-review-round3-claude-2026-08-06.md`: Opus+Sonnet critics, Opus adversarial verifier — 9 confirmed, 3 weakened-and-restated, 0 refuted, 5 critic fixes corrected). Rounds 1–2 (codex) in the sibling review docs. MJ-ratified: embeddings posture (documented-OpenAI, placeholder-space only), memory-core retirement at recall ship, **blind index from day one**, forward-only backfill default. Author: Fable, 2026-08-06.
**Siblings:** `assistant-context-continuity-directive.md` (push leg), `encryption-at-rest-directive.md` (this version now conforms to its §4 blind-index mandate).

**One-line summary:** A fail-closed, capture-time transcript ledger (`TranscriptEvent`, ciphertext-only) written under a durable per-turn transaction spanning ALL conversation channels — including the hibernation wake path nobody remembered — chunked with two-cursor ordering and transactional invalidation, searched via per-tenant keyed blind FTS + vectors (declared residuals), exposed as one provenance-only, rate-capped tool.

## 1. Product contract
1. Recall from enablement forward ("memory has a birthday"); backfill per C8 only.
2. Provenance, never truth — typed stores authoritative; tool description verbatim from round-1 finding 9.
3. Privacy floor is mechanical (C1). At-rest: ciphertext text + HMAC'd blind word-index; embeddings are the sole declared plaintext-derived residual (frequency-safe rationale in C3).
4. Excerpts are data: role-labeled, historically framed, never instructions; top-k ≤ 5, ≤ 600 chars, 40 calls/day/tenant enforced server-side (C11).
Non-goals v1.0: OC-jsonl mining, cross-tenant anything, summarization-at-index, send-path reads, friends-message absorption (explicitly excluded: `FriendMessage.text` carries ANOTHER tenant's real names by design — indexing it breaks placeholder-space by construction).

## 2. Architecture v4

### C1 — Fail-closed capture: a TYPE, not a convention
`capture_transcript_event()` accepts ONLY a `RedactionResult` value whose `confirmed_placeholder_space=True` was set by the redaction engine itself — never a bare string, never a boolean argument a caller can pass. Fail-open fallbacks (`redact_user_message` returns raw on error/disabled) are type-incapable of reaching a write. On unconfirmed: text-free quarantine row + repair queue + alert at >1% rate. Precedent exists in-repo: `redact_for_buffer` already drops text rather than persist raw.
**Seam matrix (complete per three review rounds):**
| Seam | Redacted source | Action |
|---|---|---|
| iOS queued (`_drain_ios_batch`) | `PendingMessage.user_text` + NEW per-row redaction-outcome in `payload` (JSONField — no migration; absent key reads as unconfirmed → quarantine, which self-handles the rolling-deploy window) | ledger (C2) |
| iOS on-device (`ChatLocalTurnView`) | none today | redact at capture, fail-closed |
| Telegram poller | redacted queue text + outcome in payload | ledger (C2) |
| Telegram webhook | RAW today | redact at capture, fail-closed |
| LINE webhook | redacted queue text + outcome in payload | ledger (C2) |
| **Hibernation buffered delivery** (`deliver_buffered_messages_task` — the round-3 blocker: POSTs + relays with zero capture, hard-deletes buffers) | `redact_for_buffer` output + NEW redaction-outcome recorded at buffer write (`wake_on_message.py`) — its bare `""`-on-failure cannot satisfy the type without it | ledger at relay-complete |
| Assistant replies (all channels incl. buffered) | model output; passes the confirm gate | ledger at delivery-complete |
| Proactive sends | `record_proactive_outbound` chokepoint (corrected producer list incl. journal/extraction, first_session_welcome) PLUS the `send_to_user` runtime endpoint for container-originated sends; OC-native modes that bypass both are enumerated at implementation and either wired or documented as gaps — no silent omissions | ledger at record time |
| Operator one-off turns (`broadcast_single_tenant_task`) | EXCLUDED, documented: operator-authored, MJ-only, no queue row | — |
Drive-by fix folded in: `PendingMessage.user_text` help_text is stale ("Raw user-facing excerpt") — corrected to describe redacted reality so implementers trust the source.

### C2 — Durable turn ledger, and the delivery-truth fix that precedes it
Root defect first (round-3, verifier-sharpened): the drain's outer task marks rows DELIVERED **unconditionally** once the gateway responded, then hard-deletes — annihilating budget-exhausted turns today. v4 makes the DELIVERED mark conditional on actual delivery results returned by the channel helpers (API change: helpers return delivery metadata, not a boolean; the LINE relay-exception swallow is fixed in the same motion). THEN the ledger: one `transaction.atomic()` per completed turn (stable `turn_id`) persisting model-result ref, per-attempt delivery state **SENT / PARTIAL(delivered_chunks/n) / FAILED / AMBIGUOUS** (Telegram is per-chunk; empty-render no longer reports success), the turn's `TranscriptEvent` rows, and the index-outbox record — all BEFORE queue-row deletion, all in the OUTER task (never beside the inner capture calls, which credit-limit paths return before). Latency: negligible, not free — the block is fewer round trips than today's N autocommit saves (verified R1), and no external send ever sits inside the transaction.
**Stated loudly (was a parenthetical):** Telegram/LINE have no durable raw source after drain — a quarantined TG/LINE turn is PERMANENT capture loss. Accepted for v1.0, alarmed in telemetry, and the reason quarantine-rate alerting is not optional.

### C3 — At-rest: ciphertext-only text, blind index, one declared residual
- `TranscriptEvent.text_enc` and chunk `text_enc`: **NOT NULL ciphertext, no plaintext sibling column, no write-flag fallback** — a deliberate departure from the `DocumentChunk` dual-column dark-ship exemplar (which is a migration shape for legacy plaintext, not a greenfield pattern).
- Word search: per-tenant keyed blind index per the encryption directive §4 — `search_blind tsvector` of `HMAC-SHA256(K_search, lexeme)[:12]` hex tokens, computed app-side pre-encryption; query terms HMAC'd with the same per-tenant key at search time. Frequency leak accepted per the directive's residual #3 rationale (tolerable BECAUSE tokens are keyed); `crypto_shred` of K_search renders the index permanently meaningless including in backups.
- Embeddings: the sole plaintext-derived residual, declared in the residuals ledger; placeholder-space input only; documented-OpenAI posture (ratified) until a ZDR route is verified.

### C4 — Identity: the ledger IS the durable root
`TranscriptEvent` uniqueness: `(tenant, source_type, source_event_id, revision)`; `turn_id` groups user+assistant. Source ids: iOS = `client_msg_id` (durable `AppChatMessage` lineage — the only seam with a raw repair source); Telegram = ingress `update_id`; LINE = ingress `webhook_event_id` (both already in hand at enqueue sites, riding `payload` — no QStash contract impact, verified R2); buffered = `BufferedMessage.id` captured before hard-delete; proactive = `ProactiveOutbound.id`. `content_hash` is a revision fingerprint, never identity. Raw provider user ids never stored on events.

### C5 — Transactional invalidation with version-independent reachability
Edit/delete: same transaction retires chunk ROWS (text_enc + blind index + vector), writes invalidation-outbox entries. Reachability predicate is version-independent — membership join UNION `(tenant, thread_key, occurred_at range)` — so chunks stamped under older algorithm versions cannot escape retirement; serving refuses chunks older than the invalidation contract version.

### C6 — Worker: two cursors, bounded storms
**Consumption cursor = event PK sequence** (never skips late inserts). **Window composition order = (occurred_at, pk)** (chronology for chunk semantics; backdated on-device turns and backfill land correctly). Backfill runs as its own version-fenced pass, never appended to the live cursor. Per-(tenant, channel, thread_key) serialized consumption; coalescing; invalidation radius ±1 window; queue-depth cap; retry ceiling → DLQ; **flag-gated with drain-and-discard for disabled tenants** (a skip would poison the DLQ with routine disables). Connection hygiene mirrors `_update_with_retry` for reconnect+GUC only.

### C7 — Rollout: honest gates, written ordering
`recall_manifest_ok` boolean per the existing pattern — which is MANUAL and unreconciled (its own help_text: "Nothing reconciles this field"), so the safety statement is three enforceable properties + one runbook rule: (1) config keys never emitted while the flag is false (config_generator test, mirroring the existing manifest-not-ok byte-identical test); (2) nothing in code auto-flips the flag; (3) the runtime endpoint independently rejects flag-off tenants; (RUNBOOK) fleet image coverage is verified BEFORE any fleet-wide flag flip — explicitly written because the `latest`-tag wake path skips image refresh, so hibernated tenants can wake stale. Wake test asserts (1) against an old-manifest fixture. Order: image → flag/config → indexing → tool. Rollback clears flags before image rollback.

### C8 — Lifecycle
Enable: birthday recorded and user-visible. Disable: capture stops, tool hidden, worker drains-and-discards that tenant's outbox; events+chunks retained 30d then purged; re-enable ≤30d resumes with documented gap, >30d new birthday. Hard-delete cascades all (deprovision-vs-hard-delete semantics per `deprovision_tenant` + `_do_hard_delete`, cited). **iOS historical backfill BLOCKED until:** fail-closed redactor live, quarantine lineage queryable, canary sample run reports a measured NER false-negative rate with an MJ-visible number (the round-3 reviewers' unanimous do-not-weaken). Telegram/LINE trailing-35d backfill allowed at enablement, same gate, own version fence.

### C9 — Capacity (measured) & C10 — Entity expansion & C11 — Caps
- C9: canary (heaviest) = 511 iOS msgs total / 338 per 30d / avg 122 chars. Fleet corpus O(10⁴) events across ALL seams (multi-channel re-derivation before the EXPLAIN gate — the iOS-only number under-counts by design); exact cosine scan, no ANN v1.0; batch embed API is NEW code (benchmark it, don't assume).
- C10: expansion = deterministic query redaction → placeholder set → blind-index tokens of placeholder + canonical-name lexemes only; note prose enters nothing. **Named prerequisite (shared with the sibling directive, built once):** an inverse multimap `canonical_key → [(placeholder, entry)...]` in entity_registry — the existing `inverted_names_ci` keeps one winner and silently drops same-name collisions, so the refusal behavior both directives specify is unbuildable without it. Same-name: refuse to guess; return per-entity tagged alias sets.
- C11: 40 calls/day/tenant enforced by a per-tenant daily counter (small Postgres table, upsert-increment, checked in the runtime endpoint pre-execution — no existing counter to reuse, verified).

## 3. Implementation plan (phases, each canary-gated)
1. **P0 prerequisites:** redaction-outcome recording (PendingMessage payload + buffer write), the DELIVERED-mark truth fix + LINE swallow fix (ships value standalone — it fixes today's annihilated-turn bug), inverse multimap primitive, K_search per-tenant key minting.
2. **P1 ledger:** TranscriptEvent + turn transaction across all seams; quarantine + alerts; no search yet.
3. **P2 index:** chunker worker (two cursors), blind index, embeddings, invalidation.
4. **P3 tool:** endpoint + plugin tool + caps + AGENTS/rules updates + manifest gates; canary enablement = memory-core retirement (ratified).
5. **P4 evals/telemetry:** round-2's battery; fleet ladder MJ → Kiho → all.

## 4. Review trail
Round 1 codex (UNSOUND: concept holes) → v2. Round 2 codex (UNSOUND: mechanics) → v3. Round 3 Claude family (Opus critic UNSOUND C1–C6 / Sonnet critic SOUND-WITH-CHANGES C7–C11 / Opus verifier: 9 confirmed, 0 refuted, 5 critic fixes corrected) → v4. All review docs in this directory ride the same PR.
