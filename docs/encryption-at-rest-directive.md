# Encryption-at-Rest for User Communication — Design Directive

**Date:** 2026-07-09 · **Status:** DESIGN — nothing implemented
**Provenance:** produced by a multi-agent pass — 7 subsystem mappers → 4 independent designs → 3 judges → synthesis → 3-lens adversarial red team. Red-team findings are folded in below (register in §13). All file:line cites verified against `main`-era working tree.

---

## 1. Goal and the honest ceiling

Privacy is a core product value. Today **every word a user has ever said to their assistant rests in plaintext** — Postgres chat/journal tables, the per-tenant file share, and the PII placeholder→real-identity map itself (`Tenant.pii_entity_map`, `apps/tenants/models.py:490`). Anyone with a DB backup, the storage account key, or a Django shell reads all of it.

**The honest ceiling, stated once:** the assistant reads plaintext server-side on every turn (`RuntimeJournalContextView`, digest builds, redact/rehydrate). We can never truthfully say *"we can't read your data."* What we CAN deliver:

- a stolen database backup is opaque ciphertext (T1);
- casual operator reads (`psql`, admin) are stopped, and routine decrypt paths are logged — a determined operator with code execution in the Django process can still read data, and we say so (T2);
- compromising tenant A's container yields zero decryption capability for anyone, including A's own at-rest data (T3);
- deleting an account destroys its key, making its ciphertext unrecoverable — with named caveats about backups and embeddings until later phases close them (T4);
- message text stops leaking into logs, Sentry, and convenience copies (T5).

**Non-goals:** mathematical E2E/zero-access (impossible with a server-side assistant); encrypting pgvector embeddings in v1 (kills recall; see §11 residuals); encrypting the file share at app level (§6).

---

## 2. Ground truth (condensed from the subsystem maps)

- **Redaction is at-egress, not at-rest.** Stored content is plaintext; `redact_user_message`/`redact_tool_response` act on in-flight text bound for model providers; `rehydrate_*` restores real values at user-facing seams (~15 call sites). Encrypting content stores does NOT break live redaction — the layers compose. The only bulk plaintext reader is `memory_sync.render_memory_files` (`apps/orchestrator/memory_sync.py:21-96`).
- **Content tables (Postgres, all plaintext):** `AppChatMessage.user_text/reply_text` (user_text verbatim, reply rehydrated), `ConversationTurn` (placeholder-space user_text, rehydrated reply), `ProactiveOutbound.message_text` (fully rehydrated, never deleted), `LineOutboundMessage.text_excerpt`, `BufferedMessage.payload/user_text` (**raw pre-redaction webhook**), `PendingMessage.payload/user_text` (never deleted in practice), `ChatThread.title`; journal: `Document.title/markdown`, `DailyNote/UserMemory/JournalEntry/WeeklyReview`, `Goal/Task` titles+descriptions, `Lesson.text/context/galaxy_note/cluster_label`, `DocumentChunk.text` + embeddings, `TutoringSession.messages`, `PendingExtraction.text`, insights tables.
- **Search:** `nbhd_journal_search` = query-time Postgres FTS over `Document.title+markdown` (`runtime_views.py:2039-2055`) — no persisted index; plaintext IS the index. `grounding_probe.py:85` additionally does `markdown__icontains` substring scans.
- **File share (`ws-<prefix>`):** mounted SMB via the **shared storage account key** (`azure_client.py:620-642`) — one key reads every tenant. Container reads files directly; `sanitize_share_text` strips NUL/C0 (ciphertext-hostile); `entrypoint.sh:101` JSON-parses config or dies. `USER.md` is **not redacted** on write (`workspace_envelope.py` — zero `RedactionSession` calls). Non-BYO session transcripts live on ephemeral EmptyDir, NOT the share; BYO tenants' `claude-state/projects/*.jsonl` transcripts DO rest on the share.
- **Key infra:** Key Vault `kv-nbhd-prod`, `SecretClient` only (no `KeyClient`/wrap/unwrap anywhere), per-secret RBAC to per-tenant MIs proven by the internal-key migration (complete fleet-wide). No app-level crypto of any user data exists. `cryptography` is vendored but unused for app data.
- **Deletion is not deletion:** `deprovision_tenant` leaves journal rows, the PII map, `tenant-<uuid>-internal-key`, BYO and integration-token secrets behind; KV deletes are soft (7–90d recoverable); `purge_deleted_secret` used nowhere.
- **Processes:** `startup.sh` runs `manage.py migrate` → gunicorn (2 workers, gthread, no `--preload`, `--max-requests` recycling) **plus a separate poller process** — three+ app processes, each with its own memory.

---

## 3. Architecture

### 3.1 Key hierarchy

```
Azure Key Vault
  kv-nbhd-prod   (existing) — platform secrets, unchanged
  kv-nbhd-keks   (NEW)      — per-tenant KEKs only, soft-delete ON (7-day), purge-protection OFF
     └── kek-<tenant-uuid>     KV *Key* (RSA-3072), wrap/unwrap only, HSM-resident, versioned

Postgres — NEW side table (not columns on tenants):
  tenant_deks(tenant_id, dek_epoch smallint, wrapped_dek bytea, kek_version text, created_at)
     · one row per DEK generation; multiple generations coexist during DEK rotation
     · KEK rotation re-wraps IN PLACE (updates kek_version, same dek_epoch — no data re-encrypt)
     · DEK epoch bump = new key material = background re-encrypt while both rows remain

Derived per use via HKDF-SHA256 from the unwrapped DEK (domain separation, one wrapped secret):
  K_content = HKDF(DEK, info="content-v1")   AES-256-GCM content encryption
  K_map     = HKDF(DEK, info="map-v1")       AES-256-GCM PII-map encryption
  K_search  = HKDF(DEK, info="search-v1")    HMAC-SHA256 blind-index key (quarantined)
```

Why this shape:
- **Wrapped DEK in Postgres under a per-tenant KEK**: a stolen backup contains only wrapped DEKs — inert without a live KV `unwrap`. Per-tenant KEK purge shreds exactly one tenant, including everything in backups that is ciphertext.
- **Side table, not a single column** *(red team: a single `dek_wrapped` column cannot represent rotation — rows at the old epoch become undecryptable mid-rotation, and hibernated tenants waking across a rotation lose their buffered history)*. KEK version and DEK epoch are **separate concepts**: KEK rotation is a KV re-wrap with no epoch bump and no re-encrypt; DEK rotation adds an epoch row and dual-reads until backfill completes.
- **One DEK + HKDF subkeys, not three wrapped keys**: one thing to mint, cache, rotate, shred. Leaking the frequency-analyzable `K_search` reveals nothing about `K_content` (HKDF is one-way). Search-key rotation = bump info string to `search-v2` + rebuild the index.

### 3.2 Identities and RBAC (custom roles — the built-ins don't work)

*(Red team: Azure's built-in `Key Vault Crypto Officer` has no `wrap` dataAction — mint would 403 — and `Crypto User` bundles wrap+unwrap inseparably, which would silently hand the provisioner fleet-wide unwrap.)*

| Identity | Custom role dataActions on `kv-nbhd-keks` | Used for |
|---|---|---|
| Provisioner MI (existing `AZURE_PROVISIONER_CLIENT_ID`) | `keys/create`, `keys/rotate`, `keys/delete` (begin_delete only), `keys/wrap/action` — **NO unwrap, NO purge** | KEK mint at provision, DEK wrap, KEK-rotation re-wrap (wrap side), begin_delete at deprovision |
| Decrypt broker MI (`mi-nbhd-decrypt`, NEW) | `keys/unwrap/action` only | The ONLY identity on decrypt seams; also the unwrap side of rotation |
| Break-glass shred identity (NEW, no standing assignment or PIM-style just-in-time) | `keys/purge/action` | Interactive `manage.py crypto_shred` only — never the automated deprovision path |
| Tenant MIs (`mi-nbhd-*`) | **nothing** on `kv-nbhd-keks` | Containers never see DEKs — this is what closes T3 |

*(Red team: purge-protection OFF + delete authority on the everyday provisioner = a one-command irreversible fleet wipe held by the most-exposed identity. Fix: deprovision only soft-deletes; a human runs the confirmation-gated purge after the 7-day grace. The 7-day recoverability of a deleted tenant's key is not a real T4 gap at this scale; an irreversible fleet-wipe button is a real availability catastrophe.)*

### 3.3 Ciphertext envelope

Stored in sibling `<col>_enc bytea` columns (NUL-safe; legacy `TextField` untouched until post-soak drop):

```
byte 0        0x01                   alg tag (AES-256-GCM v1) — also the dual-read marker
bytes 1..2    dek_epoch (uint16 BE)  which tenant_deks row decrypts this
bytes 3..14   nonce (12 random bytes)
bytes 15..    ciphertext || GCM tag
AAD = f"{tenant_id}:{table}:{column}".encode()   — EXACTLY this, at EVERY write site
```

- **AAD contains NO row id — ever** *(red team CRITICAL: `reply_text` is encrypted both post-INSERT via `.update()` (pk known) and at-INSERT by `ChatLocalTurnView` (`chat_views.py:646-656`, no pk yet); any "append row_id where cheap" policy makes every on-device turn permanently undecryptable — GCM fails closed on AAD mismatch and the plaintext was never stored)*. AAD stops cross-tenant/table/column relocation. Disclosed residual: an attacker with DB write can swap two blobs within one tenant+column or replay an old blob into a row — displacement of the tenant's own text to the tenant's own owner; not worth the data-loss risk of row-binding.
- **Dual-read**: `0x01` marker present → decrypt; else return the legacy column verbatim as plaintext. This makes rollout per-row, per-tenant, reversible in both directions.
- AES-GCM random-nonce bound (~2³² per key) is unreachable at per-tenant volume; if a column ever gets hot enough, switch it to counter nonces.

### 3.4 DEK cache and pre-warm

`apps/crypto/cache.py`: per-**process** dict (2 gunicorn workers **and the poller** — red team: the poller drains every Telegram/LINE batch and was unaccounted for), key `(tenant_id, dek_epoch)`, value = unwrapped DEK. Properties:

- **Never evicted on KV failure** — entries are immutable per epoch, so a cached DEK stays valid across an arbitrarily long KV outage.
- **Pre-warm ALL provisioned tenants (~35), including hibernated ones** — hibernated tenants ARE the wake storm; warming only "active" ones guarantees cold misses exactly when KV pressure peaks.
- **Pre-warm is async and best-effort, never a boot gate** *(red team: a synchronous pre-warm in `AppConfig.ready()` runs inside `manage.py migrate` — before the new columns exist on first deploy — and inside every process; a KV throttle at deploy time would flap gunicorn boot and take down messaging fleet-wide)*. Gate to WSGI/poller entry, skip management commands, log-and-continue on any failure; cold miss → one broker `unwrap`.
- Hot path never calls KV; cold misses are new tenants and post-recycle first-touches only (gunicorn `--max-requests` recycles workers — pre-warm re-runs async after recycle).

**Named residual (T2, disclosed — do not paper over):** every app process holds fleet DEKs in RAM. An operator (or attacker) with code execution in the Django container reads everything, unlogged. Mitigations: no `az exec` (existing rule), no shell into prod, subkeys derived on demand, DEK buffers never logged; the honest claim wording in §10 reflects this.

### 3.5 Decrypt seams and audit

```python
from apps.crypto import box
blob = box.encrypt(tenant_id, table, column, plaintext)
text = box.decrypt(tenant_id, table, column, blob)          # dual-reads legacy plaintext
texts = box.decrypt_bulk(tenant_id, table, column, blobs)    # one audit event for N blobs
```

- Explicit service calls everywhere a `tenant_id` is in hand (runtime endpoints already set per-tenant RLS at `runtime_views.py:154`; memory_sync, arbiter, owner reads, platform LLM features).
- A thin `EncryptedTextField` reading the DEK from a contextvar set at the RLS boundary is allowed ONLY in unambiguous single-tenant scopes; **never** in admin/cross-tenant/bulk loops (`from_db_value` cannot reliably resolve the row's tenant).
- **DecryptAudit** *(red team: per-blob auditing = ~120 synchronous INSERTs per digest render — a write-amplification bomb whose noise also buries the one admin decrypt it exists to catch)*:
  - one event per **operation** (`decrypt_bulk` boundary), carrying `row_count`;
  - **only `admin` and `owner_request` principals are audited**; `system_cron`/`runtime_endpoint` decrypts are the service functioning, not a human reading;
  - sink is **Log Analytics** (operator with DB creds cannot rewrite it), not a same-DB table.
- Support tooling: `manage.py decrypt_tenant_rows --tenant --table (--id | --status --since)` (audited, broker unwrap) **plus a metadata-only triage view** (status, timestamps, `text_len`, error, phase — all plaintext sidecars) so a crypto/KV incident is diagnosable without any DEK *(red team: the debug tool must not share fate with the failure it debugs)*.

---

## 4. The search story

Keep `nbhd_journal_search` working via a **per-tenant keyed blind index**, shipped in the SAME PR that encrypts `Document.markdown`:

- New `Document.search_blind tsvector` + GIN. Write path: stem `title`/`markdown` with the same text-search config Postgres uses at query time → `HMAC-SHA256(K_search, lexeme)[:12]` hex tokens → `setweight(A)` title / `setweight(B)` body — preserving `SearchRank` **ordering**, not just recall.
- Query path: stem + HMAC the `websearch` terms → `to_tsquery` over hex tokens → identical `SearchRank(...).order_by('-rank')`. Snippets (`_make_snippet`) cut from the **decrypted** markdown of matched rows only.
- **Phrase/negation honesty** *(red team: `websearch` supports quoted phrases and `-negation`; hashed tokens can't; silent wrong results are worse than absent features)*: the query rewriter strips quotes → AND and the tool description tells the agent phrases are no longer phrase-matched; negation maps to `!token` where stemming allows, else is dropped with a logged warning.
- **Grounding probe keeps substring semantics** *(red team: `grounding_probe.py:85` `icontains="cardio"` matches "cardiovascular"; a token index flips the proactive-grounding gate silently — messages wrongly suppressed or wrongly passed)*: candidate-gather via blind index PLUS a bounded recent-doc window, then the existing `term in blob` substring tests run over **decrypted** markdown of candidates.
- **Shadow-mode cutover with an adversarial corpus**: write `search_blind` while serving old FTS; diff old-vs-new across **all tenants' organic queries plus a synthesized phrase/negation/stemming corpus** (one canary's organic mix won't exercise the failure modes); flip on parity; `Document.markdown` stays plaintext until the diff is clean.
- **pgvector untouched**: embeddings stay plaintext floats; co-located verbatim text (`DocumentChunk.text`, `Lesson.text`) is encrypted; `generate_embedding` decrypts in-process before the OpenAI call. Embedding leak disclosed in §11.

**Value-predicate inventory + CI guard** *(red team found a live miss)*: every DB-level value predicate on a to-be-encrypted column must get a sidecar or rewrite — confirmed sites: `Lesson.galaxy_note__gt=""` (`apps/lessons/agent_context.py:91` — needs `has_galaxy_note` boolean), `markdown__icontains` (grounding), `(text or "").strip()` empty-turn suppression in `chat_history.py` (needs `has_text`/`text_len` sidecars), every `reply_text=""` writer. Add a CI grep-guard failing the build on value predicates against encrypted columns (same spirit as the plugin/Dockerfile guard).

---

## 5. The PII-map story

Redact-at-egress and encrypt-at-rest are **orthogonal, composable layers**: redaction answers "what may leave to a model provider," encryption answers "what does a thief/backup/operator see." Live-traffic redaction reads in-flight text, not rows — unaffected. The map is encrypted **LAST (Phase 4)** because it sits on the hot path of every inbound redact, every outbound rehydrate, and the hourly fleet arbiter — the DEK cache and degraded mode must be proven on lower-stakes data first.

Mechanics when Phase 4 lands:
- One `K_map` blob per column: `pii_entity_map_enc`, `pii_denylist_enc` (bytea on `tenants`).
- **Five locked writers, not three** *(red team correction)*: redactor (`redactor.py:408-471`), memory_sync (`memory_sync.py:79-94`), arbiter (`arbiter.py:242-304`), settings map-edit and map/denylist-delete (`tenants/views.py:1120-1353`). Each keeps `select_for_update` → decrypt in-process (cached DEK, no KV hop) → mutate → re-encrypt → save inside the existing transaction.
- Arbiter's `exclude(pii_entity_map={})` → plaintext sidecar `pii_entity_count` (+ `pii_denylist_count`), maintained by **one shared write-helper that every writer funnels through** *(red team: five hand-maintained `.update()` sites WILL drift — a stuck-at-0 count silently exempts a tenant from arbitration forever)*.
- **Sequencing with #1074** *(red team: the unmerged self-cleaning work — hygiene/junk sweeps, review queue — queries the map at the DB level in ways the count sidecar doesn't cover)*: **merge #1074 first**, then inventory every `pii_entity_map`/`pii_denylist` DB-level access it introduced, route each through decrypt-then-filter or add metadata sidecars, THEN ship Phase 4. The CI grep-guard covers the map columns too.
- Disclosed: the map's real names egress to Claude Haiku during hourly arbitration (`arbiter._call_arbiter_llm`) — opaque at rest ≠ never leaves the box.

**Pseudonymize-at-rest amplification (Phase 0):** `Goal.title`, `Task.title`, `Lesson.text`, `MeditationSession.title`, `PendingExtraction.text` are already placeholder-space at rest. Convert the three fully-rehydrated copies — `ProactiveOutbound.message_text` (richest real-PII copy in the control plane, never deleted today), `AppChatMessage.reply_text`, `LineOutboundMessage.text_excerpt` — to placeholder-space-at-rest + rehydrate-on-read via the existing proven seam (`parse_markdown_items` runs before conversion). Honesty guardrail: `AppChatMessage.user_text` and `Document.markdown` are the user's own verbatim words (real names they typed) — pseudonymization can NEVER cover them; that's what Phases 2–3 encryption is for.

---

## 6. The file-share story

**Do NOT app-encrypt the share** (unanimous, red-team-confirmed): the container reads it over SMB with no decrypt hook; `entrypoint.sh` dies on unparseable config; `sanitize_share_text` corrupts ciphertext; and shipping the DEK into the container reopens exactly the T3 hole the internal-key migration closed. Instead:

- **Phase 0: redact `USER.md` on write** — it is NOT redacted today; goals/tasks/lessons/profile land in plaintext. Cheap, real, live gap.
- **Infra floor (parallel track, no remount):** storage-account CMK (key in `kv-nbhd-keks`) + infrastructure double-encryption + Log Analytics CMK. Covers disk/backup-file theft only — see §11 for what it does not cover.
- **Share isolation is its OWN canary-first fleet migration (Phase 6), not a week-one config flip** *(red team: re-pointing every container's volume = new revision on ~35 single-revision apps — the exact 409/wedge fan-out the memory warns about — mislabeled "low risk" in the draft synthesis)*. Also: **verify before committing** whether Container Apps supports identity-based SMB mounts at all — `AzureFileProperties` carries only `account_name/account_key`, so the likely-achievable win is **per-tenant storage accounts still mounted by account key**: a leaked key then reads ONE tenant, not the fleet, and Django's `list_keys` capability is the named residual. Azure caps storage accounts per subscription (~250–500/region) — fine at 35, flag at scale.
- **Share crypto-shred:** deprovision already deletes the share; destroying the per-tenant `cmk-files-<prefix>` key makes storage-side backups unreadable too.
- Ordering stays clean: memory_sync = decrypt `Document.markdown` → existing `RedactionSession.redact` → `sanitize_share_text` → write plaintext-redacted markdown to the share. Ciphertext never meets the C0-stripper.
- **Named weakest surface:** BYO `claude-state/*.jsonl` transcripts, agent-written `MEMORY.md`/`memory/*.md`, `cron/runs/*.jsonl` are share-only content protected by isolation + CMK + deletion, not content encryption.

---

## 7. Data minimization and secondary leaks (Phase 0 — days, not months)

- **Transient queues are EXEMPT from app-DEK encryption, minimized instead** *(red team CRITICAL: the KV-outage degraded mode said "buffer on the queue, never plaintext, never drop" — but Phase 2 encrypted that same queue; you cannot encrypt a buffer for a tenant whose key you cannot unwrap. All three invariants could not hold. Exempting the queues resolves the circularity: buffering never needs a DEK.)*
  - `BufferedMessage`: hard-delete on successful drain; TTL undelivered rows; store a redacted minimal envelope instead of the raw webhook where wake-reconstruction allows (verify on canary — the hibernation drain re-POSTs `payload` verbatim, `hibernation.py:1361`).
  - `PendingMessage`: hard-delete after drain + TTL (rows are never deleted today).
  - Their at-rest protection = short lifetime + redaction/placeholder-space + infra CMK floor. Disclosed as a bounded exception.
- **Pseudonymize-at-rest conversions** (§5): `ProactiveOutbound.message_text`, `AppChatMessage.reply_text`, `LineOutboundMessage.text_excerpt`.
- **NO silent `AppChatMessage`/journal TTL** — that's a memory-lobotomy of the durable transcript; if retention controls are ever offered, they're owner-visible and opt-in.
- **Log/telemetry fixes:**
  - Remove/redact the onboarding INFO lines logging raw user text (`apps/router/onboarding.py:468,506,528`).
  - Sentry: **allowlist-based** locals scrubbing *(red team: a denylist of "known content fields" cannot enumerate `d`, `row`, `plaintext`, or the DEK cache dict)* + keep `before_send_log` in mind — `enable_logs=True` forwards WARNING+ messages, so an interpolated decrypted string ships regardless of locals scrubbing.
  - **No-log guard at the crypto boundary:** `box.decrypt` returns a wrapper `str` subclass whose `repr` is a redaction marker unless explicitly `.reveal()`-ed — makes "logger.warning(f'...{text}')" safe by default.
  - `platform_issue_logs.summary/detail`: enforce redaction at write (keeps admin `search_fields` triage working — deliberately NOT encrypted).
  - **Container stdout is in scope** *(red team: containers receive raw journal markdown and log to the same Log Analytics workspace; `redact-stdout.js` masks placeholder patterns, not arbitrary prose)*: harden `redact-stdout.js` / cap container log verbosity for content-bearing paths.
  - Skip wiring `RedactTelegramToken` — Telegram is decommissioning.

---

## 8. Phased rollout

Every content migration: add nullable `<col>_enc` → deploy dual-read → throttled per-tenant backfill behind `ENCRYPT_WRITES_<table>` (default off) + allowlist → **per-tenant atomic flip: inside the writers' `select_for_update`, re-encrypt the CURRENT plaintext as the last backfill step, then flip reads** *(red team: without the flip-under-lock, a mint/append landing between backfill snapshot and flag-flip is silently lost — leaked `[PERSON_N]` to a user or a lost journal append; with no staging, an under-specified cutover IS the production test)* → soak → drop legacy column later. Rollback both directions: flag off → new writes go plaintext, `_enc` rows still decrypt.

| Phase | Contents | Risk | Verify (drive the flow) |
|---|---|---|---|
| **0 — Minimize + leak fixes** | §7 in full; USER.md redaction | Low (queue-envelope change: medium — canary wake-reconstruction first) | Canary drain survives hard-delete; share USER.md shows placeholders; LA/Sentry show no raw text |
| **0b — Infra floor** (parallel) | Storage CMK + double encryption, LA CMK — account-level, NO remount | Low | Azure portal/CLI attest CMK active |
| **1 — Crypto substrate (dark)** | `apps/crypto/*`; `kv-nbhd-keks` + custom roles + broker MI; `tenant_deks`; KEK+DEK mint wired into provisioning + 35-tenant backfill; cache + async pre-warm (workers AND poller); `box` + wrapper type; DecryptAudit→LA; stateful mock (§9); sidecar columns | Low (nothing reads ciphertext) | Round-trip on canary throwaway column; AAD mismatch fails closed; pre-warm skips `manage.py migrate`; poller warms |
| **2 — Chat stores** | `AppChatMessage.user_text/reply_text`, `ConversationTurn.*`, `ProactiveOutbound.message_text/parsed_items`, `LineOutboundMessage.text_excerpt`, `ChatThread.title`. Truncation/marker-strip/`parse_markdown_items` move pre-encrypt. Queues exempt (§7). Seams: digest, `build_since_page` (+`has_text`), `_serialize_message`, `ChatContextView`, apologies, `_build_batch_chat_content`, quote-reply | Medium | iOS `?since=` renders (no ghost bubbles); cron digest shows today's chat; LINE quote-reply resolves; hibernated tenant wakes clean |
| **3 — Journal + search** | All journal/lessons/insights content columns (embeddings stay); blind index + query rewrite + grounding-probe substring preservation **same PR**; seams: runtime GET/append endpoints, memory_sync, `generate_embedding`, `generate_cluster_labels`, poller contextual recall | High | Shadow-diff (all tenants + adversarial corpus) → parity → cutover; reconcile-scan still updates ledger; grounding gate old-vs-new diff |
| **4 — PII map (LAST, after #1074 merges)** | `pii_entity_map/pii_denylist` via `K_map`; count sidecars via single helper; five writers decrypt-mutate-encrypt under existing locks; settings UI | Highest (hot path) | Redact→rehydrate round-trip on canary; full arbiter run; fan out one tenant at a time |
| **5 — Shred + rotation** | `crypto_shred` command (break-glass, confirmation-gated, post-grace purge); deprovision = begin_delete + cleanup of today's leftovers (`tenant-<uuid>-internal-key`, BYO, integration tokens); rotation tooling | Medium (irreversible by design) | Deprovision a disposable tenant; after purge, decrypt fails; share gone |
| **6 — Share isolation** | Per-tenant storage accounts; identity-mount IF Container Apps supports it (verify first); canary-first, one-tenant-at-a-time revision fan-out with the 409/wedge runbook | High (fleet remount) | Canary container boots on new mount, `openclaw.json` parses, media/vision works |

Phases 0/0b ship this week; 1–2 prove the machinery; 3 is the hard one; 4 only after the cache + degraded mode have production hours; 5–6 complete the story.

---

## 9. Ops runbook essentials

- **Rotation.** KEK rotation (routine): new KV key version → re-wrap each `tenant_deks` row (broker unwraps old, provisioner wraps new — two identities, one offline command), update `kek_version`, no epoch bump, no re-encrypt, **no container revision**. DEK rotation (break-glass): insert epoch-N+1 row, background re-encrypt dual-reading both epochs, delete old row only when its backfill completes. Search-key rotation: bump HKDF info + rebuild index.
- **KV outage.** All provisioned tenants pre-warmed in every process; caches never evict → the fleet (including waking hibernated tenants) runs indefinitely on cached DEKs. Transient queues are DEK-free, so inbound buffering works for anyone. The only degraded case: a **brand-new** tenant during the outage — provisioning is KV-dependent anyway; their signup waits. No plaintext fallback needed, nothing dropped.
- **Crypto-shred (T4).** Deprovision: `begin_delete_key(kek-<t>)` + share delete + secret cleanup. After the 7-day grace, a human runs `manage.py crypto_shred --tenant <uuid> --confirm` under the break-glass identity → `purge_deleted_key` → every ciphertext row for that tenant, in every backup that captured it as ciphertext, is permanently dead. Automated code paths can never purge.
- **Backups.** Unchanged mechanically; they capture ciphertext + wrapped DEKs (inert). **Named holes until closed:** (a) legacy plaintext columns ride backups until dropped post-soak; (b) plaintext embeddings ride backups always (§11); (c) pre-encryption backups age out on the Supabase retention window. The Phase 5 user claim is gated on (a) and the retention window having elapsed.
- **Dev/test.** `AZURE_MOCK=true` → **stateful in-process mock key registry** — mint stores a per-tenant random key, purge deletes it, decrypt-after-purge raises *(red team: the draft's deterministic `HKDF(SECRET_KEY, tenant_id)` re-derives forever, making the shred invariant untestable)*. Deterministic seeding for fixtures. Budget a sweep of test assertions that read legacy plaintext columns; add a fixture helper that writes through the encrypt path.
- **Monitoring.** Alert on: DecryptAudit `admin` events (rare by construction — reviewable one-by-one), unwrap error rate, cache-miss rate spikes, backfill progress, shadow-diff mismatch count.

---

## 10. Honest user-facing claims per phase

- **After 0/0b:** "We keep only what we need: raw delivery copies of your messages are deleted right after delivery, your message text no longer appears in our operational logs or crash reports, and our storage is encrypted with keys we control."
- **After 2:** "Your chat history is encrypted at rest with a key unique to your account. A stolen database backup can't read it, and staff access to routine systems can't casually browse it — administrative decryption is logged to a tamper-resistant audit trail."
- **After 3:** "Your journal and memories are encrypted at rest under your account's key. Search works by matching keyed one-way fingerprints of your words — the words themselves aren't stored in the clear." *(Internal footnote: embeddings residual, §11 — do not claim vector data is encrypted.)*
- **After 4:** "Everything you write — including the private dictionary linking nicknames to the real people in your life — is encrypted at rest under a per-account key held in a hardware-backed vault, separate from the database."
- **After 5 (and only once legacy plaintext columns are dropped AND the backup retention window has elapsed):** "Delete your account and we destroy your key — your encrypted data becomes permanently unreadable, including in backups." Until then: "…and your data becomes unreadable in our live systems immediately; backup copies age out within N days."
- **Private mode (existing on-device path):** the only genuine "we can't read this" surface — "In private mode, your message is processed on your device and never sent to any cloud model."
- **Never say:** "We can't read your data." Also say the two uncomfortable truths when asked: a compromised assistant container can read *its own tenant's* content (that's what it's for), and a determined insider with production code execution could too — we minimize, log, and monitor; we don't claim impossibility.

---

## 11. Disclosed residuals (the things encryption does NOT fix)

1. **Embeddings** *(red team: modern inversion reconstructs chunk-sized text near-verbatim — names and numbers, not just "topics")*: `DocumentChunk/Lesson/Workspace` vectors stay plaintext in Postgres and every backup, and KEK purge does not reach them. v1 accepts this for product survival (recall, clustering); options later: encrypt vectors + in-RAM cosine at 35-tenant scale (kills pgvector index), or exclude vector tables from backups. Until resolved, no user claim mentions vector data.
2. **Process-memory DEKs**: fleet keys in app RAM (§3.4). Casual read stopped; code-exec insider not.
3. **Blind-index frequency leak**: deterministic HMAC exposes per-tenant token frequency/co-occurrence (topics) to a DB thief. Quarantined to `K_search`; strictly better than plaintext markdown.
4. **Share plaintext**: the container must read its workspace; protected by isolation + CMK + shred, not content encryption.
5. **PII-map names egress to Haiku** hourly during arbitration; content egresses to model providers on every turn (redacted for chat, raw for journal context). Encryption-at-rest is an at-rest guarantee only.
6. **Compromised container = its own tenant's full plaintext** via runtime endpoints and the share mount. Cross-tenant isolation holds; own-tenant exposure is inherent.

---

## 11.5 User-held keys (assessed 2026-07-09)

"One key to decrypt everything?" — No: keys are per-tenant. But one *identity* (the broker) can unwrap all of them, because the server must decrypt to run a **proactive** assistant (crons, briefings, wakes fire with no user device present). A user-gated master key therefore forks into two bad outcomes: the server keeps a copy anyway (the user's key is theater) or the assistant goes blind between chats (product lobotomy). Rejected for live data.

Where user-held keys ARE honest and planned (Phase 5+, alongside crypto-shred):
1. **Encrypted export, user-held key.** Archive download encrypted under a key we generate, display once, and do not retain. The exported copy is genuinely zero-access to us.
2. **Deletion handoff.** On account deletion: deliver the final encrypted archive + its key to the owner, then purge the tenant KEK. Truthful claim: *"You now hold the only key to your history. We destroyed ours."*

Rejected: user-passphrase "sealed vault" for live content — the assistant needs nearly all content to be helpful, the sealable remainder is tiny, and lost-passphrase lockouts are unsupportable for this audience. The genuine zero-access surface for live interaction remains on-device private mode (existing), consistent with the iOS-first / device-local-PII direction.

---

## 12. Explicit rejections

- **Mathematical E2E / "we can't read your data"** — architecture forbids it; claiming it would be dishonest.
- **Per-message RBAC brokering** ("only the tenant MI can unwrap") — RBAC propagation is minutes, not per-request; replaced by broker identity + audit.
- **App-encrypting the file share** — no container decrypt hook; sanitizer corrupts ciphertext; DEK-in-container reopens T3.
- **Encrypting pgvector embeddings in v1** — kills recall/clustering; residual disclosed instead (revisit as Phase 6+ option).
- **Encrypting `BufferedMessage`/`PendingMessage` with the app DEK** — creates the circular degraded mode; minimized + TTL'd + infra-floored instead.
- **Row ids in AAD** — guaranteed encrypt/decrypt divergence across write paths = permanent data loss.
- **Single wrapped-DEK column** — cannot represent rotation; `tenant_deks` side table instead.
- **Purge-protection-OFF-with-provisioner-delete** — an irreversible fleet-wipe primitive on the most-exposed identity; grace-window + break-glass purge instead.
- **Per-blob DecryptAudit rows in the same DB** — write-amplification on the hottest path, self-burying signal, operator-rewritable; op-boundary events to Log Analytics instead.
- **Silent transcript TTLs** — irreversible memory lobotomy; owner-visible opt-in only, someday.
- **Shared KEK** (can't per-tenant shred), **raw DEK as KV secret** (provisioner reads everyone), **implicit `from_db_value` field decryption in bulk paths**, **ORAM/FHE searchable encryption**, **client-side search index** (crons must search with no device present) — all rejected for the reasons inline above.

---

## 13. Red-team register (what the adversarial pass caught, all folded in above)

| # | Finding | Severity | Resolution |
|---|---|---|---|
| 1 | AAD "row_id where cheap" → permanent data loss on dual-write-path columns | CRITICAL | AAD = tenant:table:column, byte-identical everywhere (§3.3) |
| 2 | Single `dek_wrapped` column can't survive DEK rotation / hibernated wake | CRITICAL | `tenant_deks` side table; KEK-version ≠ DEK-epoch (§3.1) |
| 3 | Degraded mode circular: can't encrypt the buffer without the key being buffered around | CRITICAL | Queues exempt from app-DEK; minimize+TTL+CMK (§7) |
| 4 | Provisioner needs wrap ⇒ built-in roles hand it unwrap; broker split was convention, not RBAC | CRITICAL | Custom roles splitting wrap/unwrap/purge across 3 identities (§3.2) |
| 5 | Purge-protection OFF + provisioner delete = one-command irreversible fleet wipe | SEVERE | Grace window; break-glass interactive purge only (§3.2, §9) |
| 6 | Per-blob DecryptAudit = write-amplification bomb that buries its own signal | SEVERE | Op-boundary events, admin/owner only, Log Analytics sink (§3.5) |
| 7 | Pre-warm in `AppConfig.ready()` crashes `manage.py migrate` on first deploy; poller unaccounted | HIGH | Async best-effort, WSGI/poller entry only (§3.4) |
| 8 | Embeddings inversion reconstructs the "encrypted" journal from backups; Phase-5 claim false | HIGH | Residual disclosed; claim language gated (§10, §11) |
| 9 | Grounding probe substring semantics silently flip under token index | HIGH | Substring tests over decrypted candidates preserved (§4) |
| 10 | `galaxy_note__gt=""` and friends — un-inventoried value predicates | HIGH | Sidecars + CI grep-guard (§4) |
| 11 | Cutover loses concurrent mints/appends between backfill snapshot and flag flip | HIGH | Per-tenant flip-under-lock as final backfill step (§8) |
| 12 | Identity SMB mount likely unsupported on Container Apps; remount is a fleet revision migration, not week-one config | HIGH | Verify first; per-tenant accounts as the achievable win; own Phase 6 (§6) |
| 13 | Shadow-diff on one canary can't catch phrase/negation regressions | MEDIUM | All-tenant diff + adversarial query corpus (§4) |
| 14 | #1074 self-cleaning queries the map at DB level; Phase 4 collision | MEDIUM | Merge #1074 first, inventory, then Phase 4 (§5) |
| 15 | Deterministic mock key makes crypto-shred untestable in CI | MEDIUM | Stateful mock key registry (§9) |
| 16 | `pii_entity_count` drift across 5 writers | MEDIUM | Single shared write-helper (§5) |
| 17 | DecryptAudit in same DB is operator-rewritable; T2 wording overstated | MEDIUM | External sink + honest claim wording (§3.5, §10) |
| 18 | Container stdout / Sentry WARNING-interpolation still leak content post-design | MEDIUM | §7 log-plane fixes incl. `.reveal()` wrapper |
| 19 | Debug tooling shares fate with crypto incidents | MEDIUM | Metadata-only triage view + set-based support command (§3.5) |

**Build order in one line:** ship Phase 0 minimization + leak fixes + USER.md redaction now (days, real wins); stand up the substrate dark; encrypt chat to prove the machinery; encrypt journal behind a shadow-diffed blind index; encrypt the PII map only after #1074 lands and the machinery has production hours; then shred, rotation, and share isolation.
