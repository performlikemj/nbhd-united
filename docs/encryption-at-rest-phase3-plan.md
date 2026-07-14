# CONTINUITY — Encryption-at-Rest, Phase 3 (journal, fuel, lessons, insights, core content)

**Date:** 2026-07-14 · **Status:** PLAN — no code yet. Docs-only.
**Companion to:** `docs/encryption-at-rest-directive.md` (master design, §4 search / §8 phase table / §11 residuals), `docs/encryption-at-rest-phase2-plan.md` (the executed chat ladder this reuses).
**Anchored to:** origin/main `5980847f` (PR #1204 — provision-time chat flags + `converge_unencrypted_chat_tenants`).

> Directive from MJ (2026-07-14): "The other data should also be encrypted and we need to plan that out and test it before July 18th." An investigation the same day demonstrated journal content and fuel data are readable plaintext via raw SQL. This plan enumerates the remaining plaintext user-content stores, reuses the Phase-2 machinery unchanged, and gives an honest ladder + timeline against the 4-day window.

---

## 0. The framing you must read before the scope table

Two things are true and are constantly conflated; keep them apart:

1. **The 07-18 "irreversible plaintext erase" covers CHAT columns only** — `AppChatMessage.user_text` and `ChatThread.title` (Phase-2 plan §5 PR-6). It is gated on the *chat* soak evidence (fleet read-flag on, zero unwrap-error spikes, backfill complete, no missing-history reports). **Nothing in Phase 3 blocks it, and it should not slip for Phase 3.**

2. **Phase 3 stores have their OWN, later erase.** Encrypting journal/fuel/lessons/insights does not need to *complete* by 07-18 — only the **plan (this doc) + a canary-proven test pass** does, which is exactly what MJ asked for ("plan that out and test it before July 18th"). Fleet rollout and the P3 legacy-plaintext erase are MJ-gated and land *after* soak, decoupled from the chat erase.

The single deliverable that is genuinely due 07-18 is: **the crypto ladder for these stores, unit-tested and proven on the canary + demo tenants.** The dependency graph in §6 states plainly what ships-and-soaks vs what is explicitly deferred.

**Machinery reused, not reinvented.** Everything below rides the Phase-1/2 substrate exactly as chat does — `apps.crypto.box.encrypt/decrypt/decrypt_bulk` (AES-256-GCM under `HKDF(dek, "content-v1")`, `apps/crypto/box.py`), sidecar `<col>_enc bytea` columns with dual-read (`b""` sentinel for empty, NULL = "use legacy plaintext"), per-tenant flag pair on `Tenant`, `encrypt_chat_history`-style backfill, the shared completeness predicate (`apps/orchestrator/chat_encryption.py:count_unsealed_chat_rows`), the `converge_unencrypted_*` task, the `#1204` verify-gated provisioning flip, and the `scripts/check_encrypted_column_predicates.py` CI guard. No new crypto primitives. The only genuinely new design work is **search** (§2) and a **numeric codec** if the fuel body-metrics are pulled in (§1.4, deferred).

---

## 1. Scope inventory (verified against origin/main `5980847f`)

Legend for **Verdict**: **NOW** = encrypt in this phase · **3b** = fast-follow (search-coupled or needs its own mini-design) · **DEFER/verify** = confirm live-writer + row counts (metadata only) at execution, then fold in or retire · **OUT** = not Phase 3 (Phase 4 / cross-tenant / dead / ops-authored).

Value tier: **V** = verbatim real names (highest value) · **P** = placeholder-space at rest (medium — a thief sees `[PERSON_N]`, but topics/health still leak) · **S** = structured/metadata.

AAD `table` = the model's `db_table`; AAD `column` = the *logical* plaintext column name (never the `_enc` sidecar) — same convention as `apps/router/enc_columns.py`. Verify each `db_table` when writing the constants module.

### 1.1 Journal group (`apps/journal`) — the core memory surface

| Store (db_table) | Column | Type | Holds | Tier | Verdict |
|---|---|---|---|---|---|
| Document (`journal_document`) | `markdown` | TextField | The journal/note body — **crown jewel**, verbatim, **search-coupled** | V | **3b** (see §2) |
| Document | `title` | CharField(256) | Doc title, verbatim, search weight-A | V | **3b** (see §2) |
| Document | `target` | JSONField | Structured goal target | S | OUT |
| Goal (`journal_goals`) | `title`, `description` | CharField/TextField | User-stated intention + reflection | V | **NOW** |
| Task (`journal_tasks`) | `title`, `description` | CharField/TextField | Actionable item + notes | V | **NOW** |
| Purpose (`journal_purposes`) | `statement` | TextField | North-Star statement | V | **NOW** |
| Purpose | `evidence` | JSONField | `[{kind,ref,note}]` proposal evidence | V | **NOW** (JSON — see §1.5) |
| PendingExtraction (`journal_pending_extractions`) | `text` | TextField | Extracted lesson/goal/task card | P | **NOW** |
| DocumentChunk (`journal_document_chunks`) | `text` | TextField | Embedded chunk verbatim | V | **NOW** (embedding stays plaintext — §7.4) |
| Session (`journal_sessions`) | `summary` | TextField | YardTalk work-session summary | V | **NOW** |
| Session | `accomplishments`, `blockers`, `next_steps` | JSONField | User-derived work notes | V | **NOW** (JSON — §1.5) |
| PendingTaskAction (`journal_pending_task_actions`) | `evidence` | TextField | Journal evidence for an auto-applied action | V | **NOW** |
| DocumentIngestionArtifact (`journal_document_ingestion_artifacts`) | `content_excerpt` | TextField | Kept-item excerpt (survives deletion — forget receipt) | V | **NOW** |
| DocumentIngestion (`journal_document_ingestions`) | `original_filename`, `source_ref` | CharField | Filename / email-subject / post-title | V | **NOW** (`original_filename`); `source_ref` is an id → S/OUT |
| DailyNote (`journal_dailynote`) | `markdown` | TextField | **Legacy v1 daily note — still live-written** (`services.py:390,432,446`) | V | **NOW** (verify counts; retire path if dead) |
| UserMemory (`journal_usermemory`) | `markdown` | TextField | Legacy MEMORY.md — **no writer found** | V | **DEFER/verify** (likely dead → leave or retire) |
| JournalEntry (`journal_entries`) | `raw_text`, `reflection`, `mood` | TextField | Legacy v1 journal — **API still routed** (`urls.py:75-76`) | V | **DEFER/verify** (encrypt-if-live or retire endpoints) |
| WeeklyReview (`weekly_reviews`) | `mood_summary`, `raw_text` | TextField | Legacy v1 review — **API still routed** (`urls.py:85-86`) | V | **DEFER/verify** (same) |
| NoteTemplate (`note_templates`) | `name`, `sections` | CharField/JSON | Template seed content | S | OUT |

Writer/reader anchors (journal): mutate-in-place writers already lock via `select_for_update` at `document_views.py:443-450` (`DocumentAppendView`), `integrations/runtime_views.py:1325-1366` (`RuntimeDailyNoteAppendView`), `:1476-1485` (`RuntimeUserMemoryView.put`), `:2552-2560` (`RuntimeDocumentAppendView`). **Three mutate-in-place writers do NOT lock and must gain a lock as part of the flip-under-lock work** (directive §8 red-team #11): `document_views.py:366-380` (`DocumentDetailView.patch`), `:408-416` (`DocumentClearView.post`), `integrations/runtime_views.py:2145-2149` (`RuntimeDocumentView.post` update branch). Bulk plaintext reader is `apps/orchestrator/memory_sync.py:40-75` (`render_memory_files` → `f"# {doc.title}\n\n{doc.markdown}"` → `RedactionSession.redact` → share write); decrypt must happen there **before** redaction (directive §6). USER.md envelope readers: `apps/journal/envelope.py:85,126,290-304`. Deferred-field read site (must add `_enc` at read-flip): `document_views.py:481` `SidebarTreeView.get` uses `.values("kind","slug","title","updated_at")` — the only `.values()`/`.only()` naming `title`/`markdown`.

### 1.2 Lessons + insights + core (Phase 3 per directive §8)

| Store (db_table) | Column | Type | Holds | Tier | Verdict |
|---|---|---|---|---|---|
| Lesson (`lessons`) | `text` | TextField | The lesson | P | **NOW** (registered phase3 already) |
| Lesson | `galaxy_note` | TextField | Player's pinned note — verbatim (`views.py:628`, no redaction) | V | **NOW** (registered phase3) |
| Lesson | `context` | TextField | Extraction origin note | P? | **NOW** (register; has a predicate — §2.3) |
| Lesson | `cluster_label` | CharField(200) | Auto cluster name | S | **3b**/assess (predicate in `dedup_lessons.py:51`, test at `test_cluster_naming.py:111`) |
| TutoringSession (`tutoring_sessions`) | `messages` | JSONField | **Verbatim dialogue transcript, no redaction** | V | **NOW** (JSON — §1.5) |
| TutoringSession | `connections_made` | JSONField | `[{to_star_id, player_text}]` | V | **NOW** (JSON) |
| StarJournalEntry (`star_journal_entries`) | `text` | TextField | Star reflection, verbatim | V | **NOW** |
| AssistantInsight (`insights_assistant_insight`) | `statement` | TextField | AI-noticed pattern prose | V/P | **NOW** |
| AssistantInsight | `user_responses` | JSONField | User reply log (may hold prose) | V | **NOW**/assess (§1.5) |
| CoreProfile (`core_profiles`) | `additional_context` | TextField | Free-form meditation context | V | **NOW** |
| MeditationSession (`core_meditation_sessions`) | `feedback_note` | TextField | User's free-text note on a sit | V | **NOW** |
| MeditationSession | `title` | CharField(160) | AI title — placeholder-space (directive §5) | P | **NOW** (low priority) |
| MeditationSession | `theme`, `manifest`, `guidance_text` | Text/JSON | Generated narration script | P? | **NOW**/assess (§1.5) |
| TopicRegistry (`insights_topic_registry`) | `description` | TextField | Ops-authored taxonomy | S | OUT |
| PillarSnapshot (`insights_pillar_snapshot`) | `payload` | JSONField | Computed render mirror | S | OUT |

Anchors: Lesson search is pgvector cosine (`apps/lessons/services.py:144-158`, `nbhd_lesson_search`), **not** FTS — encrypting `Lesson.text` does not touch a WHERE/ORDER BY, only the post-fetch `.text` read. Value predicates on lessons columns: `agent_context.py:91` (`galaxy_note__gt=""`, allowlisted), `journal/extraction.py:235` (`text__icontains`, allowlisted), **new/unregistered:** `dedup_lessons.py:51` (`context__startswith="Goal"`).

### 1.3 Fuel free-text (`apps/fuel`) — NEW: not in the original directive

The directive predates the fuel surface being flagged; MJ named it explicitly on 07-14. Fuel splits cleanly into **free-text (ladder-compatible, NOW)** and **numeric body-metrics (needs a codec, DEFER — §1.4)**.

| Store (db_table) | Column | Type | Holds | Tier | Verdict |
|---|---|---|---|---|---|
| Workout (`fuel_workouts`) | `notes` | TextField | Session notes | V | **NOW** |
| Workout | `notes_thread` | JSONField | `[{at,who,text}]` conversation on the session | V | **NOW** (JSON — §1.5) |
| Workout | `detail_json` | JSONField | Exercises/sets/pace — **the bulk of "fuel data"** | V/S | **NOW** (JSON — §1.5) |
| Workout | `skip_reason` | CharField(128) | "traveling", "kid sick" — carries PII | V | **NOW** |
| Workout | `activity` | CharField(128) | Free-text activity name | V | **NOW** |
| WorkoutPlan (`fuel_workout_plans`) | `notes`, `objective` | Text/Char | Programming notes / one-line objective | V | **NOW** |
| WorkoutPlan | `name` | CharField(128) | Plan name | V | **NOW** |
| WorkoutPlan | `schedule_json`, `week_overrides` | JSONField | Structured week template | S | **NOW**/assess (§1.5) |
| FuelProfile (`fuel_profiles`) | `additional_context` | TextField | Free-form fitness context | V | **NOW** |
| FuelProfile | `limitations` | JSONField | `["right shoulder — rotator cuff"]` — health | V | **NOW** (JSON) |
| FuelProfile | `goals`, `equipment`, `preferred_time`, `fitness_level` | JSON/Char | Structured prefs | S | **NOW**/assess |
| WorkoutTemplate (`fuel_workout_templates`) | `name`, `detail_json` | Char/JSON | Reusable template | V/S | **NOW**/assess |
| SleepLog (`fuel_sleep`) | `notes` | TextField | "woke up twice" | V | **NOW** |
| PersonalRecord (`fuel_personal_records`) | `exercise_name` | CharField(128) | Exercise name | S | **DEFER** (low PII, structured) |
| FuelGoal (`fuel_goals`) | `exercise_name` | CharField(128) | Exercise name | S | **DEFER** |

No value predicate exists on any fuel free-text column (grep clean). SQL aggregation touches only `duration_minutes` (`services.py:748,762`, `views.py:632,990`) — a workout-duration integer, **not** a body-metric and **not** in scope; it stays plaintext.

### 1.4 Fuel numeric body-metrics — DEFER to a Phase 3b (recommended)

`RestingHeartRateLog.bpm`, `SleepLog.duration_hours`/`quality`, `BodyWeightLog.weight_kg`, `Workout.rpe`/`duration_seconds`. These are deeply personal but need work the text ladder does not provide:

- **A numeric codec.** `box.encrypt` takes a `str`; these are `DecimalField`/`IntegerField`. Encrypting means serialize→seal→bytea sidecar + null the numeric column. That is a *new codec surface* and a new sidecar shape.
- **Confirmed: they are NOT SQL-aggregated** (no `Avg`/`Sum` over `bpm`/`weight_kg`/`duration_hours`; trends are computed in Python after `.order_by("-date").first()` / `[:limit]` fetch — `fuel/envelope.py:110-114`, `views.py:687/1094/1185`). So encryption would *not* break existing analytics — but that is a fact to re-verify per reader before flipping, and it forecloses any future SQL trend query.
- **The higher-value, faster win is the free-text (§1.3).** Recommendation: ship free-text fuel in this phase; ship the numeric body-metrics as a small Phase-3b behind the same `encrypt_fuel_*` flag once the numeric codec + a per-reader aggregation audit are done. See Decision 2 (§8).

### 1.5 JSON columns — a real sub-decision

Several in-scope columns are `JSONField` (`notes_thread`, `detail_json`, `messages`, `limitations`, `evidence`, `accomplishments`…). `box.encrypt` seals a `str`, so the pattern is: `json.dumps(value)` → seal → bytea sidecar; read = decrypt → `json.loads`. This is clean **as long as no DB-level predicate reaches inside the JSON** (`__contains`, `->>`, key lookups). Grep is clean for the in-scope JSON columns today, but the plan's expand PR must register each JSON column in the CI guard and the guard must be extended to catch JSON key-path predicates (it currently only matches scalar lookups). Columns whose JSON is *structured render/config* with no names (`schedule_json`, `week_overrides`, `payload`) are lower value — assess individually; default them OUT unless they carry free text.

### 1.6 Explicitly OUT of Phase 3 (with reason)

- **`Tenant.pii_entity_map` / `pii_denylist`** — Phase 4, and **gated on the PII UX overhaul #1074 merging first** (directive §5, red-team #14: #1074 introduces DB-level map access the count sidecar doesn't cover). Keep it out; it is the hottest path (every inbound redact / outbound rehydrate / hourly arbiter) and must prove the cache on lower-stakes data first.
- **Friends cross-tenant content** — `FriendMessage.text`, `SharedGoal.title/description`, `SharedGoalUpdate.text`, `SharedGoalMembership.commitment`, `NeighborProfile.bio`, `Friendship.invite_note`, `SharedLesson.redacted_text/redacted_context`. **This is a genuine key-model mismatch, not an oversight** (§7.5): the per-tenant DEK assumes single-tenant ownership, but this content is read by *two or more* tenants. Encrypting `FriendMessage.text` under sender A's DEK makes it unreadable to recipient B. It needs its own design (shared per-friendship/per-circle key, or accept the existing NER-scrub floor). `SharedLesson.*` is already NER-scrubbed with no rehydration map (lower risk). The friends surface is dark except @mj/@kiho today, so live exposure is minimal. **OUT — own follow-up.**
- **`apps/agents`** — dead (migrations only, not in `INSTALLED_APPS`, zero non-migration refs). Superseded by `apps.router`. OUT.
- **`SautaiMealPlanJob.user_prompt`** (`apps/integrations`) — already placeholder-space at rest by design (`models.py:97` docstring). Low value; assess later, default OUT.
- **`AutomationRun.error_message`, `MeditationSession.error`, `SharedLesson.scrub_error`, `Session.references`** — system-generated diagnostics, not user content. OUT.
- **`apps/actions` `display_summary`** — system-templated; can echo a Gmail subject/calendar title. Borderline; DEFER/assess, default OUT for the 07-18 window.

---

## 2. The search fork — the plan's biggest decision

`Document.markdown` + `Document.title` are the crown-jewel verbatim columns **and** the only ones with a live search dependency:

- **`nbhd_journal_search`** — `apps/integrations/runtime_views.py:2165-2250` (`RuntimeJournalSearchView`): `SearchVector("title", weight="A") + SearchVector("markdown", weight="B")`, `SearchQuery(query, search_type="websearch")`, `SearchRank(...).order_by("-rank")`, Python substring snippets over `doc.markdown` (`_make_snippet`, :2214). No persisted index — **plaintext IS the index.**
- **Grounding probe** — `apps/orchestrator/grounding_probe.py:42-53` replicates the FTS query; `:85` does `Document.objects.filter(tenant=tenant, markdown__icontains=topic)` plus in-Python substring blobs (`:89,95`). This gate decides whether proactive messages are suppressed — a silent flip mis-grounds the assistant.

Encrypting `markdown`/`title` breaks both. The directive §4 answer is a **per-tenant keyed blind index** (`Document.search_blind tsvector` + GIN, HMAC-SHA256 lexemes under `HKDF(dek,"search-v1")`, `setweight` A/B, query rewriter for websearch phrase/negation, grounding-probe substring preservation over *decrypted candidates*, all-tenant shadow-diff cutover). That is correct and complete — but it is the single largest and riskiest piece here, and its whole red-team is about *silent wrong search results*. Rushing it into a 4-day window is the highest-risk move on the board.

**Three honest options (this is Decision 1 in §8):**

- **Option A — Defer the crown jewel + search to Phase 3b (RECOMMENDED).** Encrypt everything in the journal group *except* `Document.markdown`/`Document.title` now (Goal/Task/Purpose/PendingExtraction/DocumentChunk/Session/PendingTaskAction/DocumentIngestion*/DailyNote + lessons + insights + core). Those have **no search dependency** — pure encrypt-and-read. `Document.markdown`/`title` stay plaintext behind a **search-parity gate** and ship in Phase 3b together with the blind index, shadow-diffed to parity across all tenants + an adversarial corpus (directive §4). Cost: the highest-value column stays plaintext a few extra days — but honestly gated, and the machinery + all other content encrypt and soak on schedule. **Lowest risk, meets "plan + test by 07-18," no rushed silent-wrong-search.**

- **Option C — Encrypt `Document.markdown`/`title` now; interim "decrypt-and-scan" search.** Replace the SQL FTS with an in-process scan: fetch the tenant's Documents (bounded window), `box.decrypt` in-process (cache-warm, cheap), run the exact same websearch/substring/rank logic in Python. At 35 tenants with tiny volume this is feasible and **preserves search correctness with zero blind-index risk**; the blind index becomes a later *scale* optimization, not a 07-18 blocker. Cost: an N-decrypt-per-search hot path (watch latency; §7.1), more churn inside the cutover PR, and the grounding probe must move to decrypt-and-scan too. Gets the crown jewel encrypted on time **and** keeps search correct.

- **Option B — Encrypt now; search goes dark until the blind-index fast-follow.** Rejected: `nbhd_journal_search` is a live agent tool and grounding drives proactive messaging; shipping a knowingly-broken feature (even with an "unavailable" stub) degrades the product for days. Listed only to be dismissed.

**Recommendation: A** for the 07-18 window (lowest risk, cleanest test story), with **C as the fallback** if MJ wants `Document.markdown` encrypted by 07-18 and accepts the decrypt-hot-path. Do **not** attempt the full blind index inside the 4-day window.

---

## 3. The ladder (reuses Phase-2 exactly, per store-group)

### 3.1 Flags — two new per-tenant pairs on `Tenant` (recommended granularity)

Mirror `encrypt_chat_writes`/`read_encrypted_chat`. Recommend **one pair per store-group**, not per-column (16+ flags is unmanageable; columns within a group flip together) and not one global flag (couldn't roll journal back without fuel):

- `encrypt_journal_writes` / `read_encrypted_journal` — covers the journal group **+ lessons + insights + core** (they co-feed the USER.md envelope and `memory_sync`, and co-read as "the memory surface"; the directive groups them in one phase). The shared completeness predicate simply enumerates more `(model, value_field, enc_field)` tuples.
- `encrypt_fuel_writes` / `read_encrypted_fuel` — covers the fuel group (independent surface: `fuel/envelope.py`, `fuel/runtime_views.py`; separate rollback lever).

Sub-option, if independent rollback of lessons/insights is wanted: split them to a third pair. Default: keep them under the journal pair for a simpler rollout (Decision 3, §8).

### 3.2 PR ladder per store-group (each PR small + reversible except the final erase)

Identical shape to Phase-2 plan §5, run once per group:

- **PR-1 Expand** (`feat/enc-p3-journal-expand` / `…-fuel-expand`): add `<col>_enc BinaryField(null=True)` sidecars for every NOW column in the group; add the group's two `BooleanField(default=False)` flags to `Tenant`; **clone the latest `..._relock_after_*.py` tenants relock migration in the same PR** so `apps.tenants.test_public_schema_lockdown` stays green (any tenants-app migration triggers the RLS-relock gate — Phase-2 plan §5 PR-1). Define AAD `(table, column)` tuples **once** as constants (new `apps/journal/enc_columns.py`, `apps/fuel/enc_columns.py`, extend for lessons/insights/core) — never hand-typed (directive red-team #1; the one permanent-data-loss vector). Register every NOW column in `scripts/check_encrypted_column_predicates.py:ENCRYPTED_COLUMNS` as `"phase3"`. Accept: `makemigrations --check` clean, `ruff format` clean, `test_public_schema_lockdown` green, deploy touches no `_enc`.
- **PR-2 Dual-write** behind `encrypt_*_writes`: at every writer (insert + mutate-in-place), `enc = box.encrypt(tenant_id, TABLE, COL, value) if tenant.<flag> else None`, wrapped try/except (log-count-and-continue → `enc=None`), written in the **same** INSERT/UPDATE as the plaintext (ciphertext atomic with the row). `enc=None` stays readable via plaintext — availability preserved. **Add `select_for_update` to the three unlocked mutate-in-place Document writers** (`DocumentDetailView.patch`, `DocumentClearView.post`, `RuntimeDocumentView.post` update branch) so a concurrent append can't be lost between backfill-snapshot and flip (directive §8 red-team #11 — this is the flip-under-lock rule Phase-2 didn't need because chat is insert-once, but journal `markdown` is mutated). Reuse `apps/router/enc_read.py`'s helper shape for the read side later.
- **PR-3 Backfill** (`encrypt_journal_history` / `encrypt_fuel_history`): clone `apps/orchestrator/management/commands/encrypt_chat_history.py` verbatim — per-tenant RLS GUC isolation, `box.encrypt` **outside** any transaction (invariants #8; KV round trip), plain `.update(<col>_enc=…)` with the reconnect-and-re-set-RLS **retry-once** (`_update_with_retry`), idempotent (`_enc IS NULL` only), `""`→`b""`, zero-arg QStash-triggerable + `--tenant-id`/`--dry-run`/`--max`. For `DocumentChunk`: it is delete-and-recreate on re-embed (`embedding.py:84,97`) so backfill just seals the current rows; no flip-under-lock.
- **PR-4 Read-flip** behind `read_encrypted_*`: route each read through a group `enc_read`-style helper (prefer `_enc`, fall back to legacy; `box.decrypt` dual-reads either way). `.reveal()` at each egress seam (the RedactedStr CI guard flags un-revealed buffers into JSON/DRF). **Bulk-decrypt** the multi-row owner seams (`SidebarTreeView`, envelope builders, journal list views) via `box.decrypt_bulk(..., principal="owner_request")` — one audit event per page. System/cron reads (`memory_sync`, digest, grounding, embedding) stay silent (`principal="system"`). **Add `<col>_enc` to the one deferred-field site** `document_views.py:481` `.values(...)`. `memory_sync.render_memory_files` decrypts **before** `RedactionSession.redact` (directive §6). Do **not** add per-view `set_principal` — #1129 sets `owner_request` ambiently at the DRF auth boundary (Phase-2 plan §5 PR-4 amendment b).
- **Fleet rollout (ops, not a PR):** write-flag fleet → backfill fleet (QStash no-body fire, watch COUNTS) → read-flag a few tenants at a time via the converge task, §5 checks between. Verify serving-sha == origin/main after each deploy.
- **PR-6 Erase legacy plaintext (MJ-gated, IRREVERSIBLE), per group:** only after the group's fleet read-flag is on, soaked ≥ N days, zero unwrap-error spikes, the group completeness predicate returns 0 fleet-wide, and (journal group) **search parity proven** — writers stop populating plaintext, migration crypto-erases existing plaintext, later follow-up drops the columns. **Invert the PR-2 soft-fail to FAIL-CLOSED here** (Phase-2 plan §5 PR-6 amendment a): with no plaintext written, a `box.encrypt` failure must raise and abort, never persist a row with neither plaintext nor ciphertext.

### 3.3 Convergence + the shared completeness predicate

Generalize `apps/orchestrator/chat_encryption.py`. Either (a) extend `count_unsealed_chat_rows` into a parametrized `count_unsealed_rows(tenant, columns)` taking a group's `(model, value_field, enc_field)` tuples, or (b) add per-group siblings (`journal_encryption.py`, `fuel_encryption.py`) exporting `<GROUP>_ENC_COLUMNS` + `count_unsealed_<group>_rows`. Recommend (a) — one predicate, one place, no drift (the whole reason #1204 extracted it). Add `converge_unencrypted_journal_tenants` / `converge_unencrypted_fuel_tenants` cloned from `converge_unencrypted_chat_tenants.py` (flip-writes → backfill → verify-zero → flip-reads, idempotent, no-op once converged, ships dark).

### 3.4 The extended erase rule (state this plainly for MJ)

Two independent erase concepts must not be conflated:

1. **Per-group legacy-plaintext erase (PR-6-style).** Each group (chat / journal / fuel) has its *own* erase, gated on its *own* soak + completeness + (journal) search parity. **The 07-18 chat erase does NOT cover journal/fuel/lessons/insights** — those get erased later, each MJ-gated. So enabling Phase 3 encryption does not accelerate *or* block the chat erase.
2. **Per-tenant KEK purge (T4 crypto-shred, Phase 5).** This is orthogonal: purging a tenant's KEK makes *all* their ciphertext — chat + journal + fuel + lessons + insights — permanently unreadable at once, because it is all sealed under subkeys of that one tenant DEK. That is a feature (account deletion), unchanged by Phase 3 except that more columns now become unrecoverable on purge (state this in the deletion claim).

---

## 4. Provisioning (extend the #1204 verify-gated pattern)

`provision_tenant` (`apps/orchestrator/services.py`, the `2a3`/Phase-2 block) currently, after the fail-closed DEK mint: sets `encrypt_chat_writes=True` (persisted first), then flips `read_encrypted_chat` **only** if `count_unsealed_chat_rows(tenant)==0` (pre-provision plaintext chat rows are real — `492cb858`). Extend identically for each new group, right beside it:

```
# after mint_and_wrap_dek(tenant):
tenant.encrypt_journal_writes = True         # persist first
tenant.encrypt_fuel_writes = True
tenant.save(update_fields=[...])
if count_unsealed_rows(tenant, JOURNAL_ENC_COLUMNS) == 0:
    tenant.read_encrypted_journal = True     # verify-gated, loud-warn on non-zero
if count_unsealed_rows(tenant, FUEL_ENC_COLUMNS) == 0:
    tenant.read_encrypted_fuel = True
tenant.save(update_fields=[...])
```

Same rationale: a new tenant has ~zero journal/fuel history, but the chat endpoints proved that pre-provision rows *can* exist (messaging-while-spinning-up), so **never assume zero — verify**. A non-zero count leaves reads OFF (loud warn, tenant id + count, never content) and the converge task seals + flips it later. No sealing inside provision (a KV hiccup must not fail-close provisioning).

---

## 5. Test strategy

**No prod contact in this plan.** All ladder verification is unit + a canary-first drive on **MJ's canary tenant `148ccf1c` + the App-Review demo tenant `1c77c8c1` ONLY** (per the no-touch rule; fleet steps are MJ-triggered).

- **Unit (mock, `AZURE_MOCK=true` stateful key registry):** sealer round-trip for every NOW column incl. JSON (dumps→seal→decrypt→loads equality); dual-read fallback (legacy `str` verbatim, `b""`→`""`, unmarked-bytes fail-closed); backfill idempotency (`_enc IS NULL` only; rerun = "0 encrypted"); the generalized `count_unsealed_rows` predicate over multi-model tuples; the converge task ordering (write-flag→backfill→verify-zero→read-flag, read-gated-on-clean, no-op when converged, never touches already-converged); provision path asserts both group flags set with the verify-gate biting (a pre-provision plaintext row ⇒ writes=ON/reads=OFF, appears in converge candidates). Add a fixture helper that writes through the encrypt path and sweep test assertions that read legacy plaintext columns (directive §9).
- **Integration (canary + demo only):** run the compressed ladder end-to-end on `148ccf1c`, then `1c77c8c1`. Drive the **real flows**: iOS/console journal render shows exact text (no ciphertext/ghost bubbles); a runtime append writes `_enc` (probe `get_byte(_enc,0)=1`, `octet_length>=15` — never the content column); USER.md refresh → share shows verbatim-then-redacted memory (decrypt-before-redact); grounding probe old-vs-new diff; fuel: workout detail render, a `detail_json` write, the congrats cron. Log Analytics: `owner_request` audit on owner reads, silence on cron/digest.
- **Search (only if Option C is chosen):** the shadow-diff harness — write the interim decrypt-and-scan path alongside the SQL FTS, diff results across the canary's organic queries + a synthesized phrase/negation/stemming corpus, flip only on parity. Under Option A there is no search test this phase (Document.markdown stays plaintext); the shadow-diff lands with the 3b blind index.
- **Discipline:** every DB probe uses `get_byte`/`octet_length`/`count`/`IS NULL`, never the text column or a decrypted string; LA queries match audit *shape*, never content.

---

## 6. Timeline against 07-18 (the dependency graph MJ needs)

```
07-18 chat erase (Phase-2 PR-6) ──────────────►  DECISION: chat soak only.
        │  covers: AppChatMessage.user_text, ChatThread.title
        │  NOT blocked by Phase 3.  Should NOT slip for Phase 3.
        └── independent of everything below.

Phase 3, due-by-07-18 = PLAN (this doc) + CANARY-TESTED ladder:
  D0-D1  PR-1 expand (journal-group + fuel), CI-guard registrations, relock migration
  D1-D2  PR-2 dual-write (+ lock the 3 unlocked Document writers), PR-3 backfill
  D2-D3  PR-4 read-flip; unit suite green; canary+demo integration drive
  D3-D4  soak on canary+demo; metadata-only probes; provision verify-gate test
  ── by 07-18: journal-group (minus Document.markdown/title under Option A) + fuel
     free-text are PROVEN on canary+demo. This is what MJ asked for. ──

Decoupled, MJ-gated, AFTER 07-18 (NOT due 07-18):
  • Fleet rollout of journal/fuel groups (converge task, few-at-a-time)
  • Phase 3b: Document.markdown/title + blind index (Option A) OR the decrypt-scan
    cutover (Option C) — search parity shadow-diff first
  • Phase 3b: fuel numeric body-metrics (numeric codec)
  • Per-group legacy-plaintext erase (each its own soak gate)
  • Friends cross-tenant design; PII map (Phase 4, after #1074)
```

**The one thing MJ must internalize:** "encrypt the other data by 07-18" is satisfiable as *plan + canary-proven ladder* by 07-18; *fleet-complete + erased* is not, and does not need to be, because the P3 stores have their own later erase and the chat erase is independent. 07-18 does not have to slip.

---

## 7. Risks

1. **N+1 decrypt on hot paths.** Journal list/sidebar, USER.md envelope build (`memory_sync` decrypts every doc), and — under Option C — every search decrypt the candidate set. Mitigate with `box.decrypt_bulk` (one cache-warm DEK lookup per batch) and bounded windows. Watch canary latency on the envelope build and any search path.
2. **Payload size + latency.** Each seal adds ~30 bytes + a GCM op; JSON columns (`detail_json`, `messages`) can be large. Fine at 35-tenant volume; measure on canary.
3. **KV throughput on backfill.** One broker unwrap per cold `(tenant, epoch)`, then cache hits — the fleet backfill is ~one unwrap per tenant per group. The Phase-2 chat backfill already proved this shape; reuse `--max` for incremental fan-out.
4. **Embeddings residual (unchanged, disclosed).** `DocumentChunk.embedding` / `Lesson.embedding` / `Workspace.description_embedding` stay plaintext floats; the co-located `text` encrypts (`generate_embedding` decrypts in-process before the OpenAI call — `embedding.py:92`, `lessons/services.py:31`). Modern inversion can reconstruct chunk-sized text from vectors, so **no user claim mentions vector data** (directive §11 residual #1). KEK purge does not reach embeddings.
5. **Friends cross-tenant key mismatch (§1.6).** The per-tenant DEK cannot seal content two tenants must both read. Kept OUT with its own follow-up; do not let a "finish the sweep" impulse encrypt `FriendMessage.text` under one party's key and lock the other out.
6. **Legacy journal tables.** `DailyNote.markdown` is still live-written; `JournalEntry`/`WeeklyReview` v1 endpoints are still routed; `UserMemory` looks dead. Before the journal erase, run a **metadata-only** `count(*)` per table and either fold live ones into the ladder or retire the endpoints — do not erase a table whose only copy of real content is plaintext.
7. **PII-map interaction kept OUT.** Phase 4 (`pii_entity_map`/`pii_denylist`) is explicitly excluded and gated on #1074 (directive §5, red-team #14). Encrypting the journal content does not touch the map; live redaction reads in-flight text, not rows, so it is unaffected.
8. **CI-guard gaps to close in PR-1.** New unregistered predicate sites surface the moment their column is registered: `lifecycle_views.py:273,316` (`Task.title`/`Goal.title` `__icontains` — the Siri/Shortcuts `?q=` picker, a real search feature needing a rewrite, not a boolean sidecar), `migrate_documents_to_typed_models.py:124` (`Task.title` bare-equality dedup), `dedup_lessons.py:51` (`context__startswith`). Also extend the guard to catch **`ModelAdmin.search_fields`** against registered columns (staff admin search silently breaks otherwise — `insights/admin.py:31` `statement`, `core/admin.py:18`, `finance/admin.py:10`, etc.) and **JSON key-path predicates**.

---

## 8. Decisions MJ must make (each stated as either/or + recommendation)

**Decision 1 — the search fork (biggest).** For `Document.markdown`/`title` (crown-jewel, search-coupled):
- **(A) Defer them + search to Phase 3b** — encrypt the rest of the journal group now; `Document.markdown`/`title` + blind index ship next, shadow-diffed to parity. Crown jewel stays plaintext a few extra days, honestly gated. **← RECOMMENDED (lowest risk in the 4-day window).**
- **(C) Encrypt them now + interim decrypt-and-scan search** — crown jewel encrypted by 07-18, search stays correct via in-process scan, blind index becomes a later scale play. Cost: a decrypt-per-search hot path + more cutover-PR churn.
- (B) Encrypt now, search dark until fast-follow — **rejected** (live tool + proactive grounding go dark).

**Decision 2 — fuel numeric body-metrics.** `bpm` / `weight_kg` / sleep hours / `rpe`:
- **Defer to Phase 3b** (encrypt fuel *free-text* now; numerics need a numeric codec + a per-reader aggregation audit). **← RECOMMENDED.**
- Pull them into this phase (adds the numeric codec surface to the 4-day window).

**Decision 3 — confirm the framing (needs an explicit yes).** *The 07-18 chat erase stays fixed and Phase 3 is decoupled from it* — Phase 3's deliverable by 07-18 is the plan + a canary-proven ladder, with fleet rollout and the P3 erase MJ-gated afterward.
- **Yes — 07-18 does not slip; Phase 3 rolls out after.** **← RECOMMENDED.**
- No — hold the chat erase until Phase 3 is further along (couples two independent risks; not advised).

(Minor, your call, not blocking: whether lessons/insights/core share the journal flag pair — simpler — or get their own for independent rollback. Default: share.)

---

**Build order in one line:** stand up two per-group flag pairs + `_enc` sidecars + CI-guard registrations → dual-write (lock the 3 unlocked Document writers) → clone the chat backfill per group → read-flip through a bulk-aware dual-read helper (decrypt-before-redact in memory_sync) → verify-gated provisioning + generalized converge task → prove on canary+demo by 07-18 → fleet + per-group erase MJ-gated after → Document.markdown+search (blind index or decrypt-scan) and fuel numerics as Phase 3b → friends cross-tenant and the PII map (Phase 4, after #1074) each get their own design.

---

## Decision record (2026-07-14)

MJ's decisions on §8, stamped before PR-1 (`feat/enc-p3-expand`) was implemented:

1. **Decision 1 — search fork: Option A.** `Document.markdown` + `Document.title`
   are **EXCLUDED from this phase** — they ship in Phase 3b together with the
   blind index / search-parity work. PR-1 encrypts the rest of the journal group.
2. **Decision 2 — fuel numeric body-metrics: DEFER to Phase 3b.** `bpm`,
   `weight_kg`, sleep `duration_hours`/`quality`, `Workout.rpe`/`duration_seconds`
   stay plaintext this phase (they need the numeric codec + a per-reader
   aggregation audit). PR-1 covers the fuel **free-text** set only.
3. **Decision 3 / minor — flag grouping: SHARE.** lessons + insights + core
   flip under the journal flag pair (`encrypt_journal_writes` /
   `read_encrypted_journal`); fuel keeps its own pair. (The framing yes — 07-18
   chat erase stays fixed and Phase 3 is decoupled — is also accepted.)

PR-1 (`feat/enc-p3-expand`) implements exactly this scope: `_enc` sidecars for
every §1 verdict-NOW column across journal/lessons/insights/core/fuel-free-text
(EXCLUDING `Document.markdown`/`title` and the fuel numeric metrics), the two
flag pairs on `Tenant`, per-app `enc_columns.py` constants, and the CI-guard
registrations. No writer/reader touched; flags default False; deploy is a no-op.
