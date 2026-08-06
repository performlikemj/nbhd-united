# DIRECTIVE: Transcript memory on Postgres — the recall leg

**Status:** First-pass design, awaiting adversarial review (codex). Author: Fable, 2026-08-06.
**Sibling doc:** `docs/assistant-context-continuity-directive.md` (the *push* leg: fresh digest + typed homes). This directive is the *pull* leg: retrieval over conversation history. Extend that doc's decisions; never contradict them. Its anchors convention applies here (resolve symbols via `git show origin/main:<path>`).

**One-line summary:** Replace the corpse of OpenClaw's SQLite `memory_search` (disabled fleet-wide, PR #525, SQLite-on-SMB corruption — 23/26 tenants had 0-byte indexes) with per-tenant transcript search in Postgres: chunked, placeholder-space, hybrid FTS+pgvector, exposed as one read tool. The user-visible promise: **"we've talked about this before" always resolves** — the assistant can find when, on which channel, and what was said, without the user re-explaining.

**The motivating failure (2026-08-06, MJ canary):** MJ mentioned "Optiver" three times across sessions. The entity map has `[PERSON_558]` → "a company in amsterdam, named Optiver" (annotated 08-04), so each mention reached the model with its note — but the model has NO tool that searches past conversation turns. `nbhd_journal_search` covers only distilled artifacts (journal/docs via `DocumentChunk`); if the nightly distillation didn't write it down, the archive is unreachable. Model-quality (flash-0731) made this worse, but even a perfect model cannot search a store that has no read path.

---

## 1. Product contract

1. **Recall on demand:** "when did we discuss X?" / "what did I tell you about X?" returns date-stamped, channel-attributed excerpts from any past conversation, across iOS / Telegram / LINE.
2. **Arc assembly:** repeated mentions of an entity can be assembled into a trajectory ("first raised July 2, again Aug 4 — status then was …") because retrieval returns time-ordered hits with provenance.
3. **Privacy holds:** the model sees placeholder-space text plus the entity notes it already gets today (#1373 mechanism). Recall NEVER becomes a rehydration side-channel — no raw names, no raw emails, in any tool result.
4. **Latency:** one tool call, < 1s p95. Retrieval is on the inbound-turn path only — never on the proactive send path (respects `a3c4bc6a`).

Non-goals v1: searching the assistant's own tool-call/reasoning traces (OC session jsonl on the share); cross-tenant anything; summarization-at-index-time (chunks are verbatim placeholder-space).

## 2. Architecture decisions

### D1 — Store: new `chat_memory_chunks` table, chunk = turn-window, placeholder-space at rest
New table (Django app `apps/pii`-adjacent or `apps/router`; decide in impl): `tenant FK · channel · thread_key · span_start_msg_id · span_end_msg_id · t_start · t_end · text (placeholder-space) · tsv (generated tsvector) · embedding vector · created_at`, RLS by tenant like every tenant-scoped table (re-lock after topo shifts — known gotcha).
Chunking: sliding turn-windows (~6–10 messages or ~1k tokens, 2-message overlap), not per-message — per-message kills retrieval quality for multi-turn topics.
Sources: `AppChatMessage` (iOS) + Telegram/LINE message stores — all already in Postgres, already placeholder-space (chat is placeholder-space e2e). The indexer therefore performs **no redaction of its own**; it must also perform **no rehydration** (chunks are copies of what the model was already allowed to see).
WHY Postgres: it's the platform's durable store; pgvector + FTS already proven by `DocumentChunk`; the share is banned for databases (PR #525 invariant) and stays banned.

### D2 — At-rest envelope: placeholder-space IS the boundary; derived indexes are accepted
Chunk text is placeholder-space, same at-rest bar as chat itself aims for (PII phase-3 direction: tokens at rest). `tsv` and `embedding` are derived from placeholder text — they index pseudonymized tokens, not raw PII. We explicitly accept that tsvector/embedding cannot be encrypted-column-wrapped (they must be queryable in-DB); the text column CAN reuse the `enc_columns` envelope chat rows use, computed app-side at write. State plainly: an attacker with DB read gets placeholder-space excerpts + the ability to correlate placeholders — the same exposure as `app_chat_messages` today, no worse. (Adversarial review: attack this claim.)

### D3 — Embeddings: reuse the `DocumentChunk` embedder, query-time entity expansion
Same embedding model/dims/pipeline as `DocumentChunk` (one embedder to operate, one provider bill). Embedding placeholder-space text weakens semantic match for entity-heavy queries ("amsterdam trading firm" won't be near "[PERSON_558]") — mitigate at QUERY time: expand the query with entity-map hits and their relationship notes before embedding (user says "optiver" → `inverted_names_ci` (apps/pii/entity_registry) → `[PERSON_558]` + its note text joins the query). Notes live only in the query path, never baked into stored chunks (notes change; chunks must not go stale).

### D4 — Ingestion: async, drain-triggered, debounced; backfill by command; nothing on the send path
QStash task (never Celery) scheduled on drain completion per thread with a short quiet-window debounce (reuse the `push_user_md` debounce pattern from the continuity directive's D1); idempotent chunk keys `(thread_key, span_start_msg_id, span_end_msg_id)` so re-runs upsert. One-shot backfill management command per tenant (canary first). Index lag of minutes is acceptable and stated: recall is for the archive; the *digest* (sibling directive) owns the last-hour window. Long-running backfill must handle idle-connection reconnect + RLS GUC re-set (known incident class).

### D5 — Retrieval surface: ONE new read tool in an existing plugin, hybrid search in Django
Tool `nbhd_conversation_recall(query, before?, after?, channel?)` added to `nbhd-journal-tools` (existing plugin — no new-plugin overhead; still an image change that rides the normal deploy train; config_generator emission + validator allowlist per invariant). Endpoint `/api/v1/integrations/runtime/<tenant>/memory/recall/` (internal auth, same `_internal_auth_or_401` pattern). Server does: entity expansion (D3) → FTS + vector search → reciprocal-rank fusion → top-k chunks with `{t_start, t_end, channel, text}` → through `redact_tool_response` chokepoint (belt-and-braces; text is already placeholder-space) with the entity legend the model already receives today. Tool description written to STEER USAGE: "search past conversations; use when the user references something previously discussed that is not in current context" — and the naive-detail parity/behavioral test suites extend to it like every other tool (this week's #1382 guards).
Deliberately NOT: extending `nbhd_journal_search` with a `sources=` flag (muddies a tool the model already uses correctly; distinct intents deserve distinct tools with distinct descriptions).

### D6 — Deletion follows the source
Chat reap (`reap_stale_app_chat_messages`) and any user-initiated delete must cascade to chunks whose span intersects deleted messages — wire a removal handler in the same pattern as `document_ingestion.REMOVAL_HANDLERS` (#1091). Entity remaps/merges are free by construction: chunks store placeholders, meaning resolves at query time.

### D7 — Rollout: flag default-off, canary ladder, eval-gated
`chat_recall_enabled` tenant flag, default off. Ladder: MJ canary → Kiho → fleet (standing two-stage rule). Ship with eval probes in the live eval system: the Optiver arc question, "what workout did I do yesterday and what did we say about it," and a negative probe (recall must NOT fire on in-context questions). Success = date-stamped correct answers on canary for a week before fleet.

## 3. Explicit tensions for the adversarial pass
1. The D2 at-rest claim (derived indexes over placeholder text = acceptable exposure).
2. Chunk-span deletion (D6) — is span-intersection sweep actually sound under message edits/resends?
3. Query-time entity expansion (D3) — does it leak note text into embedding-provider logs, and is that within the OpenRouter ZDR posture?
4. Tool-call routing risk: will models call `nbhd_conversation_recall` when they should (and not when they shouldn't)? What does the tool description need?
5. Interaction with the sibling directive's P2 (typed homes): does recall reduce the urgency of typed homes, or does relying on recall-over-transcripts recreate the "wipeable transcript as source of truth" anti-pattern that P2 exists to kill?
6. Backfill cost/size envelope on the largest tenant; embedding bill; p95 latency claim.
