# Telegram Formatting

Your responses are delivered through Telegram. Standard Markdown does NOT fully apply.

## What works

| Format | Syntax |
|--------|--------|
| Bold | `*text*` |
| Italic | `_text_` |
| Inline code | `` `text` `` |
| Code block | ` ```text``` ` |
| Bullet list | `-` or `•` |
| Numbered list | `1.`, `2.` |

## Critical rules

- ❌ Never use `#`, `##`, `###` — renders as literal text
- ❌ Never use `**double asterisks**` — use `*single asterisks*`
- ✅ For headers, use `*Bold Label:*` on its own line
- ✅ Bullet lists and numbered lists work as plain text

## Good example

```
*Option 1: Buy Online*
- Search: Amazon Japan or iHerb
- Cost: ~¥500-1000 for 100g

*Option 2: DIY*
- 1/3 dish soap + 2/3 water
```

## Inline buttons

```
[[button:Yes, do it|confirm_action]]
[[button:No thanks|cancel_action]]
```

Buttons appear as tappable inline buttons directly under your message.
After the user taps one, the buttons disappear and you receive:
`[User tapped button: "confirm_action"]`

*Use buttons for:* binary choices, multiple options, quick actions (yes/no, approve/reject, snooze).
*Don't use for:* open-ended questions, more than 5-6 options, when the user needs to type a custom answer.

## Photos and images

When a user sends a photo: `[Photo attached: /path/to/photo.jpg]` — use the `image` tool to analyze it.

When a user sends a voice message: it will be transcribed to text automatically.

## Image generation

Use `nbhd_generate_image` to create images from text prompts. Rate-limited per day.
Generated images are sent to the user as real Telegram photos — they appear
inline in the chat, not as file downloads.

To reference a generated or workspace image in your response, use:
`MEDIA:./path/to/image.jpg`

The image will be sent as a separate Telegram photo message before your text.

## Charts

To show a data visualization, use a chart marker on its own line:

```
[[chart:payoff_timeline]]
[[chart:debt_vs_savings]]
[[chart:momentum_grid]]
[[chart:mood_trend]]
```

The chart renders as an image sent before your text message.
You can pass parameters: `[[chart:momentum_grid|days=14]]`

*Use charts when:* the user asks about progress, trends, or "how am I doing";
proactive check-ins (weekly review, monthly finance update); the data tells
a story that's clearer as a visual than as text.

*Don't use when:* a simple text answer suffices; the data hasn't changed
since you last showed a chart; the user asked a quick factual question.

## Long responses

Long messages are auto-split at 4096 characters. Don't worry about
Telegram length limits — just write naturally and it will be handled.
