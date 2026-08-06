# PII coverage audit — 2026-08-04

Codex read-only audit of origin/main@41d742a4 (post #1373/#1374/#1375), commissioned after the Theo/Optiver placeholder-space breakdown. Arbitrated by Fable; recommendations at end.

# Ledger Snapshot

- Goal: map PII/redaction coverage across tenant-content storage, model context, and third-party egress.
- Status: audit complete; no files, databases, or external systems were modified.
- Source audited: `origin/main@41d742a4a14bbba1eb981ff9e58f564ee7d6d014`, the local ref containing merged PRs #1373, #1374, and #1375.
- Open question: the provenance of the specific Optiver project document is not recorded in code; its most likely creation path is identified below as **UNVERIFIED**.

Line references below are against that `origin/main` commit, not the older checked-out worktree.

## Executive finding

The PII layer is not a universal persistence or model-egress boundary. Coverage is path-dependent:

- Live Telegram/LINE/iOS model-bound messages are redacted and annotated before the main assistant call.
- Selected owner document/task/goal edits are redacted on write.
- Most runtime authoring tools—journal, daily notes, memory, tasks, goals, weekly reviews, fuel, finance, insights, lessons, cron—store and return text exactly as received.
- Local tool results are serialized directly into model context. They do not pass through `redact_tool_response`.
- Several background pipelines send raw Layer-1 content directly to OpenRouter or an embedding provider.
- Reply storage assumes the model stayed in placeholder-space. There is no value-aware re-redaction at the final storage seam.

That combination explains the observed failure:

```text
plaintext Layer-1 row
  → raw local tool result
  → OpenRouter receives real name
  → model echoes real name
  → reply storage accepts it unchanged
  → raw reply feeds later recap/digest
```

Legend:

- ✅ redactor applied at this seam.
- ⚠️ conditional, known-entity-only, inherited placeholder-space, or contract-only.
- ❌ no PII redactor; plaintext can pass.
- ↗ intentional rehydration for an owner-facing destination.

---

# 1. Coverage map

## 1A. Redactor fundamentals

| Component | Coverage |
|---|---|
| Tenant entity registry | `Tenant.pii_entity_map` stores the placeholder-to-original-value map in Postgres JSON, including the original PII values. This is necessarily plaintext from the application’s perspective: `apps/tenants/models.py:602-608`. |
| User-message redaction | ✅ `redact_user_message` detects/mints placeholders and persists entity mappings: `apps/pii/redactor.py:1102-1141`. Detection failures fail open and return the original text: `apps/pii/redactor.py:1137-1141`. |
| Known-value masking | ✅ but known-only: `redact_known_entities` replaces values already present in the tenant map: `apps/pii/redactor.py:1076-1099`. |
| Tool-response redaction | Available but not globally wired. Recursive implementation: `apps/pii/redactor.py:1430-1516`. Its validated mode does not mint new PERSON/LOCATION entities, so previously unknown people can remain: `apps/pii/redactor.py:769-792`. |
| Model annotations | Annotated placeholder format and context transformation: `apps/pii/redactor.py:42-54,876-900`. |
| Rehydration | `rehydrate_text` / `rehydrate_for_tenant`: `apps/pii/redactor.py:1024-1073`. |
| Failure posture | `RedactionSession.redact` also fails open per call: `apps/pii/redactor.py:998-1000,1019-1021`. This means “the function was called” does not guarantee all PII was removed. |

## 1B. Stores and their write seams

### Chat, buffers, and proactive content

| Store/path | Stored form | Write seam and assessment |
|---|---:|---|
| `app_chat_messages.user_text` — iOS/server chat | ❌ plaintext | The model explicitly describes this field as the user’s verbatim text: `apps/router/models.py:694-712`. iOS create stores request `text` directly: `apps/router/chat_views.py:402-436`. Only the queued model payload is redacted later: `apps/router/chat_views.py:484-531`. |
| `app_chat_messages.reply_text` | ⚠️ assumed placeholder-space | Schema says tenant-model replies should be placeholder-space, except `ON_DEVICE`: `apps/router/models.py:706-712`. Final storage does not enforce that assumption: `apps/router/pending_queue.py:2038-2061`. |
| `app_chat_messages.partial_text` | ❌ as received | Model-stream partials are persisted without PII processing: `apps/router/models.py:767-777`; final cleanup strips markers but does not re-redact values: `apps/router/pending_queue.py:2140-2230`. |
| `app_chat_messages.redaction_metadata` | ❌ sensitive map | Stores mappings used to explain/redeliver the turn, including original values: `apps/router/models.py:790-801`. |
| `ON_DEVICE` AppChat turns | ❌ plaintext by design | User and local-model reply are stored verbatim: `apps/router/chat_views.py:930-990`. The code discloses that a later server digest can reach OpenRouter: `apps/router/chat_views.py:933-939`. |
| `ConversationTurn` | ⚠️ caller-dependent | Model intends placeholder-space: `apps/router/models.py:889-930`. Capture strips markers but does not redact values itself: `apps/router/conversation_capture.py:79-100,113-154`. |
| Telegram/LINE pending and buffered messages | ✅ on current ingress paths; ⚠️ legacy/failure | Telegram calls `redact_user_message`: `apps/router/poller.py:1484-1503`; LINE does likewise: `apps/router/line_webhook.py:1375-1386`. Buffer construction performs user redaction and a known-value pass: `apps/router/buffer_envelope.py:167-199`. The models acknowledge legacy raw rows: `apps/router/models.py:22-25,152-165`. |
| Hibernation buffers | ✅ current ingress, annotated at drain | Stored through the pending/buffer path. Telegram and LINE annotate before model dispatch: `apps/orchestrator/hibernation.py:1158-1209,1329-1367`. |
| `ProactiveOutbound.message_text` | ⚠️ assumed placeholder-space | The model contract says placeholder-space: `apps/router/models.py:299-310`. `record_proactive_outbound` stores supplied text without a redaction backstop: `apps/router/proactive_context.py:96-159`. A contaminated proactive reply can therefore persist plaintext. |
| Delivery response excerpts | ⚠️ as received | `CronDeliveryOccurrence.response_excerpt` is a free-form stored excerpt: `apps/router/models.py:409-415`; no central redactor was found on this field. |

The supplied statement that “chat messages are placeholder-space at rest” is true for current Telegram/LINE buffering and intended tenant-model replies, but not for `AppChatMessage.user_text`, on-device turns, or a model reply contaminated before storage.

### Journal documents and legacy journal models

| Write path | Stored form | Evidence |
|---|---:|---|
| Runtime `nbhd_document_put`, all `Document.kind` values | ❌ no write redactor | The plugin forwards title/markdown directly: `runtime/openclaw/plugins/nbhd-journal-tools/index.js:207-248`. Django only sanitizes image markup and stores the values: `apps/integrations/runtime_views.py:2221-2289`. All kinds share this path; there is no kind-specific redaction hook. |
| Runtime document append | ❌ | Plugin: `runtime/openclaw/plugins/nbhd-journal-tools/index.js:337-379`; append endpoint stores sanitized-but-not-redacted content: `apps/integrations/runtime_views.py:2619-2699`. |
| Console/JWT document POST/create | ❌ | Create serializer path stores request title/markdown without `_redact_owner_input`: `apps/journal/document_views.py:188-212`. |
| Console/JWT document PATCH | ✅ | Title and markdown are redacted before save: `apps/journal/document_views.py:349-390`. |
| Console/JWT document append | ✅ | Appended owner text is redacted: `apps/journal/document_views.py:429-461`. |
| Owner document GET/list | ↗ rehydrated | Detailed document responses rehydrate title/markdown; list rehydrates titles: `apps/journal/document_serializers.py:10-66`. |
| Runtime daily-note `set_section` / append | ❌ | Plugin calls are direct: `runtime/openclaw/plugins/nbhd-journal-tools/index.js:381-489`. Runtime endpoint modifies markdown with only image sanitization: `apps/integrations/runtime_views.py:1349-1508`. |
| Legacy DailyNote entry/edit/template/section paths | ❌ | View writes: `apps/journal/views.py:187-263,302-339,363-389`; service writes: `apps/journal/services.py:410-513`. Owner responses rehydrate known placeholders, but writes are raw: `apps/journal/views.py:68-100`. |
| Runtime `nbhd_memory_update` DB document | ❌ | Memory update stores markdown without redaction: `apps/integrations/runtime_views.py:1525-1602`. |
| Legacy memory PUT | ❌ | Direct serializer save: `apps/journal/views.py:397-431`. |
| Runtime WeeklyReview create | ❌ | Runtime view and raw serializer path: `apps/integrations/runtime_views.py:587-614`; `apps/journal/serializers.py:48-152`. |
| Owner WeeklyReview create/update | ❌ | Owner view and serializer pass raw fields: `apps/journal/views.py:439-470`; `apps/journal/serializers.py:283-355`. |
| WeeklyReview read into OpenClaw | No dedicated read tool found | The runtime plugin has a create operation, but no dedicated WeeklyReview read operation at this commit. Owner API reads remain plaintext. This absence does not protect the content when it is copied into other context-producing stores. |
| Legacy `JournalEntry` / `NoteTemplate` | ❌ | Raw fields and serializer writes: `apps/journal/models.py:17-68`; `apps/journal/serializers.py:170-215,370-411`; views: `apps/journal/views.py:484-530`. |
| Welcome/starter cards | Safe by construction | Static product copy, not tenant-authored content: `apps/journal/services.py:209-284`; seeded during tenant creation: `apps/tenants/services.py:94-128`. No redactor, but no user PII source. “Welcome cards” is **UNVERIFIED** terminology; these starter documents are the matching implementation found. |

### Tasks, goals, purposes, and reconciliation rows

| Write path | Stored form | Evidence |
|---|---:|---|
| Runtime task create/update | ❌ | Model tool forwards JSON directly: `runtime/openclaw/plugins/nbhd-journal-tools/index.js:1741-1815`. Runtime views and serializers save raw title/description: `apps/integrations/runtime_views.py:849-994`; `apps/journal/lifecycle_serializers.py:106-108,145-147`. |
| Runtime goal create/update | ❌ | Runtime endpoints save supplied title/description: `apps/integrations/runtime_views.py:625-755`. |
| Owner/JWT Task create/update | ✅ | Shared `_redact_owner_input`: `apps/journal/lifecycle_views.py:32-61`; detail update: `apps/journal/lifecycle_views.py:89-121`; create: `apps/journal/lifecycle_views.py:233-283`. |
| Owner/JWT Goal create/update | ✅ | Detail update: `apps/journal/lifecycle_views.py:162-194`; create: `apps/journal/lifecycle_views.py:286-325`. |
| Purpose statements/evidence | ❌ on runtime/extraction paths | Fields are plaintext: `apps/journal/models.py:377-436`; extraction approval copies model output directly: `apps/router/extraction_callbacks.py:176-208`. |
| Nightly `PendingExtraction` | ❌ | Model output is copied into `PendingExtraction.text` and metadata without a redaction pass: `apps/journal/extraction.py:678-779`. |
| Extraction approval → Lesson/Goal/Task/Document/Purpose | ❌ | Raw pending text is copied into each target: `apps/router/extraction_callbacks.py:64-208`. |
| Reconcile task/goal writes | ❌ | Existing raw context is assembled: `apps/journal/reconciliation.py:37-96`; evidence, before-state, subtasks, and goal actions are saved directly: `apps/journal/reconciliation.py:110-277`. |
| `PendingTaskAction.evidence` and before-state | ❌ | Content fields: `apps/journal/models.py:542-617`; no global model/save redactor. |

### Fuel and finance

| Store/path | Stored form | Evidence |
|---|---:|---|
| Workout plan name/objective/notes | ❌ | Plaintext fields: `apps/fuel/models.py:44-79`; runtime create/update saves raw request values: `apps/fuel/runtime_views.py:1385-1667`. |
| Workout activity, notes, detail, skip reason | ❌ | Fields: `apps/fuel/models.py:186-298`; create/update/skip/complete paths: `apps/fuel/runtime_views.py:110-193,221-322,366-459`. |
| Fuel profile goals, limitations, equipment, additional context | ❌ | Runtime profile fields and writes: `apps/fuel/runtime_views.py:37-51,667-755`. |
| Sleep/workout free-form notes | ❌ | Example sleep note field: `apps/fuel/models.py:560`; no PII seam found. |
| Finance account nickname | ❌ | Field: `apps/finance/models.py:37-38`; runtime list and create/upsert: `apps/finance/runtime_views.py:69-164`. |
| Transaction description | ❌ | Field: `apps/finance/models.py:107-109`; runtime write: `apps/finance/runtime_views.py:167-215`. |
| Finance amounts/balances | Plaintext by design | These are sensitive financial data but are outside the name/location redactor’s current entity taxonomy. |

### Tenant workspace files

| File surface | Stored form | Evidence |
|---|---:|---|
| Journal mirrors under `memory/journal/...` | ⚠️ known-value-only | A `RedactionSession(mint="never")` replaces only already registered entities before upload: `apps/orchestrator/memory_sync.py:49-76,103-240`. Unknown names in a raw Document remain. |
| `USER.md` managed section | ⚠️ known-value-only | The envelope uses `mint="never"` for generated sections: `apps/orchestrator/workspace_envelope.py:101-187`. PR #1373 supplies placeholder-space identity metadata: `apps/tenants/envelope.py:234-323`. |
| Existing content outside the managed `USER.md` markers | ❌ preserved verbatim | Merge explicitly preserves agent-written content outside the managed block: `apps/orchestrator/workspace_envelope.py:330-364`; resulting file is written at `apps/orchestrator/workspace_envelope.py:455-512`. |
| `MEMORY.md` / `memory/*.md` written by OpenClaw | ❌ no Django redaction seam | Generated configuration explicitly instructs workspace memory backup writes: `apps/orchestrator/config_generator.py:1286-1348`. The Django `nbhd_memory_update` path only controls its DB Document and journal mirror; it does not guard arbitrary OpenClaw file writes. Exact OpenClaw-internal `MEMORY.md` implementation is **UNVERIFIED** because it is outside this repository. |
| `SOUL.md` / identity growth | ❌ | Identity merge preserves generated growth verbatim: `apps/orchestrator/identity_merge.py:1-32`; service writes and reasserts files without redaction: `apps/orchestrator/services.py:752-805,1110-1175`. |
| Document chunks, upload names, excerpts | ❌ | Chunk text, original filenames, and artifact excerpts are plaintext fields: `apps/journal/models.py:620-647,695-792`. Upload parsing explicitly does not redact extracted content: `apps/journal/document_ingestion.py:577-591`. |
| “Encryption” sidecar columns | Not active protection | Journal, lesson, insight, and meditation models describe these sidecars as shipping dark; e.g. `apps/lessons/models.py:24-30` and `apps/core/models.py:115-140`. They do not currently change the plaintext-column findings. |

### Logs and diagnostics

| Surface | Coverage |
|---|---|
| `PlatformIssueLog` | ⚠️ contract plus known-only masking. Model says detail must exclude user content: `apps/platform_logs/models.py:8-44`. Ingestion runs `redact_known_entities`, not full detection: `apps/platform_logs/views.py:106-124`. Unknown PII can survive; the safe summary is also logged: `apps/platform_logs/views.py:126-132`. |
| Steward `EvidenceEvent` | ⚠️ contract-only. Payload is documented as no-user-PII: `apps/steward/models.py:254-294`; no PII redactor enforces it. PR #1375’s OpenRouter collector imports metrics/usage, not prompts or content: `apps/steward/collectors/openrouter.py:96-139,235-253,363-384`. |
| Steward digests | ⚠️ control-character/length sanitation, not PII redaction. `safe_text`: `apps/steward/sanitize.py:6-10`; event rendering: `apps/steward/digest.py:83-108`; storage/send: `apps/steward/digest.py:512-622`. |
| Sentry | ⚠️ SDK defaults reduce collection but are not a content redactor. `send_default_pii` defaults false and local variables are disabled: `config/settings/base.py:786-812,890-911`. The final production environment value is **UNVERIFIED**. Interpolated log and exception text may still contain content; the only explicit event scrubber is for the BYO endpoint: `config/settings/base.py:850-888`. |
| General application logs | ❌ no central PII filter found | WARNING+ logs are forwarded to Sentry: `config/settings/base.py:804-812`. Code-level “safe” logging therefore remains dependent on each call site. |

## 1C. Model context: what reaches OpenRouter

| Context source | Coverage before model |
|---|---:|
| Current Telegram/LINE/iOS inbound turn | ✅ redacted and annotated. Telegram/LINE drains: `apps/router/pending_queue.py:1809-1837,1704-1708`; iOS drain: `apps/router/pending_queue.py:1861-1935`. |
| Hibernation buffers | ✅ annotated at dispatch: `apps/orchestrator/hibernation.py:1189-1191,1350-1352`. |
| Thread recap | ⚠️ user text is redacted again; replies are reused unchanged: `apps/router/thread_recap.py:145-215`. A previously contaminated reply therefore leaks into the next model context. |
| Conversation digest | ⚠️ AppChat user gets known-only masking; reply text is passed unchanged under a placeholder-space assumption: `apps/router/conversation_capture.py:269-374`, especially `341-368`. |
| `USER.md` bootstrap | ⚠️ managed portion known-only; preserved content raw. It is loaded each turn: `apps/orchestrator/workspace_envelope.py:1-7,101-187,330-364`. |
| `SOUL.md`, identity, workspace memory files | ❌ no PII transformation before OpenClaw bootstrap was found. |
| Session-start semantic recall | ❌ at embedding seam | Raw user text is sent to the embedding provider before later chat redaction: `apps/router/poller.py:1101-1108,1136-1202`, specifically `1161-1164`. Raw retrieved goals/tasks/chunks/lessons are later included in session context; final main-chat redaction can mask them, but the embedding request has already occurred. |
| Nightly journal extraction | ❌ direct raw egress | Daily-note/reconciliation sources are collected raw: `apps/journal/extraction.py:580-653`; prompt is posted directly to OpenRouter: `apps/journal/extraction.py:118-151`. |
| Agenda hints | ❌ | Raw journal context is formatted and sent to OpenRouter: `apps/journal/agenda_hints.py:155-179`. |
| Weekly insight synthesis | ❌ | AssistantInsight statements are formatted raw: `apps/insights/synthesis.py:289-351`; direct OpenRouter call: `apps/insights/synthesis.py:354-371`. |
| Meditation composition | ❌ | Raw profile, lesson, daily-note, goal, and fuel signals are gathered deliberately: `apps/core/services.py:79-145`; submitted to OpenRouter without redaction: `apps/core/compose.py:302-337`. |
| Embedding jobs | ❌ | Raw document chunks: `apps/journal/embedding.py:59-104`; raw lesson text to OpenAI embeddings: `apps/lessons/services.py:31-47`. |

### Local tool-result families

The common runtime behavior is direct JSON serialization:

- Journal: `runtime/openclaw/plugins/nbhd-journal-tools/index.js:73-77`.
- Fuel: `runtime/openclaw/plugins/nbhd-fuel-tools/index.js:60-62`.
- Finance: `runtime/openclaw/plugins/nbhd-finance-tools/index.js:61-63`.
- Insights: `runtime/openclaw/plugins/nbhd-insights-tools/index.js:65-67`.
- Friends: `runtime/openclaw/plugins/nbhd-friends-tools/index.js:63-65`.
- Automation: `runtime/openclaw/plugins/nbhd-automation-tools/index.js:60-62`.

| Tool family/result | Coverage |
|---|---:|
| Journal document get | ❌ raw title/markdown: `apps/integrations/runtime_views.py:2187-2219`. |
| Journal search | ❌ raw title/snippet: `apps/integrations/runtime_views.py:2292-2377`. |
| Journal context | ❌ raw goals, tasks, ideas, daily notes, memory, and north-star text: `apps/integrations/runtime_views.py:1606-1742`. |
| Daily-note get | ❌ raw markdown: `apps/integrations/runtime_views.py:1349-1508`. |
| Memory get | ❌ raw markdown: `apps/integrations/runtime_views.py:1525-1602`. |
| Task and goal lists | ❌ runtime list responses contain raw title/description: `apps/integrations/runtime_views.py:625-755,849-994`. |
| `nbhd_journal_query` | ❌ returns selected entry/task/goal fields through direct `renderPayload`: `runtime/openclaw/plugins/nbhd-journal-tools/index.js:1983-2067`. |
| `reconcile_scan` | ❌ returns raw purpose/goal/task/project excerpts, finance nicknames, and fuel activity: `apps/integrations/runtime_views.py:3148-3445`, particularly `3217-3407`. |
| WeeklyReview | No dedicated runtime read tool found | Its create response is raw; owner reads are raw. |
| Fuel summary/list/profile/plans/workouts | ❌ raw activity, notes, plan names/objectives, and profile context. Example summary construction: `apps/fuel/runtime_views.py:517-664`; plugin serializes directly. |
| Finance accounts/transactions/summary | ❌ raw nicknames and descriptions: `apps/finance/runtime_views.py:69-215`; plugin serializes directly. |
| Insights list | ❌ raw statements: `apps/insights/runtime_views.py:264-332`; plugin serializes directly. |
| Lesson suggest/search/pending | ❌ raw Lesson text/context from the generic journal plugin: `runtime/openclaw/plugins/nbhd-journal-tools/index.js:702-817`. |
| Automation/cron results | ❌ reminder text/name/payload is returned directly. Cron data fields: `apps/cron/models.py:78-140`; plugin: `runtime/openclaw/plugins/nbhd-automation-tools/index.js:170-315`. |
| Friends chat absorption | ✅ fresh recipient-specific redaction before model context: `apps/friends/services.py:1299-1327`. |
| Shared lessons | ✅ fail-closed neutralized snapshot for cross-tenant sharing: `apps/friends/models.py:151-187`; scrub implementation explicitly avoids the main redactor’s fail-open behavior: `apps/friends/scrub.py:4-20`. |
| Selected Google/Gmail/Calendar/Reddit results | ⚠️ these do call `redact_tool_response`: `apps/integrations/runtime_views.py:1105-1107,1185-1187,1267-1269,4039-4043`. Validated mode still does not freely mint new PERSON/LOCATION entities. |
| Lesson copilot and cluster naming | ⚠️ protected special cases | Copilot applies known-only context redaction and rehydrates only at owner egress: `apps/lessons/copilot.py:470-555`; cluster naming similarly redacts before its LLM call: `apps/lessons/cluster_naming.py:228-260`. These do not protect generic lesson tutoring/extraction. |
| Lesson tutoring | ❌ explicitly raw | Lesson text, journal entries, and connection candidates are placed in the prompt: `apps/lessons/tutoring.py:485-535`. |

This tool-response set is the direct contamination route described in the request.

## 1D. Third-party egress

| Destination | What leaves | Coverage |
|---|---|---|
| OpenRouter — main assistant | Annotated inbound chat, workspace envelopes, recap, and all selected tool results | Inbound current turn ✅; local tool results and preserved workspace content ❌. Dispatch seams: `apps/router/pending_queue.py:1704-1708,1809-1837,1917-1935`. |
| OpenRouter — background jobs | Journal extraction, agenda hints, insight synthesis, meditation composition, tutoring | ❌ direct raw prompts as cited above. Shared client sends messages exactly as supplied: `apps/common/openrouter.py:54-125`. |
| OpenAI/embedding provider | User query, document chunks, lessons | ❌ raw at the embedding seams: `apps/router/poller.py:1161-1164`; `apps/journal/embedding.py:59-104`; `apps/lessons/services.py:31-47`. |
| Telegram/LINE outbound | Final owner-facing message | ↗ deliberate rehydration. Telegram: `apps/router/pending_queue.py:2241-2277`; LINE: `apps/router/line_webhook.py:714-764`; cron/proactive: `apps/router/cron_delivery.py:400-457`. |
| APNs push | Reply or proactive preview | ↗ deliberately rehydrated before push: `apps/router/pending_queue.py:2075-2077,2224-2228`; proactive: `apps/router/proactive_context.py:180-208`. Preview/body delivery: `apps/router/push_views.py:76-92,399-409,428-480`. Consequently Apple and the lock screen may receive/display real names. |
| Mailgun user email | User email, display name, transactional content | ↗ intentionally plaintext to the recipient; e.g. assistant-ready email: `apps/tenants/management/commands/send_assistant_ready_email.py:94-117`. No PII redactor. |
| `sautai` M2M | Linked numeric user ID and meal-planning prompt/preferences | ↗ prompt is intentionally rehydrated before POST: `apps/integrations/sautai_client.py:184-203,289-325`. The current path does **not** rehydrate an email; identity is a linked numeric ID: `apps/integrations/sautai_client.py:100-112`. The remembered “rehydrated email” behavior is therefore stale or **UNVERIFIED**. |
| Gemini TTS | Meditation narration segments | ❌ narration text is sent to Gemini TTS: `apps/core/render.py:445-502,627-665`. The composition prompt tells the model not to use names/placeholders, but that is an instruction rather than a redaction boundary: `apps/core/compose.py:117`. |
| Steward Telegram digest | Operational digest body | ⚠️ no redactor; relies on no-PII event contracts. Telegram send: `apps/steward/notify.py:20-35`. |
| Steward Mailgun fallback | Operational digest body | ⚠️ same contract-only protection: `apps/steward/notify.py:65-91,138-158`. |
| Sentry | Exceptions and WARNING+ logs | ⚠️ default PII capture off, but no general text redactor: `config/settings/base.py:786-812,850-911`. |
| Tenant file share | Journal mirrors, workspace identity/memory, meditation artifacts | Mixed: journal mirror known-only; `USER.md` managed known-only; preserved `USER.md`, `SOUL.md`, and framework memory writes unredacted. Storage-provider configuration and external retention are **UNVERIFIED** in this code-only audit. |

---

# 2. Additional content surfaces discovered

These were outside the minimum list but are part of the same exposure class.

| Surface | Finding |
|---|---|
| Lessons, galaxy notes, star journal entries | ❌ plaintext fields and raw CRUD serialization: `apps/lessons/models.py:13-101,215-247`; `apps/lessons/serializers.py:12-33`. Lesson extraction/tutoring/embeddings can send them raw to model providers. |
| Assistant insights and user response notes | ❌ plaintext: `apps/insights/models.py:134-164`. Reply markers are copied directly into `AssistantInsight.statement`: `apps/insights/markers.py:150-180`. Runtime list results and weekly synthesis expose them to OpenRouter. |
| Meditation sessions and feedback | ❌ title, theme, manifest, guidance text, and feedback note are plaintext: `apps/core/models.py:91-170`. Raw source signals go to OpenRouter; guidance goes to Gemini TTS. |
| Friend messages | ❌ deliberately plaintext human-to-human store: `apps/friends/models.py:479-503`. They are freshly redacted only when absorbed into an agent session: `apps/friends/services.py:1299-1327`. |
| Shared missions/goals and updates | ❌ titles, descriptions, commitments, and update text are plaintext: `apps/friends/models.py:509-599`. Friends tool results are directly serialized. |
| Pending destructive actions/audit log | ❌ payload and display summary stored as received: `apps/actions/views.py:113-168,182-187`; models: `apps/actions/models.py:29-44,106-123`. Owner confirmation deliberately rehydrates: `apps/actions/messaging.py:46-54`. |
| Cron reminders | ❌ name, free-form job data, and typed payload stored as supplied: `apps/cron/models.py:78-140`. The plugin explicitly says fixed reminder text is sent verbatim: `runtime/openclaw/plugins/nbhd-automation-tools/index.js:170-198`. |
| Automation run payload/error | ❌ generic plaintext JSON/error fields: `apps/automations/models.py:50-75`. Whether a particular automation copies user content into them is workflow-dependent. |

---

# 3. Exact Optiver project-document path

There is no global `Document.save()` redactor or model signal. The observed H1 was therefore not reliably redacted “because it was a Document.”

The most likely path is:

1. The live inbound turn containing “Optiver” or “Theo” was redacted before the assistant saw it: `apps/router/poller.py:1484-1503` or the corresponding LINE/iOS drain.
2. The assistant generated a `nbhd_document_put` call whose title/markdown already contained `[ORG_n]` or `[PERSON_n]`.
3. The OpenClaw plugin forwarded that placeholder text unchanged: `runtime/openclaw/plugins/nbhd-journal-tools/index.js:207-248`.
4. The runtime endpoint stored it unchanged: `apps/integrations/runtime_views.py:2236-2276`.

So the H1 was most likely protected by **placeholder inheritance from model context**, not by a Document write-time control. This provenance is **UNVERIFIED** without the tool/audit record for that document.

The only alternate redacting document path is an owner PATCH or append:

- PATCH: `apps/journal/document_views.py:349-390`.
- Append: `apps/journal/document_views.py:429-461`.

Paths that do not provide the same guarantee include:

- Runtime Document PUT/append: no redactor.
- Console/JWT Document POST: no redactor.
- Runtime task/goal creation: no redactor.
- Daily-note writes: no redactor.
- WeeklyReview writes: no redactor.
- Extraction/reconcile writes: no redactor.

This also explains the divergent task title: once a raw task or journal tool result put “Theo” into model context, the model could emit `"Reply to Optiver (Theo)…"` literally, and runtime task creation stored it unchanged.

---

# 4. Gap ranking

| Rank | Gap | Cloud-model leak | Plaintext at rest | Volume / role in breakdown |
|---:|---|---:|---:|---|
| 1 | Local tool results: journal/task/goal/reconcile/fuel/finance/insights/lessons/cron | **Yes** | Source stores yes | Very high frequency. This is the primary tool-result contamination vector. |
| 2 | Raw background prompts and embeddings | **Yes** | Source stores yes | Nightly/weekly/session-start. Extraction, agenda, insight synthesis, meditation, tutoring, and embeddings bypass the live-chat redactor entirely. |
| 3 | Reply/partial/proactive storage has no re-redaction backstop | Indirectly—raw replies feed recap/digest | **Yes** | High-frequency amplification mechanism. Direct cause of “placeholder-space breaks down mid-conversation.” |
| 4 | Runtime Layer-1 redact-on-write gaps | Through tool reads | **Yes** | Broadest at-rest issue: documents, daily notes, memory, tasks, goals, reviews, lessons, insights, fuel, finance, actions, and cron. |
| 5 | Workspace `USER.md` preserved content, `SOUL.md`, and memory files | **Yes**, when bootstrapped | **Yes**, file share | Persistent and automatically reusable across sessions. Managed USER metadata is improved by #1373, but unmanaged growth remains. |
| 6 | AppChat user text, ON_DEVICE turns, reply metadata | Potentially via recap/digest | **Yes** | Intentional exact-history design, but current recap masking is known-only/incomplete. |
| 7 | Lesson/insight/meditation specialist pipelines | **Yes** | **Yes** | Lower frequency than chat tools, but high-content and potentially intimate. Meditation adds Gemini TTS egress. |
| 8 | Pending actions, cron reminders, proactive response excerpts | Potentially | **Yes** | Moderate volume; agent-generated content inherits any earlier contamination. |
| 9 | Known-only/fail-open mirrors, buffers, and platform logs | Possible | Yes | Lower volume, but “redactor called” is not a hard guarantee. |
| 10 | Steward/Sentry | Possible, mostly operational | Yes | Lower expected user-content volume. Relies heavily on call-site contracts and SDK defaults. |

Owner-facing Telegram/LINE, APNs, Mailgun, and linked sautai delivery are intentional egress rather than accidental cloud-model leakage. They still require product-level disclosure and minimization—especially APNs previews and sautai’s full prompt—but they are not the source of the placeholder-space collapse.

---

# 5. Closure options

## A. Value-aware re-redaction at reply storage

**Mechanism**

Before storing final or partial assistant output, replace every known tenant entity value with its canonical placeholder. Apply at:

- `_store_ios_turn_reply`.
- `_clean_assistant_text_for_app`.
- `ConversationTurn` capture.
- `ProactiveOutbound`.
- Partial/stream checkpoints.
- Any cron response excerpt that can later become context.

Reuse `redact_known_entities` or a dedicated deterministic value matcher. Compute redaction metadata after this step, then rehydrate only at owner delivery/read seams.

**Effort: S**

**Trade-offs**

- Excellent last-line defense against echoed values already present in `pii_entity_map`.
- Prevents plaintext reply persistence and stops recap/digest amplification.
- Does not prevent the initial OpenRouter exposure.
- Does not catch a previously unknown name unless that value is first registered.
- Running full NER/minting at this seam would catch more but risks false entity creation, latency, and junk-map growth.

This is worth doing immediately, but it is not sufficient alone.

## B. Redact on read for all model-bound tool results

**Mechanism**

Add one Django-side model-egress serializer/guard before local tool responses leave the control plane. Recursively transform every textual value for:

- Journal/document/daily/memory/context/search/query.
- Task, goal, purpose, reconcile.
- Fuel and finance.
- Lessons and insights.
- Friends mission data.
- Cron/automation/action summaries.

Do the same for direct background model/embedding calls; otherwise nightly extraction and session-start embeddings remain uncovered.

Prefer deterministic known-value replacement first. For unregistered entities, introduce a carefully bounded detection policy rather than blindly applying `MINT_ALL` to arbitrary structured output.

**Effort: M**

**Trade-offs**

- Smallest blast radius that directly closes the highest-impact cloud-egress and contamination gap.
- No immediate iOS/console/search migration.
- At-rest plaintext remains.
- Known-only mode misses names absent from the registry.
- Full detection on tool JSON introduces false positives and map-growth risks.
- Must preserve JSON structure and avoid redacting IDs, enum labels, amounts, dates, or tool control values.

Existing machinery that helps:

- `redact_tool_response` already handles recursive structures: `apps/pii/redactor.py:1430-1516`.
- Selected Google/Reddit paths demonstrate an integration pattern.
- Friends sharing provides a fail-closed pattern where privacy is stronger than availability.
- The model-context annotation utility already exists.

## C. Redact on write for Layer-1 authoring

**Mechanism**

Create one field-aware authoring service used by runtime tools, JWT/console endpoints, extraction approval, reconcile, and background writers. Persist user-content fields in placeholder-space; rehydrate only for authenticated owner presentation and deliberate external delivery.

**Effort: L**

**Concrete UX and operational costs**

- **Documents:** owner GET already rehydrates, but POST must be aligned with PATCH/append.
- **Tasks/goals:** owner APIs already have much of the rehydrate/redact machinery. Runtime paths need to converge on it.
- **Daily notes:** owner reads rehydrate, but all whole-note, section, entry, and runtime writes need transformation.
- **WeeklyReview, JournalEntry, NoteTemplate, memory, lessons, insights, fuel, and finance:** current client serializers generally return stored text directly. Redact-on-write would expose `[PERSON_n]` tokens in iOS/console until every read path is updated.
- **Search:** Postgres text/FTS would index placeholders. A query for “Theo” would not match `[PERSON_3]` unless owner queries are transformed into the tenant’s placeholder before searching. Existing task search uses raw `icontains`: `apps/journal/lifecycle_views.py:314-317`.
- **Nightly extraction:** stable placeholders can preserve cross-document identity, but prompts need annotations/identity legend and every source must use the same tenant map. Mixed old/new rows reduce extraction and reconciliation quality.
- **Finance:** account nicknames can be handled, but amounts and transaction semantics are outside the current PII taxonomy.
- **Migration:** requires tenant-by-tenant replacement, entity-map consistency, collision handling, FTS/chunk/embedding rebuilds, mirror regeneration, and validation that all owner reads rehydrate.
- **Encryption sidecars:** the existing dark columns do not substitute for this work.

Redact-on-write is the correct end state for the strongest privacy-at-rest posture, but it has the widest product blast radius.

## D. Hybrid sequence — recommended

I recommend:

1. **Immediate, S:** add the known-value reply/proactive/partial/capture storage backstop.
2. **Next, M:** add one model-egress guard for every local tool response plus every direct OpenRouter/embedding prompt.
3. **Then, L:** convert Layer-1 stores incrementally to placeholder-space at rest, starting with journal/daily/memory/tasks/goals/WeeklyReview, with owner rehydration, transformed search, and migration tooling delivered in the same tranche.
4. Follow with lessons/insights/meditation, then fuel/finance/actions/cron according to sensitivity and observed volume.

If only one option can be selected now, choose **redact-on-read/model-egress guarding**. It addresses the highest-impact privacy boundary and stops the tool-result contamination that is currently collapsing placeholder-space. The small reply-storage backstop should still ship alongside it if possible because it is cheap and prevents already-exposed values from becoming durable conversation state.

The Optiver document behavior should not be generalized as existing coverage. It is evidence that placeholder inheritance can work when the upstream context is clean; the rest of the audit shows why inheritance alone is not a dependable security boundary.
tokens used
621,176
# Ledger Snapshot

- Goal: map PII/redaction coverage across tenant-content storage, model context, and third-party egress.
- Status: audit complete; no files, databases, or external systems were modified.
- Source audited: `origin/main@41d742a4a14bbba1eb981ff9e58f564ee7d6d014`, the local ref containing merged PRs #1373, #1374, and #1375.
- Open question: the provenance of the specific Optiver project document is not recorded in code; its most likely creation path is identified below as **UNVERIFIED**.

Line references below are against that `origin/main` commit, not the older checked-out worktree.

## Executive finding

The PII layer is not a universal persistence or model-egress boundary. Coverage is path-dependent:

- Live Telegram/LINE/iOS model-bound messages are redacted and annotated before the main assistant call.
- Selected owner document/task/goal edits are redacted on write.
- Most runtime authoring tools—journal, daily notes, memory, tasks, goals, weekly reviews, fuel, finance, insights, lessons, cron—store and return text exactly as received.
- Local tool results are serialized directly into model context. They do not pass through `redact_tool_response`.
- Several background pipelines send raw Layer-1 content directly to OpenRouter or an embedding provider.
- Reply storage assumes the model stayed in placeholder-space. There is no value-aware re-redaction at the final storage seam.

That combination explains the observed failure:

```text
plaintext Layer-1 row
  → raw local tool result
  → OpenRouter receives real name
  → model echoes real name
  → reply storage accepts it unchanged
  → raw reply feeds later recap/digest
```

Legend:

- ✅ redactor applied at this seam.
- ⚠️ conditional, known-entity-only, inherited placeholder-space, or contract-only.
- ❌ no PII redactor; plaintext can pass.
- ↗ intentional rehydration for an owner-facing destination.

---

