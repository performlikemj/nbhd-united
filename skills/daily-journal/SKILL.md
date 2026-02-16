---
name: daily-journal
description: >
  Manage the user's daily journal — morning reports, log entries, evening check-ins, and long-term memory curation.
  Use when: a scheduled job fires for morning report, evening reminder, or memory curation. Also use when
  the user asks to add a journal entry, check their journal, review their week, or update their memory.
  Don't use when: the user is just chatting and not referencing their journal or daily workflow.
---

# Daily Journal

Operate the user's personal OS — a collaborative daily note where both you and the user write, plus a long-term memory document you curate over time.

## Core Concept

Each day has ONE markdown document. You and the user both write into it. The document follows the user's daily note template (customizable, but defaults are below). You fill in your sections (morning report, log entries). The user fills in theirs (evening check-in, reflections). You can comment on each other's entries inline.

## Tools

Journal operations use registered OpenClaw tools (provided by the `nbhd-journal-tools` plugin). No direct HTTP calls needed.

| Tool | Purpose |
|------|---------|
| `nbhd_journal_context` | Load recent daily notes + memory (session init) |
| `nbhd_daily_note_get` | Get raw markdown for a specific date |
| `nbhd_daily_note_append` | Append a timestamped entry (auto author=agent) |
| `nbhd_memory_get` | Get the long-term memory document |
| `nbhd_memory_update` | Replace the long-term memory document |

For endpoint details and response shapes, see `references/api.md`.

## Session Start

Every session, before responding to the user, load context:

1. Call `nbhd_journal_context` (defaults to last 7 days)
2. Scan for: open blockers, yesterday's "plan for tomorrow", overnight work requests, unresolved decisions
3. Use this context to inform your responses throughout the session

## Scheduled Jobs

### Morning Report (cron, user's preferred morning time)

Read the user's morning report template from `references/templates.md`. Fill in each section:

1. **Overnight work completed** — summarize what you did while the user was away
2. **Where things stand** — brief project status for active projects
3. **Decisions needed** — anything blocked on the user
4. **Reminders** — upcoming deadlines, expiring keys, events
5. **Today's priorities** — suggested focus based on yesterday's plan and current state

Append the completed morning report via `nbhd_daily_note_append` with the full markdown block as `content`.

Then send a summary to the user via their messaging channel (Telegram, etc.) — keep it concise, link to the full note if the platform supports it.

### Evening Check-in Reminder (cron, user's preferred evening time)

Send a nudge via messaging: "Hey, ready for your evening check-in? How was your day?"

If the user replies conversationally (e.g., "good day, got the merge done, didn't make it to the gym"), parse their response into the evening check-in structure and save it. Confirm what you captured and ask if anything's missing.

If the user fills it in via the app UI, no action needed — the frontend handles it.

### Memory Curation (cron, weekly — e.g., Sunday evening)

1. Load the last 7 days via `nbhd_journal_context({ days: 7 })`
2. The response includes both daily notes and current memory
3. Review daily notes for:
   - **Preferences** discovered (work habits, food, schedule patterns)
   - **Decisions** made (technical, personal, project direction)
   - **Lessons learned** (what worked, what didn't, mistakes)
   - **Goals** mentioned or updated
   - **People & context** (new contacts, relationships, team changes)
4. Update the memory document — add new insights, update existing ones, remove outdated info
5. Save via `nbhd_memory_update({ markdown: "..." })`

Keep the memory document organized by category (see `references/templates.md` for default structure).

## Throughout the Day

When you do something noteworthy during a conversation, append a log entry:

```
nbhd_daily_note_append({ content: "Checked production logs — all stable. No errors in last 12h." })
```

The tool auto-timestamps and marks it as `author=agent`. Do this for:
- Email/calendar checks
- Research completed
- Tasks finished
- Important information discovered
- Errors or issues found

Do NOT log routine acknowledgments or small talk. Only log things the user would want to see when reviewing their day.

## User Interactions

### "Add to my journal"
Append their entry via the API with `author=human` context. Confirm briefly.

### "What happened today/yesterday/this week?"
Fetch the relevant daily note(s) and summarize. Highlight key events, decisions, and mood trends.

### "Update my memory with..."
Add the information to the long-term memory document. Confirm what was added.

### "What do you know about me?"
Read the long-term memory document and present a summary. Offer to correct or add anything.

## Templates

Default templates are in `references/templates.md`. The user can customize these via the Templates page in the app. Always fetch the user's current template before generating content — don't assume defaults.

## Tone

Match the user's energy. If they write casually, respond casually. If they're detailed, be detailed. The journal is a shared space — it should feel collaborative, not robotic.
