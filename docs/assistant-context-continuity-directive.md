# DIRECTIVE: Assistant context continuity — say it once, hear it once, ingest it once

**Status:** Design complete, ready to implement — **second-pass, re-anchored to `origin/main` @ `decd9b61`.** Two critic passes fed this version. **Critic pass 1** (self-run) found two code-verified issues that survive re-anchoring and are folded in as gates: the conversation digest renders raw iOS `user_text` (→ reverse-map PII scrub, D3, BLOCKER) and `RuntimeUserMemoryView.put` is an unlocked full-document overwrite (→ Phase 2 clobber prereq). **Critic pass 2** (opus) caught the decisive error: the first draft was written against a **stale primary checkout** (`12c17091`, 63 commits behind), and its P1 mechanism extended a fire-time Django→container "grounding" wire that commit **`a3c4bc6a`** ("refactor(cron): rebuild nbhd-cron-enforcement in-container, drop fire-time Django wire", PR #1117, merged 2026-07-11 02:00 JST) **deleted** — that commit records the wire *never actually ran in prod* (a wiring bug) and its in-container replacement is deliberately **dark**. P1 is redesigned within the new architecture (D1). Every symbol below was re-resolved via `git show origin/main:<path>`; the deleted grounding symbols (`RuntimeCronGroundingView`, `CRON_GROUNDING_RULE`, `fetch_cron_pattern_context`, the plugin's `before_prompt_build`) are confirmed absent on origin/main and appear here only as rejected options. Extend the document-keeping directive, do not re-derive it.

**Anchor convention (read this first):** cited evidence is `file:symbol` at `origin/main` HEAD `decd9b61` (which already contains `a3c4bc6a`). Line hints drift. **Resolve every anchor by SYMBOL, via `git show origin/main:<path>` — never the checked-out tree.** The checked-out tree may lag origin/main by dozens of commits; that is exactly the trap the first draft hit.

**One-line summary:** Everything the assistant **says, hears, or ingests** should have exactly one durable, replayable home, and every context that *acts* — a cron, the heartbeat, an inbound reply — should read those homes fresh when it acts. Three gaps break that: (P1) proactive isolated sessions ground on the `## Conversation so far` USER.md digest, but **iOS turns never refresh that file** (the drain that captures Telegram/LINE turns and pushes USER.md is skipped for iOS), so an isolated morning cron reads an hours-stale digest — the verified root cause of the golden case; (P2) a fact the user states in chat lands only in a wipeable transcript, never a typed home; (P3) nothing records what the assistant read from an email/web page/Reddit, what it concluded, or whether it may act on it. North-star failure to kill: **the assistant asks a question, then has no idea what the user is talking about when they follow up.**

**Changes from the second-pass re-anchor (critic pass 2), summarized:**
- **P1's mechanism is replaced (was: fire-time Django grounding wire; deleted in `a3c4bc6a`).** The new, cheaper, architecture-respecting P1: **fix the iOS freshness gap** so the existing USER.md conversation digest is pushed event-driven on iOS drain completion (as Telegram/LINE already are), and **add recent `ProactiveOutbound` to that digest** for cron-to-cron dedup. Django-only, no plugin, **no image rebuild, no fire-time control-plane call** — it respects `a3c4bc6a`'s core decision to keep the send path free of synchronous Django dependencies.
- **The biggest product hole is now front-and-center (D4).** MJ runs `experimental_built_in_heartbeat=True` (2 of 33 tenants). Under that flag `_build_heartbeat_cron` returns `None` (no cron → nothing to ground) and the OpenClaw-native heartbeat carries **no Django-authored prompt** and sets `lightContext:True`, which likely trims the USER.md envelope. So **no context-file or cron mechanism reaches MJ's heartbeat — the exact sender that produced the "Jasmine" question.** Resolved as an explicit MJ decision (§7 Q1): flip MJ to the cron-based heartbeat, or accept the golden case is scoped to cron senders only.
- **D5/D6 rescoped: consumption ≠ answered.** `surface_proactive_context` marks `consumed_at` on *any* inbound surfacing ("good morning" consumes the budget question). Any linkage field is renamed `linked_at` with honest "surfaced-alongside-a-reply" semantics, must NOT be read as "answered," and must not break PR A's 7-day resurface (which keys off consumption).
- **P5 is now unblocked:** `DocumentIngestion`/`DocumentIngestionArtifact`/`REMOVAL_HANDLERS`/`forget_ingestion` **exist** at origin/main (`apps/journal/models.py`, `apps/journal/document_ingestion.py`; landed via #1091) — the first draft's "does not exist yet" caveat was a stale-tree artifact. P5's provenance stamp reuses the live ledger.
- **PR C no longer stakes on P1 (critic finding 9).** PR C's standalone justification stands: **no live conversation reads OpenClaw session `"main"`** (iOS=`thread:<id>`, Telegram=`chat_id`, LINE=`line_user_id`, heartbeat/crons=`isolated`), so the `_sync:`/`_phase2` mechanism writes to a dead namespace today; its retirement is independent of P1.
- **Plugin-cost honesty added** wherever a plugin edit is proposed; **D3 softened** ("first-party-authored post-redaction; reply text may *paraphrase* third-party content"); **Phase 3 PII-binding guards added** (same-name-fusion refusal, no-egress-to-friends, propose-then-confirm); **PR B overlap boundary specified**.

---

## 1. Product behavior (the user-visible contract)

### 1.1 The three promises
1. **The assistant knows what it just said.** A morning pulse, heartbeat, or any scheduled message is grounded in the *actual recent conversation* — not a stale framing of it — and two crons firing the same morning do not ask the same question, because each can see what the other already sent.
2. **The assistant keeps what it hears.** A durable fact the user states about a person/relationship in chat is recorded to a typed home *that turn*, while fresh — not only if a delayed nightly pass catches it.
3. **The assistant is honest about what it read, and asks before acting on it.** Reading an email/calendar/web page records where the information came from, and any durable action derived from it is proposed, not silently written.

### 1.2 The golden case, with its now-verified root cause
Diagnosed 2026-06-03: in-session the assistant asked "How did the budget meeting go?" and got a substantive answer; the next morning's isolated pulse reverted to week-old framing and re-asked. **Root cause, confirmed in code:** the isolated session reads the conversation via the `## Conversation so far` USER.md section (`apps/journal/envelope.py`; rendered by `build_conversation_digest`, `apps/router/conversation_capture.py`). That section is refreshed by a debounced `push_user_md` fired from `record_conversation_turn` — which is called from `_capture_conversation_turn` at the **Telegram and LINE** drains only (`apps/router/pending_queue.py`) and **never from `_drain_ios_batch`**. MJ chats on iOS, so his USER.md digest is only refreshed by the hourly fleet sweep (`refresh_user_md_fleet_task`, `apps/orchestrator/tasks.py`) — up to an hour stale (or staler) when the pulse fires. The digest *content* is available (it reads `AppChatMessage` directly); the *file the isolated session reads* is stale.

### 1.3 What the assistant must never do
- Never fire a proactive turn that ignores a same-window conversation; if recent turns exist, the day was not quiet.
- Never ask, in a cron, a question a sibling cron already asked in the window.
- Never let a stated person-fact evaporate because the reconcile gate only watched goals/tasks/finance/fuel.
- Never take a durable action off an ingested email/web page without proposing first (D8).
- Never re-introduce a raw third-party name into a cron/proactive context or a log line; the digest is scrubbed to placeholder-space before it is written to the share (D3).

---

## 2. Architecture decisions

Each states the choice, a short WHY, the code evidence, and the resolved disagreement.

### D1 — P1: keep a FRESH conversation+proactive digest on the workspace share (event-driven on drain), read by isolated sessions as bootstrap context. Do NOT restore the fire-time Django wire; do NOT re-light the dark plugin's prompt hook

**Decision.** Two Django-only changes: (1) **close the iOS freshness gap** — on iOS drain completion (`_drain_ios_batch`, `apps/router/pending_queue.py`), schedule the same debounced `push_user_md` that Telegram/LINE already get, so the `## Conversation so far` section is refreshed on every iOS turn; (2) **render recent `ProactiveOutbound` into that digest** (`build_conversation_digest`) so the isolated session also sees what other proactive routines already asked (D2). The write goes through the sanitize chokepoint (`push_user_md` → `upload_workspace_file` → `_put_share_file` → `sanitize_share_text`); OpenClaw auto-loads USER.md into every non-`lightContext` agent turn, so isolated crons read it with no fire-time call.

**WHY / disagreement resolved.** The data is already fresh in Postgres and the digest already renders it; the golden case is a pure *delivery-freshness* bug isolated to iOS (verified §1.2). Fixing the missing push is a targeted one-surface change, not "push USER.md harder" blindly. Critically, it stays entirely on the **inbound drain path**, never the cron send path — honoring `a3c4bc6a`'s central decision that scheduled sends must not depend synchronously on Django.

**Rejected alternatives (with reasons):**
- **Restore a fire-time Django grounding wire** (the first draft's mechanism — `RuntimeCronGroundingView` + the plugin's `before_prompt_build`/`appendSystemContext`). **Deleted in `a3c4bc6a`, deliberately, and it never ran in prod.** That commit's rationale is explicit: a synchronous control-plane dependency on the per-send critical path that "validated format, not truth, and failed open." Do not re-propose it.
- **Re-light the now-dark plugin and add a `before_prompt_build` that reads a local digest file and force-injects it.** This is the only route that could bypass a `lightContext` trim (D4), but it is a plugin edit = **OpenClaw image rebuild + 33-container fleet roll + hibernated tenants only at wake** (the PR #937 class), and it re-activates a hook and a plugin `a3c4bc6a` intentionally left dark. Hold as a narrow escalation for the built-in-heartbeat case only (D4), never the primary.
- **A runtime tool the isolated session must call.** Violates "backend computes evidence, LLM judges" (`memory: feedback_llm_not_formula_for_judgment`); an isolated cheap-model cron won't reliably call it.

**Honest cost.** The USER.md file is a snapshot, so freshness is bounded by the debounce, not zero. For the golden case (evening chat → morning cron) that is sufficient: the evening iOS turn now pushes the digest, and the morning cron reads it. The residual staleness is a debounce window, tunable (§7 Q2), not an hour.

### D2 — Cron-to-cron dedup rides the same digest: render recent `ProactiveOutbound`; no new schema

**Decision.** `build_conversation_digest` (or a sibling block in the same section) also renders the last few `ProactiveOutbound` rows in the window — read from `apps/router/models.py::ProactiveOutbound`, bounded (reuse `proactive_context.py::DEFAULT_WINDOW_HOURS=24`, `DEFAULT_LIMIT=3`), placeholder-space.

**WHY.** `ProactiveOutbound` is written on every proactive push but read only on the *inbound* reply path (PR #1132's forward bridge), never cron-to-cron — so two crons each speak into apparent silence and repeat each other. Rendering "what you/a sibling routine already sent in the last 24h" into the same digest the isolated session already reads closes that with zero new schema.

### D3 — PII: the digest is scrubbed to placeholder-space before it hits the share (posture confirmed sound; injection-surface claim softened)

**Decision.** The digest is rendered in placeholder-space and written to the share that way. Reply lines are already placeholder-space at rest (`clean_reply_for_capture` / `_store_ios_turn_reply`), and the one raw field — iOS `AppChatMessage.user_text`, the user's *own* typed words — the `?since=` feed echoes verbatim by design (`conversation_capture.py` closing docstring: "pseudonymization can't cover a user's own words"). Where the digest surfaces that raw field, reverse-map known real names to placeholders against `tenant.pii_entity_map` (a pure dict pass, inverse of `rehydrate_for_tenant`) so no *third-party* real name reaches the model via this path.

**Softened claim (critic finding 8).** Do not assert "the digest carries no attacker-controllable text." Reply lines can *paraphrase* third-party content the assistant read (an email, a web page), so the digest is a **diluted** D8 surface, not a clean first-party one. Far weaker than a raw ingested document, but the wording must not overclaim.

### D4 — The built-in-heartbeat hole: MJ's heartbeat is unreachable by ANY grounding mechanism; resolve it as a product decision, don't paper over it

**Decision.** State plainly: for a tenant on `experimental_built_in_heartbeat=True` (MJ + one other), P1 does **not** reach the heartbeat, and neither would any cron-hook or USER.md mechanism. Resolve by **recommending MJ flip to the cron-based heartbeat** (§7 Q1) so the fresh digest reaches it; if MJ keeps the built-in heartbeat, the golden case is honestly **scoped to cron senders** (Morning Briefing, Evening Check-in, and the cron-based heartbeat), which P1 *does* reach.

**WHY / evidence.** Under the flag, `_build_heartbeat_cron` returns `None` (`apps/orchestrator/config_generator.py` — "Returns None … if the tenant is on the experimental built-in heartbeat path"), so there is no Django cron to ground. The OpenClaw-native heartbeat it enables (`_build_heartbeat_defaults`, same file) carries **no Django-authored prompt** (only scheduling knobs) and sets `lightContext:True`. `lightContext` is the runtime's "lowest-cost agent turn" (`apps/cron/patterns/pure_reminder.py` docstring) and is **not documented in-repo** as to exactly what it trims — so whether it strips the USER.md bootstrap envelope must be verified in the OpenClaw runtime; but the heartbeat is out of grounding reach *regardless*, because it has no prompt surface Django can shape. The cron-based heartbeat (`_build_heartbeat_cron` when the flag is off) is a normal isolated cron that loads full USER.md and carries `_HEARTBEAT_CHECKIN_PROMPT` — so flipping MJ makes P1 reach the exact golden-case sender. Tradeoff: MJ loses the built-in heartbeat's inferred-commitments delivery (`_build_commitments_config`, `maxPerDay:3`) — a genuine product call, hence Q1.

### D5 — P2 capture-while-fresh: in-turn reflex, typed home in the PII binding; do NOT rely on nightly extraction, do NOT build a new Person table

**Decision.** Person/relationship facts captured **in the turn stated**, two destinations cheapest-first: (1) the agent-writable long-term memory Document (`RuntimeUserMemoryView`, `apps/integrations/runtime_views.py`; tool `nbhd_memory_update`) under a structured `## People & Context` section — today a convention (`config_generator.py` weekly prompt), promoted to an AGENTS.md reflex; (2) a runtime upsert of `relationship`/`notes` onto the person's existing `pii_entity_map` binding (`apps/pii/entity_registry.py`; `{name, relationship, notes}`), mirroring the human console path (`EntityRegistryItemView`, `apps/tenants/views.py`).

**WHY / disagreement resolved.** Nightly extraction (`apps/journal/extraction.py::run_extraction_for_tenant`) has **no person category** and is delayed + pending-approval — the opposite of capture-while-fresh; it stays the safety net. A `person` block in reconcile-scan (`RuntimeReconcileScanView`) has no typed rows to reconcile against until destination #2 exists; deferred. `apps/friends/` is NBHD-to-NBHD social, not real-world people; a new `Person` table is a large migration whose only consumer is this reflex. The binding already carries `{name, relationship, notes}`, surfaces read-only to the agent via USER.md's Identity context (`apps/tenants/envelope.py`), and **feeds the P1 digest loop** — capture it once, the next isolated session reads it.

### D6 — Linkage: `linked_at`, not "answered" (consumption ≠ answered)

**Decision.** If a linkage stamp is added to `ProactiveOutbound`, name it `linked_at` (or `surfaced_with_reply_at`) with honest semantics: "a reply arrived while this row was being surfaced," NOT "this question was answered." Set it deterministically where `surface_proactive_context` marks `consumed_at`. It must not gate PR A's 7-day resurface (which keys off `consumed_at`) — a `linked_at` stamp is metadata, not a resurface killer.

**WHY.** `surface_proactive_context` (`apps/router/proactive_context.py`) marks `consumed_at` for every surfaced row on *any* inbound — "good morning" consumes the budget question. Treating that as "answered" is dishonest and would silently mark unanswered questions resolved. v1 may ship **without** any stamp (the digest already replays recent outbounds); add `linked_at` only if the assistant needs to reference a specific prior question, and never overstate it.

### D7 — P3 ingestion provenance: reuse the LIVE document-keeping ledger (it landed); stamp derived writes; principles for the rest

**Decision.** `DocumentIngestion`/`DocumentIngestionArtifact`/`REMOVAL_HANDLERS`/`forget_ingestion` exist now (`apps/journal/models.py:625`, `apps/journal/document_ingestion.py`). When the assistant makes a durable write *derived from* an ingested source, file the same validated manifest with a `source_kind` (`email`/`calendar`/`reddit`) and source identity in place of `original_filename`: Gmail RFC message-id, calendar event-id, Reddit fullname (`t3_`/`t1_`). All three proxied reads already pass `redact_tool_response` (`apps/integrations/runtime_views.py` — Gmail/Calendar/Reddit). **Web is principles-only** — it never transits Django (runtime fetches it directly), so there is no server-side seam to stamp.

### D8 — Ingested content is a prompt-injection surface (mirrors the document directive's D8)

**Decision.** Treat email/calendar/web/Reddit content the assistant reads as attacker-controllable text. A durable write derived from a just-ingested source follows the same propose-then-write discipline the document directive imposes on uploads (behavioral gate in `templates/openclaw/AGENTS.md`). A deterministic same-turn write backstop is deferred (pull-based reads happen on many benign turns; a blanket block is too broad) and escalated only on eval failure.

---

## 3. Implementation phases

Cheap-first; **plugin/image edits are called out with their cost** (image rebuild + 33-container fleet roll + hibernated tenants only at wake). iOS work is zero through Phase 4.

### Phase 1 — P1 digest freshness (Django-only; no plugin, no image, no iOS)
The highest-value, lowest-risk, architecture-respecting phase.
- **Fix the iOS gap:** in `_drain_ios_batch` (`apps/router/pending_queue.py`), after the reply is stored, schedule a debounced `push_user_md` (mirror `_capture_conversation_turn`'s trigger; iOS needn't write a `ConversationTurn` since `build_conversation_digest` reads `AppChatMessage` directly — only the *push* is missing). Fail-open, off the delivery path.
- **Add `ProactiveOutbound` to the digest** (D2), bounded + placeholder-space; reverse-map scrub over raw `user_text` (D3).
- **Tests (deterministic):** (a) an iOS drain triggers exactly one debounced `push_user_md` (the missing-push regression); (b) the digest renders recent `ProactiveOutbound`, bounded, `""` when quiet; (c) PII scrub — a real name in raw `user_text` renders as its placeholder.
- **Verification:** on MJ's tenant (on the **cron-based** heartbeat per Q1), chat on iOS in the evening, then fire the morning cron and read the **outbound** — it grounds on the fresh answer. Confirm two same-window crons don't duplicate. Never dump the raw digest to logs (telemetry carries cron name + char count + hash only).

### Phase 2 — P2 capture reflex (behavioral, canary via `prompt_extras`; no schema, no iOS)
- **AGENTS.md reconcile gate** (`templates/openclaw/AGENTS.md`): add a person/relationship arm routing durable facts to `nbhd_memory_update`'s `## People & Context` section, delivered canary-only via `set_prompt_extras agents_md` (the per-tenant lever; `render_workspace_rules` has no tenant arg).
- **Close the memory-write clobber first:** `nbhd_memory_update` → `RuntimeUserMemoryView.put` is a full-document overwrite with no `select_for_update`, and a compaction `memoryFlush` writer already races it. Lock the put (mirror `EntityRegistryItemView`) or route through a section/append primitive **before** raising write frequency.
- **Tests:** directive-render scoping test; concurrency test (a person-fact write + a memory flush both survive).

### Phase 3 — P2 typed home (runtime upsert onto the PII binding; flag-gated tool) — WITH guards
- **`nbhd_person_note`** → `POST runtime/<tenant_id>/people/<placeholder>/note/` (`_internal_auth_or_401`, `set_rls_context(service_role=True)`), upsert `relationship`/`notes` onto an existing binding under `select_for_update` (the map is a shared JSON blob).
- **Same-name-fusion refusal (critic finding 7).** `inverted_names`/`inverted_names_ci` (`apps/pii/entity_registry.py`) collapse same-named people to one placeholder — two different "Alex"es share `[PERSON_n]`. The endpoint MUST refuse (or route to propose-then-confirm) when the target placeholder is fused/ambiguous, so a note never attaches to the wrong person (`memory: project_pii_same_name_fusion_assessment`).
- **Consent + no-egress.** The binding is human-write-only today (console `EntityRegistryItemView`); an agent-written note is an agent judgment, so gate it **propose-then-confirm**. State explicitly that person-notes are **local to the owner** and never egress to shared/friends surfaces (`apps/friends/`); the friends bootstrap is not egress-redacted, so this must be a hard boundary, not an assumption.
- **Not-found contract:** a NER-missed name has no binding → structured `not_found`, never mint from the runtime; the agent falls back to destination #1 (the memory section).
- **Loop verification:** capture a relationship, fire a cron, confirm it appears in the isolated session's USER.md.

### Phase 4 — P2 linkage (`linked_at`; backend-only, optional)
Add `linked_at` to `ProactiveOutbound` only if needed (D6); stamp at `surface_proactive_context`'s consumption; honest semantics; do not break PR A resurfacing. Test: a "good morning" that consumes a question stamps `linked_at` but the row is NOT treated as answered.

### Phase 5 — P3 provenance (reuse the LIVE ledger + action gate; backend + AGENTS.md)
Now unblocked (ledger landed). Stamp durable writes derived from Gmail/Calendar/Reddit with `source_kind` + source identity, forgettable via the existing `forget_ingestion`. Ship the AGENTS.md action gate (D7/D8) — which needs no schema — independently and early. Web is principles-only.

---

## 4. Enforcement + measurement

**Telemetry (house `key=value`; Log Analytics `035a49db-1da5-452d-8b32-b074d7a5d606`; never log names/bodies — `pii_mint` rule):**
- `digest_pushed tenant=%s channel=%s turns=%d proactives=%d chars=%d` — from the drain-triggered refresh (P1 reach + size; proves the iOS push now fires).
- `digest_stale_detected tenant=%s age_s=%d` — optional guard: an isolated cron whose USER.md digest is older than a threshold (surfaces any residual staleness).
- `person_fact_captured tenant=%s dest=%s` — `dest=memory|binding` (P2 volume; no fact text).
- `person_note_refused tenant=%s reason=fused|ambiguous` — Phase 3 fusion guard fired.
- `proactive_linked tenant=%s job=%s` — Phase 4 stamp (no "answered" claim).
- `ingest_provenance_stamped tenant=%s source_kind=%s` / `ingest_write_blocked tenant=%s source_kind=%s` — Phase 5.

**Eval plan:**
- **Deterministic CI:** the iOS-drain-pushes-USER.md regression, the digest bounds/scrub tests, the memory-clobber concurrency test, the fusion-refusal test.
- **Golden-case regression (P1's target):** evening iOS answer → morning cron grounds on it (run on MJ once he's on the cron heartbeat).
- **Offline LLM-judge over sampled threads** ("backend computes evidence, LLM judges"): proactive fires that followed a same-window conversation → grounded vs stale, and no sibling-cron repeat; chat threads stating a relationship fact → captured that turn / typed home / not over-capturing.
- **Injection eval (D8):** an email instructing a durable write → propose, don't write.

**Escalation:** if P1's file route proves too stale in practice (`digest_stale_detected` nonzero on active tenants), tighten the debounce before considering the plugin re-inject route (D1 escalation, image cost). If MJ keeps the built-in heartbeat and its silence matters, that is the trigger to evaluate the plugin re-inject — an explicit, costed decision, not a default.

---

## 5. Semantics detail
- **Says** → `ProactiveOutbound` becomes replayable to isolated senders via the digest (D2); optionally `linked_at` (D6).
- **Hears** → durable person-facts → `pii_entity_map` binding `relationship`/`notes` (D5/Phase 3), memory `## People & Context` as the day-one home + fallback.
- **Ingests** → email/calendar/reddit-derived durable writes → live `DocumentIngestion` manifest (D7/Phase 5); web principles-only.
- **PII:** digest scrubbed to placeholder-space before the share write (D3); person-notes local-only, fusion-guarded (Phase 3); forget never prunes `pii_entity_map` (document directive D7 carries over).
- **PR B overlap boundary (critic finding 10):** PR B (cold-session thread rehydration at the iOS drain) owns **since-last-delivered-turn, thread-scoped** history for the live chat session; the USER.md digest owns the **terse cross-channel recent summary** for isolated proactive sessions. They target different session types (chat vs isolated cron), so no double-injection into one session; keeping the digest terse (existing bounds) keeps any incidental overlap harmless.

---

## 6. Explicitly out of scope / deferred
- **Restoring the fire-time Django grounding wire** — deleted in `a3c4bc6a`, never ran, deliberately dropped. Do NOT re-propose.
- **Re-lighting the dark `nbhd-cron-enforcement` plugin / adding a prompt-inject hook** — narrow escalation only (D1/D4), with full image-roll cost.
- **A general "record every external read" audit log** — P5 stamps derived writes only.
- **Web-content server-side provenance** — no Django touchpoint.
- **A new `Person`/`Contact` model; a `person` block in reconcile-scan** — reuse the binding (D5).
- **A deterministic same-turn ingestion write backstop** — behavioral gate + eval first (D8).
- **"Answered" semantics on `ProactiveOutbound`** — consumption ≠ answered (D6).
- **Telegram/LINE-specific proactive surfaces** — iOS is the only future channel; the digest reads `ConversationTurn` only because `build_conversation_digest` already does.

---

## 7. Open product questions for MJ (genuine decisions only)

1. **The built-in-heartbeat flip — the headline decision (D4).** MJ runs `experimental_built_in_heartbeat=True`, so P1 cannot reach his heartbeat (no Django prompt, `lightContext`, no cron). **Flip MJ (and the other built-in-heartbeat tenant) to the cron-based heartbeat** so the fresh digest reaches the exact golden-case sender — at the cost of losing the built-in heartbeat's inferred-commitments delivery (`maxPerDay:3`)? Or keep the built-in heartbeat and accept the golden case is scoped to cron senders (Morning Briefing / Evening Check-in / cron heartbeat)? **Recommendation:** flip to the cron heartbeat — it is the sender that produced the Jasmine question, and commitments are a lesser feature than "the assistant remembers the conversation." Your call.
2. **Digest freshness debounce.** The iOS push is debounced (leading-edge). Tighter = fresher but more share writes. **Recommendation:** reuse the existing `_REFRESH_DEBOUNCE_SECONDS` (already tuned for Telegram/LINE) — no new knob.
3. **Person-fact capture aggressiveness.** Capture every relationship mention, or only clearly durable facts and let venting pass? **Recommendation:** conservative (durable facts only), measured by eval — over-capture pollutes the typed home and the P1 digest.
4. **Phase 3 typed tool now, or prove Phase 2 behavioral first?** **Recommendation:** Phase 2 first; ship Phase 3 only if capture compliance is poor — with the fusion guard non-negotiable when it does ship.
5. **`linked_at` at all?** The digest already replays recent outbounds; a stamp is only for citing a specific prior question. **Recommendation:** skip it for v1; add later with honest semantics if needed.

**Implementer must-verify (engineering, not product):**
- **Everything resolves against `origin/main` via `git show`, never the checked-out tree** — the first draft's core error. The primary checkout may lag origin/main by dozens of commits.
- **`lightContext` behavior** — verify in the OpenClaw runtime whether it trims the USER.md bootstrap envelope before relying on the file route for *any* `lightContext` turn; the built-in heartbeat is out of grounding reach regardless (no prompt surface).
- **The memory-write clobber fix** is in place before Phase 2 raises write frequency.

**Key files (absolute paths; symbols at `origin/main` `decd9b61` — resolve by symbol via `git show`):**
- `/Users/michaeljones/Projects/nbhd-united/apps/router/pending_queue.py` — `_drain_ios_batch` (add the push), `_capture_conversation_turn` (the Telegram/LINE trigger to mirror).
- `/Users/michaeljones/Projects/nbhd-united/apps/router/conversation_capture.py` — `build_conversation_digest`, `record_conversation_turn` (fires `push_user_md`), `_collect_turns`, `_REFRESH_DEBOUNCE_SECONDS`.
- `/Users/michaeljones/Projects/nbhd-united/apps/orchestrator/workspace_envelope.py` — `push_user_md`, `push_user_md_in_background` (the debounced share write via the sanitize chokepoint).
- `/Users/michaeljones/Projects/nbhd-united/apps/router/models.py` — `ProactiveOutbound` (digest source; optional `linked_at`), `AppChatMessage`, `ConversationTurn`.
- `/Users/michaeljones/Projects/nbhd-united/apps/router/proactive_context.py` — `surface_proactive_context` (`consumed_at` = consumption, NOT answered), window/limit constants.
- `/Users/michaeljones/Projects/nbhd-united/apps/orchestrator/config_generator.py` — `_build_heartbeat_defaults` (`lightContext:True`, no prompt), `_build_heartbeat_cron` (returns None under the built-in flag), `_build_commitments_config`.
- `/Users/michaeljones/Projects/nbhd-united/apps/integrations/runtime_views.py` — `RuntimeUserMemoryView` (clobber fix), `RuntimeReconcileScanView` (no person block), `_internal_auth_or_401`, `redact_tool_response` sites (Gmail/Calendar/Reddit).
- `/Users/michaeljones/Projects/nbhd-united/apps/pii/entity_registry.py` — binding `{name, relationship, notes}`, `inverted_names`/`inverted_names_ci` (fusion guard) · `/Users/michaeljones/Projects/nbhd-united/apps/tenants/views.py` — `EntityRegistryItemView` (human write path + `select_for_update` to mirror) · `/Users/michaeljones/Projects/nbhd-united/apps/tenants/envelope.py` — Identity-context read seam.
- `/Users/michaeljones/Projects/nbhd-united/apps/journal/models.py` — `DocumentIngestion`/`DocumentIngestionArtifact` (landed) · `/Users/michaeljones/Projects/nbhd-united/apps/journal/document_ingestion.py` — `REMOVAL_HANDLERS`, `forget_ingestion` (P5 reuse).
- `/Users/michaeljones/Projects/nbhd-united/runtime/openclaw/plugins/nbhd-cron-enforcement/index.js` — post-`a3c4bc6a`: hooks are `cron_changed` + `before_tool_call` on `nbhd_send_to_user` (outbound validation), plugin DARK, NO prompt-inject seam. Any edit = image rebuild + fleet roll.
- `/Users/michaeljones/Projects/nbhd-united/templates/openclaw/AGENTS.md` — reconcile gate (person arm, P2), action gate (D7/D8, P5).
- `/Users/michaeljones/Projects/nbhd-united/docs/document-information-keeping-directive.md` — the contract + live ledger P5 reuses.
- Commit `a3c4bc6a` — read in full before touching cron/plugin surfaces; it is the architecture P1 must live within.
