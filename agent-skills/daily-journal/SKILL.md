---
name: daily-journal
description: Guide a warm daily reflection and persist a structured journal entry.
---

# Daily Journal

## When to Use
- User explicitly asks to journal.
- User starts reflecting on their day and wants structure.
- User wants a quick daily check-in.

## When NOT to Use
- User asks for factual lookup or task execution.
- User is doing end-of-week synthesis (`weekly-review` is better).
- User is venting and wants pure support with no structured capture.

## Flow
1. Open gently and confirm they want a short or deep check-in.
2. Capture mood (free text) and energy (`low|medium|high` inferred from user language).
3. Capture wins (0-10 short bullets).
4. Capture challenges (0-10 short bullets).
5. Offer optional reflection for tomorrow.
6. Summarize and confirm save.

## Tooling

After confirmation, call:

`nbhd_journal_create_entry`

Payload shape:

```json
{
  "date": "YYYY-MM-DD",
  "mood": "string",
  "energy": "low|medium|high",
  "wins": ["string"],
  "challenges": ["string"],
  "reflection": "string",
  "raw_text": "natural language summary of session"
}
```

## Output to User
- Confirm saved with a 1-2 line recap.
- If save fails, acknowledge technical issue and retry once.
- Avoid clinical language; be concise and human.
