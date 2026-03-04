# LINE Messaging Channel — Design Spec

**Status:** Draft
**Author:** The Claw + MJ
**Date:** 2026-03-04

## Overview

Add LINE as a second messaging channel for NBHD United subscribers, alongside Telegram. One NBHD account can link one or both channels. The tenant's OpenClaw container is channel-agnostic — it receives the same `/v1/chat/completions` call regardless of source.

## Architecture

```
LINE User                    Telegram User
    │                              │
    ▼                              ▼
LINE Platform                Telegram Bot API
(webhook POST)               (long-poll getUpdates)
    │                              │
    ▼                              ▼
┌─────────────────────────────────────────────┐
│            Django (Central Router)           │
│                                             │
│  LineWebhookView ──┐    ┌── TelegramPoller  │
│                    ▼    ▼                   │
│              resolve_tenant()               │
│                    │                        │
│          forward_to_container()             │
│                    │                        │
│              relay_response()               │
│              ┌─────┴─────┐                  │
│              ▼           ▼                  │
│        LINE Reply    Telegram Reply         │
└─────────────────────────────────────────────┘
                     │
                     ▼
            Tenant OpenClaw Container
            /v1/chat/completions
            (channel-agnostic)
```

## Data Model Changes

### User model additions

```python
# apps/tenants/models.py — User
line_user_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
line_display_name = models.CharField(max_length=255, blank=True, default="")
preferred_channel = models.CharField(
    max_length=16,
    choices=[("telegram", "Telegram"), ("line", "LINE")],
    default="telegram",
    help_text="Primary channel for proactive messages (cron, alerts).",
)
```

### New model: LineLinkToken

Mirror of `TelegramLinkToken` — same flow, different platform.

```python
# apps/tenants/line_models.py
class LineLinkToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user = models.ForeignKey("tenants.User", on_delete=models.CASCADE, related_name="line_link_tokens")
    token = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
```

## LINE Bot Setup

1. **LINE Developers Console** → Create a Messaging API channel
2. Get **Channel Access Token** (long-lived) and **Channel Secret** (for signature verification)
3. Store in Key Vault: `line-channel-access-token`, `line-channel-secret`
4. Set webhook URL: `https://nbhd-django-westus2.{domain}/api/v1/line/webhook/`

### Django Settings

```python
LINE_CHANNEL_ACCESS_TOKEN = read_key_vault_secret("line-channel-access-token")
LINE_CHANNEL_SECRET = read_key_vault_secret("line-channel-secret")
```

## Endpoints

### Webhook Receiver

```
POST /api/v1/line/webhook/
```

- Validates LINE signature (X-Line-Signature header using HMAC-SHA256 of body + channel secret)
- Parses events array from body
- Routes each event to the appropriate handler

### Account Linking

```
POST /api/v1/line/link/generate/          → Generate link token + LINE deep link
GET  /api/v1/line/link/status/            → Check if LINE is linked
DELETE /api/v1/line/link/                  → Unlink LINE account
```

## Webhook Event Handling

### Event Types to Handle (Phase 1)

| LINE Event | Action |
|---|---|
| `follow` | User added bot. Send welcome + linking instructions |
| `message` (text) | Route to tenant container → relay response |
| `message` (image/audio/video) | Download media → forward as attachment |
| `postback` | Handle inline button callbacks (extraction approval, etc.) |
| `unfollow` | Clear `line_user_id`, log |

### Webhook View (Pseudocode)

```python
class LineWebhookView(View):
    def post(self, request):
        # 1. Verify signature
        signature = request.headers.get("X-Line-Signature")
        if not verify_signature(request.body, signature):
            return HttpResponse(status=403)
        
        # 2. Parse events
        body = json.loads(request.body)
        events = body.get("events", [])
        
        # 3. Handle each event
        for event in events:
            event_type = event.get("type")
            if event_type == "message":
                self._handle_message(event)
            elif event_type == "follow":
                self._handle_follow(event)
            elif event_type == "postback":
                self._handle_postback(event)
        
        return HttpResponse(status=200)  # LINE requires 200 within 1 second
```

**Important:** LINE requires a 200 response within 1 second. All heavy processing (forwarding to container, waiting for AI response) must happen asynchronously. Use `reply_token` for immediate responses, `push message` API for async replies.

### Async Response Pattern

```
1. LINE webhook arrives → immediately return 200
2. Async (thread/task): forward to container → get AI response
3. Use LINE Push Message API to send response back
   (reply_token expires after ~30 seconds, so push is safer)
```

This is actually simpler than Telegram's approach of blocking in the poll loop.

## User Linking Flow

### Option A: Deep Link (Primary)

1. User signs up on web → clicks "Connect LINE"
2. Backend generates `LineLinkToken`, returns LINE deep link:
   `https://line.me/R/oaMessage/@nbhd-united/?link_TOKENHERE`
3. User taps link → LINE opens → bot receives message containing the token
4. Django validates token → links `line_user_id` to User
5. Confirmation reply via LINE

### Option B: QR Code

Same as above but rendered as QR code on the web settings page.
LINE app can scan QR codes natively.

### Option C: In-LINE Registration (Future)

Bot greets new follower → collects email → creates account → links automatically.
Deferred — requires handling payment in-LINE which is complex.

## Outbound Message Routing

### CronDeliveryView Changes

The `nbhd_send_to_user` plugin calls `CronDeliveryView`. Modify to be channel-aware:

```python
class CronDeliveryView(APIView):
    def post(self, request, tenant_id):
        # ... existing auth/validation ...
        
        user = tenant.user
        channel = self._resolve_channel(user)
        
        if channel == "line":
            return self._send_via_line(user.line_user_id, message_text)
        else:
            return self._send_via_telegram(user.telegram_chat_id, message_text)
    
    def _resolve_channel(self, user):
        """Determine which channel to use for outbound messages."""
        # User's preferred channel if set and linked
        if user.preferred_channel == "line" and user.line_user_id:
            return "line"
        if user.preferred_channel == "telegram" and user.telegram_chat_id:
            return "telegram"
        # Fallback: whichever is linked
        if user.line_user_id:
            return "line"
        if user.telegram_chat_id:
            return "telegram"
        return None
```

### LINE Message Sending

```python
def _send_via_line(self, line_user_id, text):
    """Send via LINE Push Message API."""
    resp = httpx.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "to": line_user_id,
            "messages": [{"type": "text", "text": text}],
        },
    )
```

### Message Formatting

| Feature | Telegram | LINE |
|---|---|---|
| Bold | `*bold*` or `**bold**` | Not supported in text (use Flex) |
| Links | `[text](url)` | Auto-linked URLs |
| Inline buttons | `inline_keyboard` | `quick_reply` or `template` messages |
| Rich cards | Not native | **Flex Messages** (JSON-based, very powerful) |
| Max message length | 4,096 chars | 5,000 chars |
| Markdown | Supported | **Not supported** |

**Phase 1:** Strip markdown → plain text. Good enough for launch.
**Phase 2:** Convert structured responses to Flex Messages (morning briefing as a card, etc.)

## Extraction Callbacks (Inline Buttons)

Currently uses Telegram `inline_keyboard` for lesson/goal/task approval. LINE equivalent:

```python
# LINE Quick Reply
{
    "type": "text",
    "text": '💡 Something worth remembering:\n"Lesson text here"',
    "quickReply": {
        "items": [
            {
                "type": "action",
                "action": {
                    "type": "postback",
                    "label": "✅ Add to constellation",
                    "data": "extract:approve_lesson:UUID"
                }
            },
            {
                "type": "action",
                "action": {
                    "type": "postback",
                    "label": "❌ Skip",
                    "data": "extract:dismiss:UUID"
                }
            }
        ]
    }
}
```

The `extraction_callbacks.py` handler needs to accept both Telegram callback_query format and LINE postback format. Abstract the callback handling behind a shared interface.

## Config Generator Changes

OpenClaw channel config for tenant containers:

```python
"channels": {
    "telegram": {"enabled": True, "capabilities": ["inlineButtons"]},
    "line": {"enabled": True, "capabilities": ["inlineButtons"]},
},
```

No bot tokens in the container — same as Telegram. The container just knows which surfaces it can use.

## Frontend Changes

Settings page additions:
- "Connect LINE" button (alongside existing "Connect Telegram")
- QR code display for LINE linking
- Preferred channel toggle (Telegram / LINE)
- Unlink button for each channel

## Implementation Phases

### Phase 1: MVP (~2-3 days)

- [ ] User model: add `line_user_id`, `line_display_name`, `preferred_channel`
- [ ] `LineLinkToken` model + migration
- [ ] LINE webhook view with signature verification
- [ ] Handle `follow`, `message` (text), `unfollow` events
- [ ] Account linking flow (deep link)
- [ ] Forward messages to container (async pattern)
- [ ] Relay AI responses via Push Message API (plain text)
- [ ] Modify `CronDeliveryView` for channel-aware routing
- [ ] Django settings + Key Vault secrets for LINE credentials
- [ ] LINE Bot setup in LINE Developers Console

### Phase 2: Polish (~2 days)

- [ ] Flex Messages for morning briefings / structured content
- [ ] Quick Reply buttons for extraction callbacks (lessons, goals, tasks)
- [ ] Media forwarding (images, audio → container)
- [ ] Voice message transcription (LINE audio → Whisper)
- [ ] Frontend: Connect LINE UI, preferred channel toggle
- [ ] Postback handler for inline button callbacks

### Phase 3: Native Features (future)

- [ ] LINE Rich Menu (persistent bottom menu with common actions)
- [ ] LINE Flex Message templates for calendar events, task lists
- [ ] LINE LIFF (LINE Frontend Framework) for mini web apps inside LINE
- [ ] In-LINE registration (no web signup needed)

## LINE API Costs

- **Free tier:** 200 push messages/month (per bot)
- **Light plan:** ¥5,000/mo (~$33) for 5,000 messages
- **Standard plan:** ¥15,000/mo (~$100) for 30,000 messages

At early scale (5-20 subscribers), free tier is tight. Light plan covers ~250 messages per subscriber per month. Factor into pricing.

## Security Considerations

- Always verify `X-Line-Signature` using HMAC-SHA256
- Channel Secret never exposed to tenant containers
- Rate limit LINE API calls (LINE has rate limits: 100,000/min for push)
- LINE user IDs are per-bot (not global) — tied to this specific bot

## Migration Path

No breaking changes. LINE is additive — existing Telegram users are unaffected. New users can choose either channel at signup.

## Decisions (Resolved)

1. **LINE Official Account name:** "Neighborhood United" (alias: "Hood United")
2. **Language detection:** Agent detects language naturally — no forced default. System messages (welcome, linking confirmations) should be bilingual (EN + JP).
3. **LINE Login:** Include if implementation is trivial. LINE Login provides OAuth2-based web auth — could replace email/password for JP users. Research effort needed on Django integration (django-allauth has LINE provider support).
4. **Pricing:** Light plan (¥5,000/mo, 5,000 messages) likely needed from day one. Factor into operating costs, not subscriber pricing.

## Sautai Reference Code

The Sautai repo (`neighborhood-united`) has LINE scaffolding:
- `chefs/services/sous_chef/agents_factory.py` — `@line/line-bot-mcp-server` npm package (LINE's official MCP server). Not needed for NBHD since routing is at Django layer.
- `chefs/services/sous_chef/tools/categories.py` — Channel-based tool restrictions (LINE = no UI navigation, sensitive tools wrapped). Same pattern applies: agent is channel-agnostic, Django handles routing.
- No actual webhook receiver or message handling implemented in Sautai — was planned but not built.

**Useful for reference:** The MCP server package (`@line/line-bot-mcp-server`) and the tool category pattern. Not directly portable but validates the architecture.
