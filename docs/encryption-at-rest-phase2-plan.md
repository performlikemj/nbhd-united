# CONTINUITY — Encryption-at-Rest, Phase 2 (iOS chat: AppChatMessage.user_text + ChatThread.title)

Companion to docs/encryption-at-rest-phase1-status.md and docs/encryption-at-rest-directive.md.

Status: PLAN — Phase 1 substrate LIVE + dark; no ciphertext yet.
Design source: docs/encryption-at-rest-directive.md §3,§8 · docs/encryption-at-rest-phase1-status.md (preconditions) · CONTINUITY_encryption-phase1.md §3.
Seam inventory: independently code-verified against current main and cross-checked by a second (audit-seams) pass — the two agree on every write/read site, both columns write-once, and zero DB predicates.

> Three amendments from the team-lead review are woven into §5 (PR-2 fail-closed inversion, PR-3 transaction discipline, PR-4 audit-principal correction); each is tagged **[team-lead review]** at its spot.

Scope lock (from MJ — do not relitigate): encrypt exactly two columns — AppChatMessage.user_text (the one still-verbatim column doing non-redundant work) and ChatThread.title (verbatim). reply_text is OUT (its two-path writer, UPDATE'd in the drain at pending_queue.py:1627, needs select_for_update first — its own later flip). ConversationTurn.* and LineOutboundMessage.* are OUT (Telegram/LINE decommissioning). ProactiveOutbound.message_text/parsed_items are registered phase2 in the guard but DEFERRED to Phase 2b here — placeholder-space (lower value) and carry an allowlisted predicate (audit_proactive_sync.py:63) needing a has_parsed_items sidecar. Encrypting user_text but leaving table-sibling reply_text plaintext is a deliberate asymmetry: user_text is the user's own verbatim real-name words (pseudonymization can never cover them); reply_text already rests placeholder-space and its writer needs locking first.

Both in-scope columns are INSERT-ONCE (verified: zero UPDATE paths). Per CONTINUITY §3 that means NO flip-under-lock — per-tenant write-flag + backfill + read-flip suffices, each step reversible until the final plaintext erase. Assumes the in-flight cache-evict-on-fresh-start PR lands first.

## 0. The two columns, precisely
- AppChatMessage.user_text — apps/router/models.py:628; TextField() (no blank/default); db_table (AAD `table`) = "app_chat_messages"; verbatim real names; insert-once; can legitimately be "" (photo/PDF-only turn, no caption).
- ChatThread.title — apps/router/models.py:555; CharField(max_length=120, blank=True, default=""); db_table = "chat_threads"; verbatim real names ("Main" for main thread); insert-once; can legitimately be "" (POST /threads/ with no title).

## 1. Complete writer inventory
AppChatMessage.user_text — 3 writers, all .objects.create(), insert-once. No UPDATE mutates it (post-insert .update()s touch only attachment_path/user_redactions/created_at; drain UPDATEs at pending_queue.py:1627/1642/1657/1676 touch reply_text/status/waking_at only):
 1. chat_views.py:333-338 — enqueue_tenant_turn, budget-exhausted (status=ERROR). user_text=text.
 2. chat_views.py:348-353 — enqueue_tenant_turn, normal (status=PENDING). user_text=text.
 3. chat_views.py:802-807 — ChatLocalTurnView.post, on-device (source=ON_DEVICE, status=READY, real-name space per models.py:629-634). user_text=user_text, already truncated [:_MAX_CHARS] at :783.
enqueue_tenant_turn is the single chokepoint for ChatMessageView POST AND the Siri escalation (siri_views.py) — both hit #1/#2. Truncation/attachment markers apply on separate copies before storage → stored user_text is already final → no post-encrypt truncation hazard.

ChatThread.title — 2 writers, insert-once, NO rename/auto-title path:
 1. chat_views.py:117-121 — _get_or_create_main_thread, defaults={"title":"Main"}.
 2. chat_views.py:504-507 — ChatThreadListView.post, title=str(request.data.get("title") or "").strip()[:120] (can be "").
Verified absent: no LLM/auto title generation, no PATCH/PUT rename endpoint (chat_urls.py = list + messages only), no .update(title=…) anywhere.

Grep false positives to ignore (different models): raw_user_text= at poller.py:846/912/1037/1361, line_webhook.py:1221 (→ _forward_to_container kwargs); user_text= at pending_queue.py:1456, views.py:387, conversation_capture.py:138 (→ ConversationTurn, models.py:161); wake_on_message.py:74 (→ BufferedMessage, models.py:46); title= at push_views.py:181/345 (APNs payload "NBHD"), extraction_callbacks.py:113/155 (Goal/Task).

## 2. Complete reader inventory
user_text readers —
Owner-facing (principal=owner_request):
 • chat_views.py:244 — _serialize_message → "user_text": msg.user_text. Serves ChatThreadMessagesView GET, poll (ChatMessageDetailView/ChatMessageView.get), ChatMessageView.post reply, ChatLocalTurnView/replay responses.
 • chat_history.py:223,229 — _app_rows in build_since_page (?since= feed, called only from chat_views.py:574): if (m.user_text or "").strip(): then text=m.user_text. NOTE: empty-check is a Python truthiness test on the fetched value, NOT a DB predicate.
System-facing (principal=system, silent):
 • conversation_capture.py:247-248,254 — _collect_turns → .only("created_at","user_text","reply_text") then "user": m.user_text. THE SINGLE direct system read. Feeds build_conversation_digest → USER.md "Conversation so far". BOTH downstream consumers funnel through it: (a) model-facing USER.md path (memory_sync→workspace_envelope) — verbatim, written to the share (decrypt in-process, then existing redact→sanitize_share_text→write; ciphertext never meets the C0 stripper); (b) owner-facing on-device ChatContextView.get (chat_views.py:702-742) which calls render_context_digest→build_conversation_digest→_collect_turns and rehydrates the rendered markdown after — a TRANSITIVE consumer of this one seam, not a separate read. user_text stays verbatim by design (conversation_capture.py:330-332). Decrypting here as system/silent is correct (audit exists to catch admin browsing, not a shared builder or the user's own device).
Not in scope (shared column name, different model): backfill_daily_notes_from_messages.py:179 (msg.payload → PendingMessage/BufferedMessage); pending_queue.py:1167/1254/1344 + hibernation.py:* (PendingMessage/ConversationTurn). The iOS drain (_build_batch_chat_content:1309, _drain_ios_batch:1530) rebuilds the prompt from PendingMessage.user_text and only WRITES AppChatMessage.reply_text — never reads AppChatMessage.user_text.

title readers —
Owner-facing: chat_views.py:201 — _serialize_thread → "title": thread.title (ChatThreadListView GET, thread embed in ChatThreadMessagesView GET, create response).
Logging/admin (leak vector, low-stakes): models.py:575 — ChatThread.__str__ → self.title or str(self.id). (AppChatMessage.__str__ :770 uses status/thread_id — safe.)
Not a content read: push_views.py:412 reads ChatThread by id only (values_list("id")).

DB-level predicate cross-check: the guard (scripts/check_encrypted_column_predicates.py) registers both columns with ZERO allowlisted sites; both my grep and audit-seams confirm NO .filter/.exclude/Q value-predicate, WHERE/LIKE, index, sort (ChatThread.Meta.ordering is last_active_at/created_at, :565), or constraint on either column. title__icontains hits at journal/lifecycle_views.py:273,316 + migrate_documents_to_typed_models.py:124 are journal Document/Task/Goal. NOTHING to add to the allowlist; no predicate/index/sort migration; NO has_text/has_title sidecar needed (only in-Python emptiness checks). The guard is already load-bearing — any future PR adding a predicate fails at PR time.

.only()/.values() sites naming the columns (must add _enc at read-flip or the deferred field triggers per-row reloads/misses ciphertext): exactly one — conversation_capture.py:247-248. build_since_page deliberately avoids .only() (documented chat_history.py:386-389).

## 3. Storage design — sidecar _enc bytea, NOT in-place
Recommend additive user_text_enc BinaryField(null=True) on app_chat_messages and title_enc BinaryField(null=True) on chat_threads; legacy TextField/CharField stays until post-soak erase (directive §3.3).
Why not in-place: box.decrypt dual-read discriminates on Python type — legacy str passes through verbatim; bytes without the 0x01 marker FAIL CLOSED (box.py:63-71). Retyping to BinaryField would be destructive AND turn every legacy plaintext row into unmarked bytes → CryptoError. Sidecar preserves the legacy column as the dual-read fallback and keeps every step reversible until erase.
Read routing (at flip): box.decrypt(tenant_id, "app_chat_messages", "user_text", row.user_text_enc if row.user_text_enc is not None else row.user_text). Prefer _enc; fall back to legacy plaintext for any not-yet-backfilled row. box.decrypt(str)→verbatim and box.decrypt(0x01…)→decrypt, so the read-flag is SAFE to flip even mid-backfill.
NULL vs b"" discriminator: _enc IS NULL = "not encrypted, use legacy plaintext"; _enc=b"" = "encrypted, value is empty" (box.encrypt("")→b"", box.decrypt(b"")→RedactedStr("")). See §6.

## 4. Flag design — two per-tenant booleans on Tenant
Match core_enabled/constellation_enabled. Two booleans, not one enum — write must precede read, each rolls back independently:
 • Tenant.encrypt_chat_writes (default False) — when True, the 5 writers ALSO populate _enc via box.encrypt. Plaintext keeps being written too (sidecar) so read stays reversible.
 • Tenant.read_encrypted_chat (default False) — when True, readers prefer _enc (box.decrypt) over legacy.
Per-tenant (not settings) required: canary = MJ's tenant first (the Asia/Tokyo tenant — leave its id a runbook parameter, do NOT query prod), then fleet one/few at a time.
Semantics: expanded-dark (off/off) → dual-write (on/off) → backfilled (on/off) → read-flipped (on/on, read _enc w/ plaintext fallback) → erased (on-enc-only/on, plaintext dropped).
Insert-once means a row inserted AFTER the write-flag flips already has _enc; backfill only closes the pre-flip gap → no lost-mint window → no flip-under-lock (directive §8's flip-under-lock is for mutable columns like the PII map).

## 5. The PR ladder
Dependency order; each PR small + reversible (except the final erase), mirroring the Phase-1 continuity style.

PR-1 — Expand: _enc columns + flags + relock · feat/enc-p2-expand (S)
- Router migration: user_text_enc, title_enc (BinaryField(null=True)) on AppChatMessage/ChatThread.
- Tenants migration: encrypt_chat_writes, read_encrypted_chat (BooleanField(default=False)) on Tenant.
- FRESH tenants relock migration same PR (clone latest ..._relock_after_*.py) so apps.tenants.test_public_schema_lockdown stays green — the tenants-app migration triggers workflow.md §pre-push-3. (Router ALTER TABLE ADD COLUMN on existing RLS tables doesn't unlock RLS; the accept criterion proves it.)
- Define the AAD identifier strings ONCE as constants (e.g. apps/router/enc_columns.py) imported by every future write/read/backfill site — see risk #6.
- Accept: makemigrations --check --dry-run clean; ruff format clean; test_public_schema_lockdown green; deploy — nothing touches _enc. Rollback: revert; drop columns.

PR-2 — Dual-write behind encrypt_chat_writes · feat/enc-p2-dual-write (M, needs PR-1)
- At each of the 5 writers: enc = box.encrypt(tenant_id, TABLE, COL, value) if tenant.encrypt_chat_writes else None, wrapped in try/except (log-count-and-continue → enc=None on failure), passed <col>_enc=enc IN THE SAME .objects.create() (one INSERT, ciphertext atomic with the row). enc=None leaves the row readable via plaintext — availability preserved.
- **[team-lead review] The enc=None soft-fail is availability-correct ONLY while the plaintext column is still written alongside it.** In PR-2 through backfill/read-flip, a box.encrypt failure that leaves enc=None is harmless — the row is still fully readable via its plaintext column. **PR-6 (erase) MUST INVERT this:** once the writers are _enc-only (no plaintext written), a box.encrypt failure must FAIL CLOSED — raise, abort the write — never silently persist a row with neither plaintext nor ciphertext, which would be permanent, silent data loss. Call this out in PR-6 so the soft-fail→fail-closed flip isn't forgotten when the plaintext write is removed.
- No new logger.*(f"…{text}") at any write site (none exist today).
- Accept (mock + canary): flag on for canary → a real iOS send writes user_text + user_text_enc with get_byte(_,0)=1, octet_length>=15; a named thread writes title_enc; box.decrypt(_enc).reveal()==plaintext; flag off → _enc NULL. Empty cases: photo-only turn → user_text_enc=b''; titleless thread → title_enc=b''. Rollback: flag off, or revert PR.

PR-3 — Backfill command · feat/enc-p2-backfill (M, needs PR-2)
- New encrypt_chat_history command, cloned from backfill_tenant_deks: zero-arg (QStash no-body triggerable) + --tenant-id/--dry-run/--max. Per-tenant isolation (RLS GUC per tenant, one at a time), idempotent: .update(<col>_enc=box.encrypt(...)) only where _enc IS NULL and legacy present ("" → b""). Register both TASK_MAP entries like the DEK backfill.
- **[team-lead review] Transaction discipline (docs/agents/invariants.md #8 — no external calls inside `transaction.atomic()`):** box.encrypt hits the DEK cache / Key Vault broker (a network call on a cold `(tenant, epoch)`), so it must run OUTSIDE any transaction. Per row (or batch), encrypt FIRST with no txn open, THEN issue a plain `.update(<col>_enc=…)` to persist the bytes — never wrap encrypt+update in `atomic()`. On `OperationalError`/`InterfaceError` (idle-in-transaction reap or a dropped connection on the cross-region link mid-sweep): `connection.close()`, re-set the RLS GUC for the current tenant, and retry the write ONCE — the `_save_session` reconnect-and-re-set-RLS pattern (see the long-QStash-task idle-DB-wedge note). This keeps the sweep from pinning a pooled backend across a KV round trip and survives a mid-sweep reap.
- Accept (mock + canary): dry-run reports COUNTS only; --tenant <canary> fills _enc for all historical rows; box.decrypt round-trips; rerun reports "0 encrypted". Rollback: none needed (writes _enc only) — to undo, UPDATE … SET _enc=NULL.
- Ordering: run only after a tenant's write-flag is on, so backfill closes exactly the pre-flip gap.

PR-4 — Read-flip behind read_encrypted_chat · feat/enc-p2-read (M, needs PR-3)
- Read helper (importing the constants) returning box.decrypt(...) when flag on and _enc non-null, else legacy. .reveal() at each egress seam (the Phase-1 RedactedStr CI guard flags raw-buffer flows into json.dumps/DRF renderer/.encode):
  - chat_views.py:244 _serialize_message → reveal user_text (owner).
  - chat_views.py:201 _serialize_thread → reveal title (owner).
  - chat_history.py:223,229 _app_rows → emptiness check stays (<decrypted> or "").strip() (b""→"", None→None); text=<decrypted>.reveal().
  - conversation_capture.py:247-254 → add "user_text_enc" to .only(); decrypt principal=system (silent); value flows verbatim downstream, unchanged.
- Bulk-decrypt for multi-row OWNER seams: ChatThreadMessagesView (page ≤ _HISTORY_LIMIT) and build_since_page (app_slice) collect the _enc blobs and call box.decrypt_bulk(..., principal="owner_request") ONCE (one audit event, row_count=N).
- **[team-lead review] Do NOT add per-view `audit.set_principal` for the single-row seams.** #1129 already sets `owner_request` AMBIENTLY at the DRF auth boundary (JWTAuthenticationWithRLS / PersonalAccessTokenAuthentication set it right after set_rls_context), so single-row owner decrypts (`_serialize_message` for poll/detail) inherit the correct principal with no per-view call. ONLY the bulk seams pass the principal explicitly — via `decrypt_bulk(principal="owner_request")` — because a one-shot bulk override is how that path attributes its single batched audit event. Adding view-level `set_principal` would be redundant and risks the stale-context bugs #1129's reset discipline exists to prevent.
- Minor cleanup: make ChatThread.__str__ (models.py:575) prefer str(self.id)/non-content label.
- Accept: drive the real iOS flow on canary — history renders exact text (no ghost/ciphertext bubbles), poll a new turn, scroll ?since=; fire USER.md refresh and read the share's "Conversation so far" (verbatim); pull ChatContextView on-device context (rehydrated). Log Analytics: owner_request audit on history loads, SILENT on cron digest. DB spot-check: serving from _enc. Rollback: read-flag off → serve plaintext.

Rollout runbook stage (ops, between PR-4 and PR-6) — not a PR: write-flag fleet → backfill fleet (QStash no-body fire, watch counts) → read-flag fleet, a few tenants at a time with §7 checks between. Verify origin/main serving-sha after each deploy.

PR-6 — Erase legacy plaintext (MJ-gated, IRREVERSIBLE) · feat/enc-p2-erase (M)
- Only after ALL tenants read-flag on, soaked ≥ N days, zero unwrap-error spikes, zero missing-history reports, backfill counts complete fleet-wide. EXPLICIT MJ go.
- Writers stop populating legacy plaintext (write _enc only; make legacy column nullable); migration crypto-erases existing plaintext (UPDATE … SET user_text='' , title=''), later follow-up drops the columns. build_since_page reads m.user_text directly with NO .only(), so the read helper must already prefer _enc fleet-wide BEFORE this lands (gated on read-flag on).
- **[team-lead review] INVERT the PR-2 soft-fail to FAIL-CLOSED here** (see the PR-2 note): with plaintext no longer written, box.encrypt failure must raise and abort the write, never persist a row with neither plaintext nor ciphertext.
- Accept: DB shows no plaintext in user_text/title; iOS still renders; box.decrypt round-trips. Rollback: NONE after erase — reversible only before this PR.

/integrate before merging PR-3 and PR-4 together if both open (adjacent code in chat_views.py/chat_history.py).

## 6. Empty-string convention — encrypt-empty via b"" sentinel, NEVER NULL
Both columns legitimately store "": user_text="" on a photo/PDF-only turn (enqueue_tenant_turn stores text unmodified); title="" on POST /threads/ with no title.
Decision: call box.encrypt(value) for EVERY non-None value including "" → stores the documented b"" sentinel (box.py:30-40). Do NOT map "" → NULL. In _enc, NULL is the dual-read discriminator ("not encrypted, use legacy plaintext"); if "" were NULL it would be indistinguishable from an un-backfilled row and read-prefer-_enc would silently serve stale data. b"" is unambiguous (distinct from NULL and from any real envelope, ≥15 bytes). Readers using (x or "").strip() behave identically since box.decrypt(b"")→"".

## 7. Verification runbook per stage (NO plaintext in any output)
- Canary write (PR-2): send a real iOS turn; probe SELECT get_byte(user_text_enc,0), octet_length(user_text_enc), (user_text_enc IS NULL) — expect byte0=1, length≥15. Named thread → same on title_enc. Never select content/decrypted value.
- Backfill completeness (PR-3): SELECT count(*) WHERE user_text_enc IS NULL AND user_text <> '' → 0 (same for title). Rerun → "0 encrypted". Counts only.
- Read-flip (PR-4): drive the actual iOS app — history exact, poll, ?since=; USER.md refresh → share "Conversation so far" verbatim; ChatContextView on-device context rehydrated. Log Analytics: owner_request audit on history loads, silence on cron digest. DB: serving from _enc.
- Erase gate (before PR-6): fleet read-flag on ≥ N days; unwrap-error rate flat; backfill counts complete; no missing-history reports → MJ explicit go.
- Discipline: DB probes use get_byte/octet_length/count/IS NULL, never the text column or a decrypted string; Log Analytics queries match audit event SHAPE, never content.

## 8. Risks / red-team
1. Push notifications — CONFIRMED not in blast radius. push_views.py reads reply_text (exclude(reply_text=""), :290) and ChatThread by ID only (:412) — never user_text/title. No push-body decrypt this phase. (When reply_text encrypts later, :290 needs a has_reply sidecar — already registered + allowlisted.)
2. PII pipeline ordering — CONFIRMED no write-path hazard. Redaction is at-egress on a SEPARATE LLM-bound copy (redact_user_message(text)); user_text is stored verbatim, never redacted. Independent copies, no ordering dependency. On read, owner decrypt returns verbatim (owner's own words); the system digest decrypts verbatim while its OTHER lines stay placeholder-space exactly as today (conversation_capture.py:321-332). title isn't PII-processed. No hazard.
3. Log Analytics leakage from new paths — new decrypt seams return RedactedStr; Phase-1 guard + .reveal()-only-at-egress keeps decrypted user_text/title out of logs/Sentry. Backfill command + 5 dual-write sites log COUNTS + tenant-id prefix only — explicit PR-2/PR-3 checklist item.
4. Backfill runtime at fleet scale — row counts unknown but estimable SAFELY with SELECT count(*) FROM app_chat_messages / chat_threads per tenant (metadata only). At 33 tenants, live-Stripe=0 subs, real volume tiny (MJ, Kiho, test tenants); each row is one AES-GCM seal with a warm DEK cache (one broker unwrap per tenant, then hits). Time the canary, extrapolate by row count; --max + per-tenant isolation bound blast radius.
5. Frontend static export — CONFIRMED never sees these columns except via API. Next.js static export (no SSR) + iOS app render user_text/title only from console-API JSON, .reveal()ed server-side under the existing keys. _enc columns are never serialized to any response. No client-side decrypt, no frontend change.
6. AAD table-string divergence (the #1 permanent-data-loss vector) — box binds AAD=f"{tenant_id}:{table}:{column}"; one byte of drift between an encrypt and a decrypt site fails GCM closed → that row unreadable forever (Phase-1 red-team #1). Define ("app_chat_messages","user_text") + ("chat_threads","title") as shared constants in PR-1, imported everywhere — never hand-typed. (db_tables confirmed.)
7. .only() deferred-field trap — conversation_capture.py:247 must add "user_text_enc" at read-flip or the decrypt touches a deferred/absent field and triggers per-row reloads. Single site; fixed in PR-4. build_since_page needs no change.
8. ChatThread.__str__ repr leak (low-stakes) — models.py:575 renders self.title in admin/logs; post-encryption that's ciphertext/RedactedStr. Prefer id/non-content label in PR-4.
9. On-device (ON_DEVICE) turns — ChatLocalTurnView writes real-name user_text authored on device; same verbatim-at-rest treatment, no special handling — box.encrypt seals it like any other. (The source-dependent nuance is entirely in reply_text, out of scope.)

Build order in one line: expand (_enc + two per-tenant flags + relock) → dual-write behind encrypt_chat_writes (canary=MJ tenant) → idempotent per-tenant backfill → read-flip behind read_encrypted_chat (dual-read covers the gap) → fleet fan-out with per-stage metadata-only probes → MJ-gated plaintext erase. reply_text + ProactiveOutbound follow as Phase 2b once reply_text's writer is locked and a has_parsed_items sidecar lands.
