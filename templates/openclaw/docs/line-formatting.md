# LINE Formatting

Your responses are delivered through LINE as Flex Messages — branded card layouts.
A post-processing layer converts your markdown into rich visual components.
Write naturally and use structure — the better you structure, the better it looks.

## What works

| Format | Syntax | How it renders |
|--------|--------|----------------|
| Section headers | `## Title` | Teal header bar (first) or bold section title |
| Bullet list | `- item` | Styled rows with teal bullet dots |
| Emoji bullets | `✅ item` | Same — emoji-prefixed lines are bullet items |
| Nested bullets | `  - sub item` | Indented, smaller, muted color |
| Numbered list | `1. item` | Same as bullets |
| Links | `[label](url)` | Tappable link rows with 🔗 icon |
| Bare URLs | `https://...` | Auto-extracted as tappable links |
| Emoji headers | `🏋️ Title` | Detected as section headers (when isolated) |

## What gets stripped

Inline formatting is removed before building the card:

- `**bold**` → plain text (use `##` headers for emphasis instead)
- `*italic*` → plain text
- `` `code` `` → plain text
- Code blocks (` ``` `) → content kept, markers removed

## Structure your responses

LINE renders structured content beautifully. Use `##` headers to create
visual sections — the first header becomes the card's teal title bar,
subsequent headers become bold section titles with separators between them.

### Good example

```
## Morning Routine

- Wake up at 6:30
- 10 min stretch
- Review tasks for the day

## Evening Wind-down

- Journal for 5 minutes
- Set tomorrow's priorities
```

This renders as a branded card with a "Morning Routine" header bar,
bullet list, a visual separator, then "Evening Wind-down" as a bold
section title with its own bullets.

### Short responses

Messages under ~200 characters render as compact cards with a teal
accent bar on the left. No header needed — just write naturally.

## Inline buttons

```
[[button:Yes, do it|confirm_action]]
[[button:No thanks|cancel_action]]
```

Buttons render as quick-reply pills at the bottom of the chat.
Keep labels short — **truncated at 20 characters**.
Maximum 13 buttons per message.

When the user taps a button you receive: `[User tapped button: "confirm_action"]`

*Use buttons for:* binary choices, quick options, confirmations.
*Don't use for:* more than ~8 options, when the user needs to type a custom answer.

## Photos and media

- User sends a photo: not currently supported on LINE (text and voice only).
- User sends voice: automatically transcribed to text — you receive the transcript.
- User sends a sticker: you receive the sticker's emotional intent as context.
- Sending images: not yet supported. Do not output `MEDIA:` references.

## Emoji headers vs emoji bullets

A single emoji-prefixed line followed by different content acts as a section header:

```
🏋️ Workout Plan
- Pull-ups 4x6-10
- Rows 3x8-12

🥗 Nutrition
- Protein: 150g target
- Track meals in app
```

But consecutive emoji-prefixed lines act as a bullet list:

```
✅ Morning stretch done
✅ Journaled for 5 minutes
🔴 Skipped meditation
  - Will try again tomorrow
```

The rule: if emoji lines appear back-to-back, they're list items (rendered
with styled bullets). If an emoji line stands alone before other content,
it's a section header (rendered as a bold title).

## Critical rules

- ✅ Use `##` headers to create visual structure — they render as branded cards
- ✅ Use bullet lists (`-`) or emoji bullets (`✅`, `🔴`) — both render with styled dots
- ✅ Nest sub-items with 2+ spaces of indentation — they render indented and smaller
- ✅ Include links as `[label](url)` — they become tappable link rows
- ✅ Keep responses concise — under ~2000 characters renders best
- ❌ Never use `*single asterisks*` for bold — they get stripped
- ❌ Never use `#` (single hash) — use `##` for section headers
- ❌ Never reference Telegram or Telegram-specific features
- ❌ Never output `MEDIA:` image references — not supported on LINE
- ❌ Never worry about Telegram's 4096 character limit — LINE limit is 5000

## What users see

Your response becomes a branded card on a warm off-white background with:
- Teal header bar (from first `##` header)
- Clean typography with comfortable line spacing
- Styled bullet points with teal dots
- Tappable link rows with 🔗 icons
- Separators between sections
- "Tap to copy" footer for clipboard access

## Long responses

Messages over ~2000 characters still render as Flex cards but the layout
may simplify. Very long messages (5000+ chars) fall back to plain text.
The card always includes a "Tap to copy" footer so users can grab the full text.
