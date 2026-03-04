# LINE Messaging API — Platform Capabilities for Phase 2

Research completed 2026-03-04. Source: LINE Developers docs.

## What LINE Can Do (That We Should Use)

### 1. Flex Messages — The Big One
LINE's equivalent of rich cards, built on CSS Flexbox. **This is what makes LINE bots feel native vs. feeling like SMS.**

- **Bubble**: single card with header, hero (image), body, footer
- **Carousel**: horizontal scroll of multiple bubbles
- Fully customizable: colors, fonts, sizing, layout, buttons
- Supports images, icons, text, buttons, separators
- **Simulator**: https://developers.line.biz/flex-simulator/ (test layouts before shipping)

**Use cases for NBHD:**
- Morning briefing → Flex card with sections (weather, calendar, news)
- Journal entry confirmation → Bubble with approve/dismiss buttons
- Lesson constellation approvals → Cards with lesson text + approve/dismiss
- Cron job notifications → Structured cards instead of plain text

### 2. Quick Reply Buttons
Horizontal button bar at the bottom of chat. Up to 13 buttons. Disappears after tapping or when a new message arrives.

- **Actions available**: postback, message, URI, datetime picker, camera, camera roll, location
- Can include icons on each button
- Perfect for in-flow choices

**Use cases for NBHD:**
- After agent asks a question → quick reply options
- "How was your day?" → mood buttons (Great / OK / Rough)
- After journal prompt → "Write more" / "Save" / "Skip"
- Confirmation flows → "Yes" / "No" / "Later"

### 3. Rich Menus — Persistent Bottom Menu
Always-visible tappable image at bottom of chat (like a keyboard toolbar). Users can toggle it open/closed.

- Up to 20 tap areas on one image
- Can be set per-user (different menu for different states)
- Supports: URI, postback, message, rich menu switch
- **Can switch between menus** → e.g., "Main menu" ↔ "Settings menu"

**Use cases for NBHD:**
- Default menu: "Journal" / "Today" / "Goals" / "Settings"
- Context-aware: different menu after onboarding vs. daily use
- Quick access to common agent commands without typing

### 4. Loading Animation
Show typing/thinking indicator while agent processes. Auto-clears when response arrives or after N seconds (5-60).

- `POST /v2/bot/chat/loading/start` with `loadingSeconds`
- Only shows when user is actively viewing the chat
- **Critical for AI agents** — LLM responses can take 3-10 seconds

### 5. Text Message v2 — Mentions & Emoji
Enhanced text messages with substitution variables for mentions and LINE emoji.

- Can embed LINE-native emoji (not just Unicode)
- Can mention users by ID

### 6. Media Messages
- **Image**: original + preview URLs (HTTPS required)
- **Video**: video URL + preview image
- **Audio**: audio URL + duration
- **Location**: title, address, lat/lng → opens in Maps
- **Stickers**: from LINE's sticker library (package ID + sticker ID)

### 7. Template Messages (Simpler than Flex)
Pre-built layouts for common patterns:
- **Buttons**: image + title + text + up to 4 action buttons
- **Confirm**: text + 2 buttons (yes/no)
- **Carousel**: horizontal scroll of button cards
- **Image carousel**: horizontal scroll of images with actions

### 8. Postback Actions
Silent data sent to server when user taps a button. Key features:
- `data` field: arbitrary string (we use this for `extract:approve_lesson:abc123`)
- Optional `displayText`: what appears in chat when tapped
- Can control UI: close rich menu, open keyboard, open voice input
- **Most important action type for bot interactions**

### 9. User Media Handling
Bot can receive and retrieve:
- Images, video, audio, files sent by users
- Content auto-deleted after some time
- `GET /v2/bot/message/{messageId}/content` to download

## What LINE Cannot Do

- **No inline keyboards on regular text messages** — only on templates, Flex, or as quick replies
- **No markdown in text messages** — LINE renders plain text only (our Phase 1 stripping is correct)
- **No message editing** — once sent, can't modify
- **No message deletion by bot** — bot can't unsend
- **No reactions by bot** — only users can react
- **Quick replies disappear** — after any new message (ours or theirs)
- **Rich menu is global per bot** (or per-user, but requires API calls to switch)
- **5 message objects max per API call** — batch carefully
- **Flex rendering varies** by device, OS, LINE version, resolution

## Message Limits & Pricing

- **Free plan**: 200 free messages/month (push messages, not replies)
- **Reply messages**: unlimited and free (use replyToken)
- **Push messages**: count toward monthly limit
- **Loading animation**: free, no limit

**Key insight**: Reply messages are free. If we respond using replyToken (within 1 min of receiving), it doesn't count. Only proactive pushes (cron, alerts) count toward the 200 limit.

→ We're using Push API everywhere right now. **Phase 2 should use Reply API when responding to user messages** to stay within free tier.

## Phase 2 Design Recommendations

### Priority 1: Loading Animation
Lowest effort, highest UX impact. Show "typing..." while agent thinks.
- Trigger immediately when webhook received
- Set to 20 seconds (covers most LLM calls)
- Auto-clears when our response arrives

### Priority 2: Reply API for Responses
Switch from Push API to Reply API for direct responses.
- Saves push message quota
- replyToken valid for ~1 minute
- Fall back to Push if token expired

### Priority 3: Flex Messages for Structured Content
Transform agent responses with structure into Flex bubbles:
- Morning briefings → multi-section Flex card
- Lists (calendar events, tasks) → structured layout
- Confirmations → Confirm template or Flex with buttons
- Agent needs to signal "this is structured" so webhook knows to format as Flex

### Priority 4: Quick Replies for Agent Questions
When agent asks a question with known options:
- Journal mood prompts → quick reply buttons
- Yes/No confirmations → quick reply
- Lesson approval → postback buttons
- Agent needs to include option metadata so webhook can attach quick replies

### Priority 5: Rich Menu
Design a persistent menu for common actions:
- "Today's briefing" / "Journal" / "My goals" / "Settings"
- Requires image design (2500×1686 or 2500×843 pixels)
- Can be updated per-user based on subscription tier

### Priority 6: Stickers for Personality
LINE bots that use stickers feel native. An agent that sends a congratulations sticker after completing a goal feels more human in the LINE context.

## Technical Constraints to Remember

1. **1-second webhook response** — must return 200 immediately, process async
2. **replyToken expires** — use it fast or fall back to Push
3. **5 messages per API call** — batch wisely
4. **Flex JSON can get large** — keep bubbles simple
5. **No markdown** — all formatting must be in Flex structure or plain text
6. **Per-user rich menus** require API calls per user on link
7. **Loading animation** only shows when user is on chat screen
