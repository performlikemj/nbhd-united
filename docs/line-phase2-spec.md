# LINE Phase 2 — Rich UX Spec

**Status:** In progress
**Branch:** `feat/line-phase2-ux`
**Date:** 2026-03-04

## Goals

Transform LINE from "plain text pipe" to a native-feeling LINE bot experience.
Every change must work alongside the existing Telegram path without breaking it.

## Implementation Order (by impact/effort ratio)

### Step 1: Loading Animation ⚡
**File:** `apps/router/line_webhook.py`

When we receive a message, immediately call the loading indicator API before
processing. This tells the user "I'm thinking" while the LLM runs.

```
POST https://api.line.me/v2/bot/chat/loading/start
Authorization: Bearer {token}
Content-Type: application/json

{"chatId": "<line_user_id>", "loadingSeconds": 20}
```

- Fire-and-forget (don't block on response)
- 20 seconds covers most LLM calls
- Auto-clears when our response message arrives
- Only for text messages forwarded to container (not link tokens, welcome msgs)

### Step 2: Reply API for Direct Responses 💰
**File:** `apps/router/line_webhook.py`

Reply messages are **free and unlimited**. Push messages cost against the 200/month
free tier. Switch to Reply API when responding to user messages.

- Capture `replyToken` from the webhook event
- Pass it through to `_forward_to_container`
- On response, try Reply API first:
  ```
  POST https://api.line.me/v2/bot/message/reply
  {"replyToken": "...", "messages": [...]}
  ```
- If replyToken expired (>1 min), fall back to Push API
- Cron/proactive messages still use Push (no replyToken available)

### Step 3: Flex Messages for Structured Responses 🎨
**File:** `apps/router/line_webhook.py`, new `apps/router/line_flex.py`

Convert agent responses that contain structure into Flex Messages.

#### Detection Rules
The webhook inspects the agent's response text and converts to Flex when:
1. **Headers detected** (`## Section` or `### Section`) → multi-section bubble
2. **Bullet lists** → structured body with separator lines
3. **Multiple paragraphs with distinct topics** → carousel of bubbles

#### Flex Templates

**Single bubble (default for structured text):**
```json
{
  "type": "flex",
  "altText": "Message from your assistant",
  "contents": {
    "type": "bubble",
    "body": {
      "type": "box",
      "layout": "vertical",
      "contents": [
        {"type": "text", "text": "Section Title", "weight": "bold", "size": "lg"},
        {"type": "separator", "margin": "md"},
        {"type": "text", "text": "Content here...", "wrap": true, "size": "sm", "color": "#666666"}
      ]
    }
  }
}
```

**Multi-section bubble (for briefings, reports):**
- Header block: title/greeting
- Body block: sections separated by `separator` components
- Footer block: action buttons if applicable

**Carousel (for lists of items):**
- Each item becomes a bubble
- Max 12 bubbles per carousel
- Each bubble: title, description, optional action button

#### Plain Text Fallback
- Short responses (<200 chars, no structure) → plain text (cheaper, faster)
- If Flex construction fails → fall back to plain text
- `altText` always set (shown in notifications and unsupported clients)

### Step 4: Quick Reply Buttons 🔘
**File:** `apps/router/line_webhook.py`

Attach quick reply buttons when the agent's response contains actionable options.

#### Detection
Look for patterns in agent response:
- Inline buttons: `[[button:label|callback_data]]` → quick reply with postback
- Numbered choices: lines starting with `1.`, `2.`, etc. at end of message
- Yes/No questions: detect question mark + common patterns

#### Implementation
Append `quickReply` object to the last message in the response:
```json
{
  "type": "text",
  "text": "How was your day?",
  "quickReply": {
    "items": [
      {"type": "action", "action": {"type": "postback", "label": "Great 😊", "data": "mood:great", "displayText": "Great 😊"}},
      {"type": "action", "action": {"type": "postback", "label": "OK 😐", "data": "mood:ok", "displayText": "OK 😐"}},
      {"type": "action", "action": {"type": "postback", "label": "Rough 😞", "data": "mood:rough", "displayText": "Rough 😞"}}
    ]
  }
}
```

- Max 13 quick reply buttons
- `displayText` shows in chat what user tapped
- `data` sent as postback to our webhook
- Only attach to last message in a multi-message response

### Step 5: Rich Menu (Phase 2b — separate PR)
Deferred to a follow-up. Requires image design and per-user menu management.

### Step 6: Stickers (Phase 2b — separate PR)
Deferred. Fun but lower priority than functional UX.

## Architecture

All LINE-specific formatting happens in the **Django webhook handler**, not in
the tenant containers. Tenant agents output standard text/markdown; Django
transforms it for the LINE surface. This keeps agents channel-agnostic.

```
User → LINE → Django webhook → forward plain text to container
Container → AI response (markdown) → Django webhook
Django webhook → detect structure → build Flex/QuickReply/text → LINE Push/Reply API
```

## Testing Strategy

1. **Unit tests** for each Flex template builder
2. **Unit tests** for structure detection (headers, lists, buttons)
3. **Unit tests** for Reply API with token expiry fallback
4. **Integration test** for loading animation (mock httpx)
5. **Edge cases**: empty response, very long response, malformed markdown,
   mixed structured + unstructured content
6. All existing Phase 1 tests must continue passing

## Rollout

- All changes behind the same webhook — no feature flags needed
- Plain text responses unchanged (short messages stay as text)
- Flex only activates for structured content
- Loading animation is unconditional (always better UX)
- Reply API is transparent (free tier savings, same user experience)
