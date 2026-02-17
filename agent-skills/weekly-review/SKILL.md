---
name: weekly-review
description: Review the week, synthesize patterns, and save a structured weekly review.
---

# Weekly Review

## When to Use
- User asks to review the week.
- End-of-week reflection moment.
- User asks for pattern summary across recent days.

## When NOT to Use
- User only wants a daily check-in (`daily-journal`).
- User wants direct task execution without reflection.
- User wants ad hoc venting without structured synthesis.

## Flow
1. Set context for a short reflection session.
2. Load the past 7 days of daily notes and memory using `nbhd_journal_context({ days: 7 })`.
3. Reflect on patterns (wins, challenges, mood/energy arc).
4. Capture lessons and 1-3 intentions for next week.
5. Ask for a simple week rating (`thumbs-up|thumbs-down|meh`).
6. Save the weekly review as a Document using `nbhd_document_put`.

## Tools

| Tool | Purpose |
|------|---------|
| `nbhd_journal_context` | Load recent daily notes + memory (days: 7) |
| `nbhd_document_put` | Save weekly review document (kind: "weekly", slug: "YYYY-MM-DD" Monday of week) |
| `nbhd_journal_search` | Search past notes for specific topics if needed |

### Load context

`nbhd_journal_context`

```json
{
  "days": 7
}
```

### Save weekly review

`nbhd_document_put`

```json
{
  "kind": "weekly",
  "slug": "YYYY-MM-DD",
  "title": "Weekly Review — YYYY-MM-DD",
  "markdown": "Free-form markdown: patterns, wins, challenges, lessons, intentions, rating"
}
```

The slug should be the Monday of the review week (ISO date). The markdown body is free-form — include sections for patterns, wins, challenges, lessons learned, intentions for next week, and the user's week rating.

## Output to User
- Keep tone reflective, not evaluative.
- Confirm what was saved in concise bullets.
- If tools fail, disclose the issue and retry once before fallback.
