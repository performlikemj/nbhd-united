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
```