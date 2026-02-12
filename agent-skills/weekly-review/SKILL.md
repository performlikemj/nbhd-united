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
2. Fetch journal entries for the window using `nbhd_journal_list_entries`.
3. Reflect on patterns (wins, challenges, mood/energy arc).
4. Capture lessons and 1-3 intentions for next week.
5. Ask for a simple week rating (`thumbs-up|thumbs-down|meh`).
6. Summarize and persist the weekly review.

## Tooling

### Read journal entries

`nbhd_journal_list_entries`

```json
{
  "date_from": "YYYY-MM-DD",
  "date_to": "YYYY-MM-DD"
}
```

### Save weekly review

`nbhd_journal_create_weekly_review`

```json
{
  "week_start": "YYYY-MM-DD",
  "week_end": "YYYY-MM-DD",
  "mood_summary": "string",
  "top_wins": ["string"],
  "top_challenges": ["string"],
  "lessons": ["string"],
  "week_rating": "thumbs-up|thumbs-down|meh",
  "intentions_next_week": ["string"],
  "raw_text": "natural language summary of session"
}
```

## Output to User
- Keep tone reflective, not evaluative.
- Confirm what was saved in concise bullets.
- If tools fail, disclose the issue and retry once before fallback.
