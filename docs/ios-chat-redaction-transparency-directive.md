# DEVELOPER DIRECTIVE — iOS Chat Redaction Transparency + In-Chat Opt-Out

**Backend repo:** `/Users/michaeljones/Projects/nbhd-united` (Django + Next.js monorepo)
**iOS repo:** `/Users/michaeljones/Projects/nbhd-ios` (this directive is for you)
**Deploy target:** Container App `nbhd-django-westus2` (backend auto-deploys on merge to `main`)
**Backend contract:** ADDITIVE and backward-compatible. New optional JSON keys only — older app builds ignore them and keep working. There is no version gate to clear.

---

## 1. WHY WE'RE DOING THIS

The platform now redacts PII out of **inbound** chat before it reaches the tenant's AI assistant. When a user types "book the July trip to Sydney with Sarah," the assistant actually receives "book the [DATE_1] trip to [LOCATION_330] with [PERSON_1]." Real values are restored (rehydrated) before the owner reads the reply, so the chat bubbles still show real text — but the model reasoned over placeholders.

That redaction is invisible to the user, and it has three bad consequences today:

1. **The user can't tell a value was hidden.** The bubble reads normally, but the assistant never saw "Sydney." When the assistant asks a clarifying question or gives a vague answer, it looks like the assistant is being dense — the user has no idea a value was withheld.
2. **Conversations degrade.** The model sometimes treats `[LOCATION_330]` as a broken template variable and asks the user to "provide the real location," or its reply reads awkwardly around a placeholder. Without a signal, this is baffling.
3. **There is no way to opt a value out from the conversation.** The opt-out (denylist) lives only in web Settings → People. A user chatting on iOS who wants their assistant to actually see "Sydney" has no in-context control.

This directive adds a subtle transparency indicator on affected chat bubbles plus a per-value in-chat opt-out. The backend half (the new metadata fields + the journal fix) ships from `nbhd-united`; **your job is the iOS presentation and the opt-out calls.**

> Full background on the redaction model: `docs/pii-redaction-security.md` in the backend repo (see the "Inbound user messages are redacted" and "Per-message redaction metadata" sections).

---

## 2. THE BACKEND CONTRACT (additive)

Chat message payloads now carry two **optional** fields describing what was obfuscated on that turn. They appear on both surfaces the app already reads:

- the `?since=` feed rows — `GET /api/v1/chat/messages/?since=<cursor>`
- the per-message serializer — `GET /api/v1/chat/messages/<client_msg_id>/` and `GET /api/v1/chat/threads/<uuid>/messages/`

```jsonc
{
  "id": 4821,
  "client_msg_id": "…",
  "role": "user",                 // or "assistant"
  "text": "book the July trip to Sydney with Sarah",   // real values, already rehydrated
  "created_at": "…",
  "source": "app",
  "thread_id": "…",

  // NEW — both optional, either may be null/absent:
  "user_redactions": [
    {"placeholder": "[DATE_1]",      "value": "July"},
    {"placeholder": "[LOCATION_330]","value": "Sydney"},
    {"placeholder": "[PERSON_1]",    "value": "Sarah"}
  ],
  "reply_redactions": null
}
```

Rules of the contract:

- **`user_redactions`** = placeholders that were substituted out of *the user's own message* before it reached the assistant. Attach your indicator to **user** bubbles from this.
- **`reply_redactions`** = placeholders the *assistant's reply* contained (in placeholder space) before Django rehydrated it. Attach to **assistant** bubbles from this. (Often null — most replies don't reference redacted entities.)
- **`null` or absent = nothing was obfuscated on that turn.** Render the bubble exactly as today. Do not show any indicator. Treat missing and null identically.
- **`value` is the real string** — the same text already visible in the rendered `text`. It exposes no new PII (same tenant, same JWT; the owner already sees it in the bubble). This is what makes an in-chat "stop hiding this" affordance safe.
- **`placeholder`** is the opaque token the model saw (`[LOCATION_330]`). You need it only if you drive the binding-deletion variant of the opt-out (§4); the denylist opt-out uses `value` alone.

Parse defensively: unknown keys today, and these keys are optional forever. Decode with `decodeIfPresent`; a decode failure on these fields must never break message rendering.

---

## 3. RECOMMENDED UX

Keep it subtle — this is a reassurance signal, not an error. Two layers:

**On the bubble (passive indicator).** Pick one:
- **Dotted underline** under each affected substring. Find occurrences of each `value` inside the bubble's `text` and underline them (dotted, low-contrast). Communicates "these exact words were kept from your assistant" precisely.
- **Footnote chip** below the bubble: a small, muted pill reading e.g. `2 values hidden from your assistant` (count = distinct entries). Cheaper to build than substring ranging; less precise. Acceptable fallback where substring highlighting is awkward.

Respect `prefers-reduced-motion` / accessibility: no animation, 4.5:1 contrast on any text, 44×44pt tap target on the chip.

**On tap (the sheet).** Tapping the underline/chip opens a sheet titled e.g. "Hidden from your assistant." It lists each entry as `value` (the real word) with a one-line explanation ("Your assistant saw a placeholder instead of this") and a per-row action:

> **[ Stop hiding this ]**

Tapping it calls the opt-out (§4), then optimistically removes the row and drops the indicator on future turns. Include a short footer explaining the tradeoff in plain language:

> "Stop hiding a value and your assistant will see it in new messages. Past messages stay as they are."

Offer a bulk "Stop hiding all" if the sheet lists several. Do not auto-opt-out; this is always a deliberate user choice.

---

## 4. THE OPT-OUT ENDPOINTS + SEMANTICS

There are two distinct server operations with **different consequences**. Get this right — they are not interchangeable.

### 4a. Denylist the value — the correct "Stop hiding this" action

```
POST /api/v1/tenants/settings/pii-denylist/
Authorization: Bearer <the JWT the app already holds>
Content-Type: application/json

{ "name": "Sydney" }        // the `value` from the metadata entry
```

Bulk variant (for "Stop hiding all"):

```
POST /api/v1/tenants/settings/pii-denylist/bulk/
{ "names": ["Sydney", "July", "Sarah"] }
```

**What it does:** adds the value to the tenant's denylist. From then on the detector still *fires* on that word but the hit is discarded before it can drive a substitution or mint — so the value flows through to the assistant in the clear on **future** messages. The existing `pii_entity_map` binding is left untouched, so **historical rehydration keeps working** — old messages and journal entries that still contain `[LOCATION_330]` continue to render "Sydney" correctly.

**This is the recommended action behind "Stop hiding this."** It is the safe, reversible-in-spirit choice.

### 4b. Delete the binding — optional cleanup, NOT a substitute

```
POST /api/v1/tenants/settings/entity-registry/bulk/
{ "placeholders": ["[LOCATION_330]"], "deny": true }
```

> ⚠️ This bulk endpoint is being added in the same backend change set (it did not exist before). Confirm its exact request/response shape against the merged backend PR before wiring it — do not ship against an unmerged contract. A single-binding delete already exists at `DELETE /api/v1/tenants/settings/entity-registry/<placeholder>/` if you need a fallback.

**What it does and why it's dangerous alone:**
- Deleting the binding **breaks historical rehydration**. Any already-stored message, task title, or journal entry still containing `[LOCATION_330]` will now surface the raw placeholder `[LOCATION_330]` to the owner instead of "Sydney" — because an unknown placeholder passes through verbatim.
- Deletion **does not** stop future redaction on its own. The NER model still detects "Sydney" next time and mints a *fresh* placeholder under a new number. So deletion without denylisting just renames the obfuscation and orphans old references.

That is why the delete variant must always carry `deny: true` (denylist + delete together). **Prefer 4a (denylist only).** Reach for 4b only if the product decision is to also purge the stored mapping — and accept that old placeholders in history may go raw.

**Plain-language summary to bake into the sheet copy / your own understanding:**
- Denylist = "stop hiding this from now on" (past stays intact). ← default.
- Binding deletion = "forget this mapping entirely" (can make old messages show `[LOCATION_330]`). ← advanced/destructive.

---

## 5. THE JOURNAL PLACEHOLDER BUG IS SERVER-SIDE — NO iOS CHANGE

A related symptom — task/goal titles and daily recaps showing raw `[LOCATION_330]` in the Journal / Horizons views — is fixed **entirely on the backend**, at the same Document endpoints the app already calls:

- `GET /api/v1/journal/tree/`, `GET /api/v1/journal/documents/<kind>/<slug>/`, `GET /api/v1/journal/status/`

The server now rehydrates `markdown` and task/goal titles at the owner-facing serving boundary, and re-redacts owner edits on write. **You do not need to change `JournalViewModel`, `RemoteJournalTools`, `HorizonsViewModel`, or anything else** — once the backend deploys, the same responses simply come back with real values. Just verify visually that the placeholders are gone after the backend ships.

---

## 6. TESTING NOTES

**Fabricate a redacted turn on a dev tenant** (so the metadata fields populate):

1. Sign in to a dev tenant in the app (or drive the API directly with its JWT).
2. Send a chat message through `POST /api/v1/chat/messages/` whose body contains obvious PII the detector will catch — a full name + a city, e.g. `"remind me to call Sarah Chen about the Sydney trip in July"`. The DeBERTa model fires on `PERSON`, `LOCATION`, and `DATE_OF_BIRTH`-style spans; a first+last name and a city are reliable triggers.
3. Read it back via `GET /api/v1/chat/messages/?since=0` (or the thread endpoint). The row should now include `user_redactions` with entries like `{"placeholder":"[PERSON_1]","value":"Sarah Chen"}` and `{"placeholder":"[LOCATION_1]","value":"Sydney"}`. `text` still shows the real words.
4. To exercise `reply_redactions`, have the assistant reply referencing a previously-bound entity (ask a follow-up like "when is that Sydney trip?"); the reply row should carry `reply_redactions` for any placeholder the model emitted.

**Verify the opt-out round-trip:**
- POST `/api/v1/tenants/settings/pii-denylist/` `{"name":"Sydney"}` → expect `200`/`201`. Then send a *new* message containing "Sydney" and confirm the new row's `user_redactions` no longer lists Sydney (it now reaches the assistant in the clear). Older rows are unchanged.
- Confirm the value also appears in web Settings → People denylist (same store), proving parity.

**Backward-compat check:** point an older app build (or a client that ignores the new keys) at the same tenant — chat must render identically, with no crash and no indicator. Decode-failure on `user_redactions`/`reply_redactions` must be non-fatal.

**Endpoints summary:**

| Purpose | Method + path | Auth |
|---|---|---|
| Send message (triggers redaction) | `POST /api/v1/chat/messages/` | tenant JWT |
| Feed with metadata | `GET /api/v1/chat/messages/?since=<cursor>` | tenant JWT |
| Per-message / thread with metadata | `GET /api/v1/chat/messages/<client_msg_id>/`, `GET /api/v1/chat/threads/<uuid>/messages/` | tenant JWT |
| Opt out one value (recommended) | `POST /api/v1/tenants/settings/pii-denylist/` `{"name": value}` | tenant JWT |
| Opt out several values | `POST /api/v1/tenants/settings/pii-denylist/bulk/` `{"names": [...]}` | tenant JWT |
| Delete + deny binding (advanced) | `POST /api/v1/tenants/settings/entity-registry/bulk/` `{"placeholders": [...], "deny": true}` | tenant JWT — CONFIRM shape against merged backend PR |

---

## 7. RISKS / OPEN QUESTIONS

- **`entity-registry/bulk/` contract is new.** Its exact request/response shape is being finalized in the backend change set. Do not build the binding-deletion path against an unmerged contract; the denylist opt-out (§4a) is stable and sufficient for the core UX.
- **Substring highlighting vs. footnote chip.** Substring underlining is more precise but needs careful range-finding (a `value` may appear multiple times, or as a case/whitespace variant of what's in `text`). If ranging is fragile, ship the footnote chip first and iterate.
- **Fail-open redaction.** If detection errors on a turn, the message reaches the model unredacted and `user_redactions` is null — so the absence of an indicator does not *guarantee* nothing was hidden, only that nothing was recorded. This is a rare transient case; do not over-promise in copy ("hidden from your assistant" describes what we recorded, not a security guarantee).
- **Coalesced replies.** The backend attaches a combined reply (and its `reply_redactions`) to one representative row when it coalesces a burst of user turns; sibling rows come back with empty reply text. Don't assume every assistant row carries its own `reply_redactions`.

---

## 8. PHASE 2 — ON-DEVICE JUNK REVIEW (APPLE FOUNDATION MODELS)

### 8.1 Why this exists

The redactor over-mints. The DeBERTa detector, run over text it was never
trained on (the assistant's own markdown notes, raw email bodies, financial
jargon), tags huge amounts of junk as PII — a production audit found **979 of
1,103 bindings on the canary tenant were junk** (structure fragments, invisible
characters, common words, unvalidated financial labels). Those junk bindings
bloat the People list and drive nonsense placeholders.

The backend used to prune this with an hourly cron that **shipped the flagged
span text to Claude Haiku in the cloud** to judge each one. **That cloud
round-trip is retired** — no PII value leaves the platform anymore except to the
owner's own authenticated clients (this app). Deterministic backend hygiene now
cleans the unambiguous junk automatically; what's left is the *judgment* class —
plausible-looking names and places a rule can't rule on. That judgment moves to
**this app**, run **on-device** via Apple Foundation Models, with the owner in
the loop. Full backend design: `docs/pii-self-cleaning.md`.

Your job: fetch the review queue, classify it on-device, and present a one-tap
"clean up" digest. **Never delete silently.**

### 8.2 The backend contract (all JWT-scoped, all `/api/v1/tenants/settings/`)

| Purpose | Method + path | Body | Effect |
|---|---|---|---|
| Fetch review candidates | `GET pii-review-queue/` | — | Returns PERSON/LOCATION bindings that survived deterministic hygiene and aren't yet reviewed |
| Keep — "this IS PII" | `POST pii-review-queue/keep/` | `{"placeholders": ["[PERSON_1]", …]}` | Marks bindings reviewed-and-real; they drop out of the queue permanently. Mapping untouched, redaction continues |
| Clean — "this is junk" | `POST entity-registry/bulk/` | `{"placeholders": […], "deny": true}` | **Existing endpoint** (see §4b). Denylists + deletes the bindings. This is the "clean up" action |

Queue row shape (confirm against the merged backend PR before wiring — the
review-queue endpoints are new in this change set):

```jsonc
{
  "placeholder": "[PERSON_1]",     // opaque token the model saw
  "value": "Sarah Chen",           // real span — safe: same tenant, same JWT, already visible elsewhere
  "entity_type": "PERSON"          // PERSON | LOCATION
}
```

`value` carries no new PII (same rationale as §2 — the owner already sees their
own data on JWT-scoped surfaces). That is what makes on-device classification and
a review card safe: you are reasoning over data the owner owns, never sending it
anywhere.

> **`keep` vs. `clean` map 1:1 onto the retired arbiter's two outcomes**
> (`is_pii=true` → keep, `is_pii=false` → clean). You are replacing a cloud LLM's
> verdict with an on-device model's *proposal* plus the owner's tap.

### 8.3 Recommended flow — availability-gated FM classification

1. **Gate on availability.** Foundation Models is only present on capable
   devices/OS. Check `SystemLanguageModel.default.availability` first. If it is
   anything other than `.available`, **skip auto-classification entirely** and
   fall back to the plain review-card UI (§8.4). Never block the feature on FM.
2. **Classify on-device.** For each queue row, ask the on-device model whether the
   span is a real personal name/place worth hiding, or junk (brand, common word,
   fragment). Use **guided generation** (`@Generable`) so the output is a typed
   verdict, not free text you have to parse. Batch in **small groups** (see §8.5).
3. **Propose, don't apply.** Collect the spans the model judged junk into a
   **one-tap digest**: a single muted banner/card reading e.g.
   **"12 junk bindings found — Clean up"**. Tapping it (a) optionally expands the
   list so the owner can deselect any the model got wrong, then (b) calls the
   **clean** endpoint (`entity-registry/bulk/`, `deny: true`) for the confirmed
   set. Spans the model judged *real* need no action — they simply stay hidden;
   optionally auto-`keep/` them so they stop reappearing in the queue.
4. **Never auto-delete.** The model's verdict is a proposal. A wrongly-deleted
   real binding breaks historical rehydration (old messages/journal entries go
   raw — see §4b). Cleanup is always **propose + one owner tap**, never silent.

### 8.4 Fallback — review card (FM unavailable or owner prefers manual)

When FM is unavailable, present the queue as a simple review list: each row shows
`value` + `entity_type`, with per-row **Keep** / **Clean** and a bulk
**"Clean all selected"**. This is the same two endpoints, just without the
model pre-sorting. It must work with zero FM dependency.

### 8.5 The classification prompt (adapt the retired arbiter's rules)

The backend arbiter's system prompt is the validated rule set — port it as your
FM instructions. Keep it **span-only** (no surrounding message context; you are
judging the word itself for a single user):

- Real first names, nicknames, surnames, full names → **keep hiding** (is PII).
- Specific places that identify a person — city, neighborhood, address, employer
  name → **keep hiding**.
- Common English words / noun labels ("goal", "calendar", "wins", "tracker",
  "session") → **clean** (not PII).
- App / brand / product names ("Spotify", "OpenAI", "ChatGPT") → **clean**.
- Bar / restaurant / venue names ("Eleven Madison Park") → **clean**.
- Month / weekday names, exercise/gym jargon ("deadlift", "Pallof") → **clean**.
- Generic geographic terms ("home", "office", "the gym") → **clean**.
- Emoji, punctuation, obvious fragments → **clean**.
- **When in doubt, keep hiding.** A false "keep" is harmless (stays redacted); a
  false "clean" leaks a real value to the assistant. Bias the prompt toward
  keeping.

### 8.6 Validation requirement — gate auto-apply on offline scoring

**Before you let the FM verdict drive any cleanup without per-row confirmation**
(i.e. before trusting the "Clean up" one-tap over the fully-expanded manual
review), score your FM prompt **offline against the labeled cleanup set** the
backend preserved: the **1,103 audited bindings in backup table
`pii_map_backup_20260709`** (124 real PII, 979 junk). Get that set from the
backend owner as a fixture; run your prompt over it; measure the false-clean rate
(real PII the model called junk). Auto-apply is acceptable only when false-cleans
are ~zero. Until then, always expand the list for explicit owner review before
calling `entity-registry/bulk/`. Do not ship auto-apply against an unmeasured
prompt.

### 8.7 Swift-level notes

- **Availability check** — branch on `SystemLanguageModel.default.availability`;
  only proceed when `.available`. Surface nothing about "AI" in the UI when
  unavailable; just render the §8.4 fallback.
- **`@Generable` verdict struct** — define a small guided-generation type so the
  model returns typed output, e.g.:

  ```swift
  @Generable
  struct SpanVerdict {
      @Guide(description: "true if this span is a real personal name or a place that identifies the user")
      let isPII: Bool
      @Guide(description: "one of: name, place, brand, common_word, fragment, other")
      let reason: String
  }
  ```

  Request one `SpanVerdict` per span via `LanguageModelSession.respond(to:generating:)`.
- **Small batches** — classify a handful of spans per session call (e.g. 5–10),
  not the whole queue at once: keeps each prompt short, latency low, and avoids
  the model conflating spans. Reuse one `LanguageModelSession` across batches;
  the instructions (the §8.5 rules) go in the session's `instructions`, the spans
  in the per-call prompt.
- **Everything stays on-device.** No queue value is sent off the phone. This is
  the whole point of moving the judge from Haiku to Foundation Models.

### 8.8 Endpoints summary (Phase 2)

| Purpose | Method + path | Auth |
|---|---|---|
| Fetch review queue | `GET /api/v1/tenants/settings/pii-review-queue/` | tenant JWT — CONFIRM shape against merged backend PR |
| Keep (is real PII) | `POST /api/v1/tenants/settings/pii-review-queue/keep/` `{"placeholders": […]}` | tenant JWT — CONFIRM shape |
| Clean (junk: delete + deny) | `POST /api/v1/tenants/settings/entity-registry/bulk/` `{"placeholders": […], "deny": true}` | tenant JWT (existing, §4b) |