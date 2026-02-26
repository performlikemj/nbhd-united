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

## Inline Buttons

```
[[button:Yes, do it|confirm_action]]
[[button:No thanks|cancel_action]]
```

When the user taps a button you receive: `[User tapped button: "confirm_action"]`

*Use buttons for:* binary choices, multiple options, quick actions (yes/no, approve/reject, snooze).
*Don't use for:* open-ended questions, more than 5-6 options, when the user needs to type a custom answer.

## Photos

When a user sends a photo: `[Photo attached: /path/to/photo.jpg]` — use the `image` tool to analyze it.

## Image Generation

Use `nbhd_generate_image` to create images from text prompts. Rate-limited per day.

## Long Responses

Long messages are auto-split. Don't worry about Telegram length limits.
