# PII Redaction & LLM Data Egress

Audit of what personal data actually reaches third-party LLM providers, what
stays behind, and what remains in plaintext at rest. Builds on
[`../reference/platform-services.md`](../reference/platform-services.md) §2
(the mechanical pipeline — detection stack, mint gating, seams table) and
[`../pii-redaction-security.md`](../pii-redaction-security.md) (the original
design doc) rather than repeating them; this doc verifies both against
current `main`, flags where they've drifted, and covers ground neither one
does: which egress paths bypass redaction entirely, what Phase 0 of
[`../encryption-at-rest-directive.md`](../encryption-at-rest-directive.md)
actually shipped vs. its own stated "nothing implemented," and one
unredacted write path found during this pass. See also
[`../pii-self-cleaning.md`](../pii-self-cleaning.md) (the local hygiene +
arbiter-retirement design) and
[`../ios-chat-redaction-transparency-directive.md`](../ios-chat-redaction-transparency-directive.md)
(owner-facing UX for what was hidden).

Audience: privacy/security auditor. The model-egress claims below describe the
post-fix state assembled on 2026-08-26, including the coordinated egress-sealing
changes. Historical at-rest findings retain their original evidence dates.

---

## Model-egress posture

**Content sent for model inference leaves only via OpenRouter ZDR routes.**
Chat and embedding requests enforce ZDR per request in code and are also
covered by the OpenRouter account setting. Speech-to-text uses an OpenRouter
model whose eligible endpoints are all ZDR; the post-deploy route check pins
that model-level property.

The disclosed limits are precise:

- Raw audio is sent for transcription without PII redaction. It still uses the
  all-endpoints-ZDR STT route.
- Background and embedding seams use known-value replacement, not NER. That
  guard is fail-open: an unknown value or redaction failure can pass through.
- Channel transports (Telegram/LINE/Apple delivery) and connected-app tool
  calls are data egress, but are outside this *model-inference* posture.
- BYO Anthropic is a parked scaffold, not part of the active posture. It is
  non-ZDR, has zero adopted credentials as of 2026-08-26, defaults off, and the
  rollout disconnect command removes any credential before reconciling config.

Newly provisioned tenant containers receive no OpenAI or Anthropic secret
reference or environment binding: they bind only `openrouter-key` as
`OPENROUTER_API_KEY`, the internal key, and Brave. Legacy containers provisioned
before this change retain inert `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` bindings
until the one-shot fleet scrub runs during rollout; afterward, no tenant
container holds a platform provider key. If the parked BYO scaffold is ever
re-enabled, it swaps in `CLAUDE_CODE_OAUTH_TOKEN` (non-ZDR) for that tenant only.

Closed direct-provider paths in the 2026-08-26 sealing pass:

| Former path | Post-fix route |
|---|---|
| Embeddings and their six callers | OpenRouter embeddings; `provider.zdr=true` is attached per request and every caller supplies tenant context for known-value redaction |
| Telegram voice and LINE voice | Shared `apps/router/transcription.py` OpenRouter STT seam; raw-audio exception disclosed above |
| Container-native STT | `openrouter/openai/whisper-large-v3-turbo`; route check requires every eligible endpoint to be ZDR |
| Lessons cluster naming, copilot, tutoring, and `rewrite_lessons_actionable` | Shared `apps.common.openrouter.chat_completion`, with the mandatory per-request ZDR body |
| `nbhd-image-gen` OpenAI plugin | Deleted from the runtime image and generated plugin allowlist |

---

## 1. The pipeline, in one paragraph

Inbound text (chat/Telegram/LINE messages, tool responses, workspace
documents) is scanned by a DeBERTa NER model + Presidio pattern recognizers
and PII spans are replaced with typed placeholders (`[PERSON_1]`,
`[LOCATION_330]`) before the text reaches OpenClaw or the model provider.
The mapping lives on `Tenant.pii_entity_map`. Outbound assistant text is
rehydrated back to real values at owner-facing send seams
(`rehydrate_for_tenant`, `apps/pii/redactor.py:687`) before delivery. Full
mechanics — the three-source mint-gating model (chat=full mint,
memory-sync=replace-only, tool=validated-only), `RedactionSession`, the
detection guardrails — are in `platform-services.md` §2; not repeated here.

What **has** materially changed since `pii-redaction-security.md` was
written and is not yet reflected there: the hourly cloud arbiter
(`apps/pii/arbiter.py`, shipped PERSON/LOCATION spans to Claude Haiku via
OpenRouter for false-positive triage) is **retired** as of commit `15dda3fe`
(2026-07-09 10:16 JST) — confirmed in code: `pii_arbiter` is removed from
`SYSTEM_CRONS` and its old QStash schedule is actively torn down via
`RETIRED_CRON_PATHS` (`apps/cron/management/commands/register_system_crons.py:129-186`).
It's replaced by a daily 03:45 UTC deterministic sweep
(`pii-junk-sweep` → `apps.pii.junk_sweep.pii_junk_sweep_task`) plus an
owner-facing review queue (§7c). **No PII value has left the platform
boundary via that path since 2026-07-09** — see §7i for two sibling docs
that still describe the old arbiter as live.

---

## 2. What reaches the LLM provider — verified path by path

| Path | Redacted before egress? | Evidence |
|---|---|---|
| Telegram inbound chat | Yes — `redact_user_message` | `apps/router/poller.py:1404` |
| LINE inbound chat | Yes — `redact_user_message` | `apps/router/line_webhook.py:1270` |
| iOS/web chat (`ChatMessageView`, Siri Tier-3 escalation) | Yes — `redact_user_message` inside `enqueue_tenant_turn` | `apps/router/chat_views.py:322`, shared chokepoint for both callers |
| Tool responses (Gmail/Calendar/Reddit) | Yes — `redact_tool_response`, validated-only mint | `apps/integrations/runtime_views.py:930,1010,1092,3719` |
| Workspace `USER.md` (agent-authored sections) | Yes — `RedactionSession(mint='never')` over every section except the placeholder-native privacy legend | `apps/orchestrator/workspace_envelope.py:100-186` (shipped #1083, 2026-07-09) |
| Workspace `USER.md` (**onboarding write**, before first refresh) | Yes — checked mint-redaction; the write is skipped if redaction is unconfirmed | `apps/router/onboarding.py` (`72dad31c`, pinned by `0b6bc9f9`) — see §7a |
| Journal mirror to file share (`memory_sync`) | Yes — same `RedactionSession` pattern | `apps/orchestrator/memory_sync.py` (per `pii-redaction-security.md`) |
| Neighborhood/Friends share | Yes — `redact_user_message`, ephemeral fresh session | `apps/friends/services.py:1158` |
| Insights synthesis (`apps/insights/synthesis.py`) | Effectively yes — reads already-placeholder-space journal/goal data, never calls `rehydrate_*` (verified: zero redact/rehydrate references in the module — it doesn't need to touch the layer because its inputs are already masked) | `apps/insights/synthesis.py` |
| Siri Tier-2 fast responder (`SiriRespondView`) | Yes — placeholder snapshot + checked intent redaction, known-value fallback, entity legend; only the reply is rehydrated | `6b141d5d` |
| Core meditation compose (`gather_meditation_signals` → `apps.core.compose`) | Yes — known-value redaction plus placeholder legend before model egress | `4a134707`, `984b5dc4` |
| BYO Claude CLI container-internal calls | Parked/non-ZDR — outside Django's pipeline if deliberately re-enabled | Flag defaults off and fleet rollout disconnects credentials; scaffold remains in `apps/byo_models` |

The former Siri Tier-2 and meditation-compose cleartext exceptions are closed.
Both now send placeholder-space context through the shared OpenRouter client;
entity legends preserve model usefulness without restoring real values.

---

## 3. Channel coverage (invariant #4 — "cover ALL channels")

| Channel | Inbound redacted | Outbound rehydrated | Notes |
|---|---|---|---|
| Telegram | Yes (`poller.py:1404`) | Yes (`cron_delivery.py`, poller reply relay) | |
| LINE | Yes (`line_webhook.py:1270`) | Yes (`line_webhook.py:674`, `cron_delivery.py`) | |
| iOS/web chat | Yes (`chat_views.py:322`) | Yes (`pending_queue.py`, `_clean_assistant_text_for_app`) | `AppChatMessage.user_text` stored verbatim by design (§6) |
| Siri Tier-0 (`SiriQuickStatusView`, no LLM) | N/A | Yes — `_rehydrated_snapshot` is correct here: it's a deterministic read served straight to the owner's own device, never to a model | `apps/router/siri_views.py:120-148` |
| Siri Tier-2 (`SiriRespondView`, fast model) | Yes — state and intent remain placeholder-space | Yes — only the model reply is rehydrated for the owner | `6b141d5d`, §2, §7b |
| Siri Tier-3 escalation | Yes — routes through `enqueue_tenant_turn` | Yes — same as iOS/web chat | `apps/router/siri_views.py:232` |
| Cron / proactive delivery | N/A (agent-authored, already placeholder-space) | Yes — mandatory egress seam, `rehydrate_for_tenant` | `apps/router/cron_delivery.py:171` |
| Hibernation buffer drain (`BufferedMessage`) | Yes, at write time (#1085) | Yes — Telegram path now converges on the live poller's rehydrated relay | `apps/router/hibernation.py` (drain), model docstring `apps/router/models.py:9-26` |
| Neighborhood/Friends share | Yes, ephemeral session | Yes | `apps/friends/services.py:1158` |
| On-device private mode (iOS Foundation Models) | N/A — never leaves the device | N/A | The only surface where "we can't read your data" is literally true; served the **rehydrated** context digest (`render_context_digest`) precisely because it stays on-device |

Every inbound message-routing channel funnels PII redaction correctly. Siri
Tier-2 now preserves that boundary through its fast-model call as well.

---

## 4. Tier / BYOK / ZDR posture — correcting a stale claim

`pii-redaction-security.md`'s threat-model table (§"Threat Model") states a
three-tier redaction policy: Starter = full redaction, Premium =
financial-only, BYOK = no redaction. **This is no longer how the code
works.** `apps/pii/redactor.py:1-7`, verbatim:

> "Uses tier-based policies from `TIER_POLICIES`. Only `starter` is defined
> today; every tier resolves to it via `.get(tier, starter)`, so redaction
> is effectively full for all tiers (the historical premium=financial-only
> / BYOK=off split is not currently implemented)."

Confirmed structurally: `redact_user_message` takes no BYO/tier branch —
inbound redaction is identical for a platform-key tenant and a BYO-key
tenant, because it runs Django-side, before the container (and whichever
model it's configured to call) ever sees the text. **A BYO tenant's chat,
tool results, and workspace files get the same placeholder treatment as
everyone else's.**

The BYO implementation remains useful as a parked scaffold, but it is not the
fleet posture:

- **ZDR is platform-OpenRouter-only.** BYO Anthropic is non-ZDR. The flag now
  defaults false and is false for non-deleted tenants; zero credentials had
  been adopted when the UI was hidden on 2026-08-26. The API and Key Vault
  plumbing remain parked, with DELETE deliberately available while flag-off.
- **BYO session transcripts on the file share.** Per the encryption-at-rest
  directive §6, BYO tenants' `claude-state/projects/*.jsonl` transcripts
  rest on the per-tenant file share (non-BYO transcripts are ephemeral,
  EmptyDir-only). This is a share-isolation/CMK concern, not a redaction
  gap — the transcript itself reflects Django-redacted input, but sits on
  storage read by the shared storage-account key today.

---

## 5. RedactionSession stability — same-name fusion

Covered in depth in `platform-services.md` §2.4-2.5 and its Risks list;
summarized here because it's a privacy-posture fact an auditor needs: one
canonical key (casefold+strip) maps to exactly one placeholder per tenant,
forever. Two different contacts named "Alex" collapse to the same
`[PERSON_N]`; there is no disambiguation, by design — the USER.md envelope
deliberately withholds identity context from the agent to avoid hallucinated
name restoration. Practical consequence: a rehydrated reply about one Alex
can display the other Alex's stored value if the map ever needs correcting.
This is stable, documented, unfixable without an architecture change, and
already tracked as `[med]` in `platform-services.md`'s Risks section — not
re-litigated here.

---

## 6. At-rest posture after Phase 0

Phase 0 of the encryption-at-rest directive (`docs/encryption-at-rest-directive.md`,
authored 2026-07-09 17:33 JST, status "DESIGN — nothing implemented" for the
*encryption* substrate) shipped several **pseudonymize-at-rest** and
**minimization** changes the same day, ahead of any actual crypto — `apps/crypto`
does not exist in the tree. Encryption (Phases 1-6 of that doc) is unstarted;
what follows is the current *pseudonymization* posture, which is a distinct,
already-live layer:

| Store | At-rest content | Status | Evidence |
|---|---|---|---|
| `AppChatMessage.user_text` | Real values | Plaintext **by design** — the owner's own verbatim words; pseudonymization structurally cannot cover this (encryption is the only lever) | `apps/router/models.py:628` |
| `AppChatMessage.reply_text` | `[PERSON_1]`-space | Pseudonymized (#1084); `ON_DEVICE`-source rows are real-value by construction (never touched the redactor) — rehydration on them is a documented no-op | `apps/router/models.py:629-634` |
| `ConversationTurn.user_text` (Telegram/LINE) | `[PERSON_1]`-space | Pseudonymized — redacted **before** `PendingMessage.user_text` is ever written (`apps/router/pending_queue.py:346-369` docstring is explicit: callers must redact before calling `enqueue_message_for_tenant`) | `apps/router/conversation_capture.py:100-107`, `apps/router/pending_queue.py:361-369` |
| `ConversationTurn.reply_text` | `[PERSON_1]`-space | Pseudonymized (#1084) | `apps/router/models.py:795-801` |
| `ProactiveOutbound.message_text` | `[PERSON_1]`-space | Pseudonymized (#1084) — previously the richest real-PII copy in the control plane, never deleted | `apps/router/models.py:296-301` |
| `LineOutboundMessage.text_excerpt` | `[PERSON_1]`-space | Pseudonymized (#1084) | `apps/router/models.py:392-400` |
| `BufferedMessage.payload` / `.user_text` | Minimal schema-versioned envelope + redacted text | Pseudonymized + minimized (#1085, 2026-07-09) for new rows; **legacy rows (no `schema` marker) still hold the raw provider webhook** and age out via delete-on-forward + TTL sweepers rather than migration | `apps/router/models.py:9-49` |
| `PendingMessage.user_text` / `.payload` | Already-redacted excerpt + prepared message body | Not a plaintext-PII leak (content is redacted pre-enqueue) — but **rows are never deleted today** (retention-hygiene gap, not a confidentiality one) | `apps/router/models.py:161-165`; open item tracked in `encryption-at-rest-directive.md` §7 |
| `Document.markdown`, `Goal.title`, `Task.title` | `[PERSON_1]`-space | Already placeholder-space at rest — agent-authored content is written in placeholder space by construction, and owner edits are **re-redacted on write** (`DocumentDetailView.patch`, `DocumentAppendView.post`) before persisting | `pii-redaction-security.md` "Owner-facing journal rehydration boundary" section |
| `DailyNote`/`UserMemory`/`JournalEntry`/`WeeklyReview`, `Lesson.text/context/galaxy_note/cluster_label`, `TutoringSession.messages`, insights tables | Unverified in this pass | **[open]** — `encryption-at-rest-directive.md` §2 lists these as plaintext content tables without asserting redact-on-write coverage; only `Document.markdown`/`Goal`/`Task` titles are explicitly confirmed redacted-at-write in `pii-redaction-security.md`. Recommend tracing each write path against the same discipline before relying on "journal is already masked" as a blanket claim. |
| `Tenant.pii_entity_map`, `Tenant.pii_denylist` | Real names/emails/phones, plaintext JSON columns | **Plaintext at rest, unchanged.** Encryption is Phase 4 of the directive — explicitly sequenced *last*, gated on Phase 1-3 machinery having production hours, and nothing in `apps/crypto` exists yet | `apps/tenants/models.py:528-552`; `encryption-at-rest-directive.md` §5, §8 |
| `PlatformIssueLog.detail`/`.summary` | Agent free text, "no PII" by convention only | Plaintext, not run through `redact_text` | `apps/platform_logs/models.py:34`, flagged `[med]` in `platform-services.md` Risks — not re-litigated here |

The `Document`/`Goal`/`Task` row is worth calling out explicitly: redaction
is doing double duty as an informal at-rest control for the densest journal
PII, well ahead of the encryption directive's Phase 3. That's a genuine
mitigating fact for an auditor weighing "how exposed is a stolen DB backup
today" — most journal PII is already placeholder-space, not because of
encryption-at-rest work, but as a side effect of the redaction layer being
orthogonal and composable (per `encryption-at-rest-directive.md` §5's own
framing).

---

## 7. Residual egress / leak paths

### 7a. Onboarding `USER.md` cleartext write — **[closed]**

The former first-write bypass is closed by `72dad31c`: onboarding now renders
the managed envelope through checked mint-redaction and skips the write when
redaction is unconfirmed. `0b6bc9f9` pins first-seen-name and failure behavior.

### 7b. Siri Tier-2 + Core meditation cleartext model egress — **[closed]**

Siri Tier-2 now sends a placeholder snapshot and checked-redacted intent, uses
known-value redaction as its deterministic fallback, supplies an entity legend,
and rehydrates only the owner-facing reply (`6b141d5d`). Meditation composition
known-value-redacts its formatted signals (`4a134707`) and adds the placeholder
legend (`984b5dc4`). Both calls use the shared per-request-ZDR OpenRouter seam.

### 7c. PAT scope bypass extends to the PII review/denylist endpoints — **[med][open]**

Cross-reference: [`authn-authz-and-api-surface.md`](authn-authz-and-api-surface.md)
§2a found that Personal Access Token `scopes` are enforced on only 3 of
~90+ `IsAuthenticated` console endpoints. `PIIReviewQueueView`,
`PIIDenylistListView`, and `EntityRegistryBulkDeleteView`
(`apps/tenants/views.py:1179,1302,1435`, routed at
`apps/tenants/urls.py:70-93`) are all plain `[IsAuthenticated]` — no
`HasPATScope`/`HasSessionsReadScope` gate. `GET pii-review-queue/`
specifically returns real span **values** ("Sarah Chen", not `[PERSON_1]`)
by design (§8.2 of the transparency directive treats this as safe because
it's JWT-scoped to the owner's own data) — but a leaked PAT nominally
scoped to `sessions:write` for an unrelated integration (e.g. the YardTalk
push skill) can read it just the same, since PAT scope isn't checked here
at all. Same root cause as the authn-authz finding; worth fixing together.

### 7d. Fail-open redaction — **[med][open]** (tracked, not new)

`redact_user_message`/`redact_text` swallow all exceptions and return the
original text unredacted on any detection failure — real PII reaches the
model for that one turn, with no `user_redactions` metadata recorded (so the
iOS transparency UI can't even signal it happened). Already flagged
`[med]` in `platform-services.md` Risks with the same remediation
recommendation (alert on `_pipeline_load_error` / exception-count metrics);
cited here for completeness of the egress picture, not re-analyzed.

### 7e. `pii_entity_map` is the single highest-value plaintext target in the system — **[high][open]**

The map is the literal reversal key for every placeholder in every stored
document, message, and workspace file across a tenant — a DB backup thief or
anyone with Django shell access reads it as plain JSON and un-redacts
everything at once. This is fully covered by `encryption-at-rest-directive.md`
§5/§8 (Phase 4, explicitly last, gated on Phase 1-3 production hours and on
`#1074` merging first) — flagged here only to state plainly that **today,
with nothing in Phases 1-4 implemented, the map is plaintext**, and that
this is the correct next target once the crypto substrate exists.

### 7f. Same-name fusion — see §5. **[med][by-design]**, tracked, not new.

### 7g. `PlatformIssueLog.detail` — see §6 table. **[med][open]**, tracked in `platform-services.md`, not new.

### 7h. BYO file-share session transcripts — see §4. **[low][by-design]**, tracked in `encryption-at-rest-directive.md` §6, not new here.

### 7i. Two sibling docs contain claims stale as of this pass — **[low][partially-mitigated]**

- `docs/reference/platform-services.md` §1.3's cron schedule table lists
  `40 * * * * | pii-arbiter | pii_arbiter | LLM sweep of new PII mints →
  denylist` as an active schedule, and §2.6 describes the arbiter as a live
  hourly Claude Haiku egress. Both are stale — confirmed in code (§1 above)
  that the schedule is torn down and the task is replaced by the zero-egress
  `pii-junk-sweep`. Likely a snapshot-timing artifact of this same
  multi-agent audit sweep (`platform-services.md` and the arbiter retirement
  landed close together); flagging so Phase 3 synthesis corrects it rather
  than propagating the stale claim.
- `docs/encryption-at-rest-directive.md` §2's ground truth states
  "`USER.md` is **not redacted** today (`workspace_envelope.py` — zero
  `RedactionSession` calls)" and §11 point 5 states "PII-map names egress to
  Haiku hourly during arbitration." Both were accurate when likely drafted
  but are superseded by same-day commits: USER.md redaction shipped as #1083
  (confirmed live in current `workspace_envelope.py`, §1/§6 above) and the
  arbiter was retired at 10:16 JST, roughly seven hours before the directive
  was authored at 17:33 JST. Neither changes the directive's Phase 4
  sequencing decision, but both should be corrected so a reader doesn't
  under-credit work already shipped (USER.md) or over-state a currently-false
  residual (Haiku egress) — see §7a for the *actual* remaining USER.md gap,
  which is different from what the directive describes.

---

## Findings

- **[closed]** Onboarding's first `USER.md` write now uses checked
  mint-redaction and fails closed when redaction is unconfirmed (`72dad31c`,
  `0b6bc9f9`). See §7a.

- **[high][open]** `Tenant.pii_entity_map`/`pii_denylist` remain plaintext
  JSON columns — the reversal key for every placeholder fleet-wide. Fully
  tracked as Phase 4 of `encryption-at-rest-directive.md` (correctly
  sequenced last); nothing in that directive is implemented yet
  (`apps/crypto` doesn't exist). Not a new finding, but the single highest-
  value target for whenever the crypto substrate lands. See §6, §7e.

- **[closed]** Siri Tier-2 and meditation compose now keep model-bound context
  in placeholder space and provide entity legends; real values return only at
  the owner-facing Siri reply seam (`6b141d5d`, `4a134707`, `984b5dc4`). See
  §2, §7b.

- **[med][open]** The PAT-scope enforcement gap found in
  [`authn-authz-and-api-surface.md`](authn-authz-and-api-surface.md) §2a
  extends to the PII review-queue and denylist endpoints
  (`pii-review-queue/`, `pii-denylist/`, `entity-registry/bulk/` — all plain
  `IsAuthenticated`, no scope check). `GET pii-review-queue/` returns real
  span values by design; any leaked PAT, regardless of its declared scope,
  can read them today. Same root cause and same fix as the authn-authz
  finding — worth landing together. See §7c.

- **[med][open]** Fail-open redaction (`redact_user_message`/`redact_text`
  swallow all exceptions and forward the original, unredacted text) has no
  alerting on the cached `_pipeline_load_error` or on redaction-exception
  rate. Already tracked in `platform-services.md` Risks; restated here as
  part of the egress picture. See §7d.

- **[med][by-design]** Same-name PII fusion (one canonical key → one
  placeholder per tenant, forever) can misattribute a rehydrated reply to
  the wrong same-named contact. Already tracked in `platform-services.md`
  Risks as a permanent, documented trade-off. See §5, §7f.

- **[med][open]** `PlatformIssueLog.detail`/`.summary` accepts agent free
  text with "no user PII" enforced by convention only, not run through
  `redact_text`. Already tracked in `platform-services.md` Risks. See §6,
  §7g.

- **[low][by-design]** BYO tenants' `claude-state/projects/*.jsonl` session
  transcripts rest on the shared-key file share (non-BYO transcripts are
  ephemeral). Already tracked as a named residual in
  `encryption-at-rest-directive.md` §6 (isolation/CMK track, not content
  encryption). See §4, §7h.

- **[low][partially-mitigated]** Two sibling docs contain claims that are
  stale as of this pass: `platform-services.md` describes the retired
  arbiter as an active hourly cloud-egress cron, and
  `encryption-at-rest-directive.md` describes `USER.md` as unredacted and
  the PII map as actively egressing to Haiku — both superseded by same-day
  commits (#1083 and the arbiter retirement at `15dda3fe`). Recommend
  Phase-3 synthesis correct both rather than propagate them. See §7i.

- **[by-design, confirmed]** BYO/BYOK tenants receive the *same* inbound
  redaction as platform-key tenants — the historical premium=financial-only
  / BYOK=off tiered policy described in `pii-redaction-security.md`'s
  threat-model table is not implemented; a single `starter` policy applies
  fleet-wide, confirmed via `apps/pii/redactor.py:1-7`'s own comment. This
  is a **positive** correction: BYO traffic is better-protected against
  redaction gaps than the existing doc implies. See §4.

- **[by-design, confirmed]** `Document.markdown`/`Goal.title`/`Task.title`
  are already placeholder-space at rest today (agent-authored content is
  written masked; owner edits are re-redacted on write), functioning as an
  informal at-rest control well ahead of the encryption directive's Phase 3.
  See §6.
