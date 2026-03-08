# Check-in Quality Improvements — Lessons from Personal Agent

## Background

Weekly review of personal agent check-ins (March 2-8, 2026) found systemic issues:
1. Stale carry-over — items listed as "pending" after being completed or dropped
2. No verification before reporting — claims about task status made from memory, not data
3. Redundant information — same event logged multiple times
4. Date math errors — "TOMORROW" when event was 2 days away

These same patterns WILL affect subscriber agents. Here's how to fix them.

## Issues & Fixes

### 1. Evening Check-in Reports Completed Tasks as Pending

**Problem:** The evening check-in loads journal context and tasks, but has no mechanism
to cross-reference what was actually completed during the day vs what's still open.
It relies on the agent's judgment, which degrades with cheaper models (M2.5 especially).

**Current prompt says:**
```
"Check which tasks are open (`- [ ]`) vs completed (`- [x]`)"
```

**But doesn't say:** verify each item before listing it. The agent can still hallucinate
task status from conversation memory rather than reading the actual tasks document.

**Fix:** Add explicit verification step to evening prompt:
```
"VERIFICATION: After loading tasks, for each item you plan to list as 'not done':
- Confirm it appears as `- [ ]` (unchecked) in the tasks document
- Confirm it was actually planned for today (not a future task)
- If a task was completed during conversation but not yet checked off,
  mark it complete (`nbhd_document_put`) before listing it as done"
```

### 2. Morning Briefing Can Report Stale Context

**Problem:** Morning briefing loads journal context from yesterday but doesn't verify
whether items mentioned are still current. If the user resolved something in a late-night
conversation, the morning briefing might still flag it as open.

**Fix:** Add to morning prompt:
```
"Before listing any carry-over item from yesterday:
1. Load the tasks document — is it still marked open?
2. Check if the user addressed it in yesterday's evening check-in
3. If the user said 'done' or 'drop it' in any conversation, remove it from carry-over"
```

### 3. No Cross-Check Between Daily Note Sections

**Problem:** The morning-report section might say "reminder: finish X" and then the
evening-check-in section lists "X not done" even if X was completed mid-day. The sections
don't reference each other.

**Fix:** Evening prompt should explicitly read the morning-report section first:
```
"1. Load today's daily note — read ALL sections including morning-report
2. Cross-reference morning priorities against what actually happened
3. Only list items as 'not done' if they were in the morning plan AND are still open"
```

### 4. Calendar Event Date Math

**Problem:** Agent might say "your meeting is tomorrow" when it's actually 2 days away.
Worse with cheaper models that struggle with date arithmetic.

**Fix:** Pre-compute dates server-side. The morning prompt already gets a weather URL
built server-side — do the same for the current date:
```python
# In _build_morning_briefing_prompt():
from datetime import datetime
import zoneinfo

user_tz_obj = zoneinfo.ZoneInfo(user_tz)
now = datetime.now(user_tz_obj)
today_str = now.strftime("%A, %B %d, %Y")

# Inject into prompt:
f"Today is {today_str} in the user's timezone ({user_tz}).\n"
f"When mentioning future events, always compute the exact number of days: "
f"'event_date minus {now.strftime('%Y-%m-%d')} = X days from now'. "
f"Never say 'tomorrow' unless the math confirms it is exactly 1 day away.\n"
```

This removes the agent's need to figure out what day it is (which M2.5 gets wrong).

### 5. Redundant Daily Note Entries

**Problem:** If the heartbeat check-in and evening check-in both fire, they might both
write overlapping content to the daily note. Background Tasks might also duplicate.

**Current state:** Each cron writes to different sections (morning-report, evening-check-in),
which helps. But heartbeat uses `nbhd_daily_note_append` which goes into a general log area
that can overlap with section content.

**Fix:** Add to heartbeat prompt:
```
"Before appending to the daily note, read the existing note first.
Do NOT re-log information that is already captured in any section.
If your observation is already covered, skip the append."
```

### 6. Tasks Document Drift

**Problem:** Tasks accumulate but nothing enforces cleanup. Old tasks from weeks ago
sit unchecked, and the morning briefing dutifully reports them every day. The user gets
numb to the list.

**Fix:** Add task aging logic to the Week Ahead Review:
```
"Review the tasks document for stale items:
- Any task older than 7 days that hasn't been completed → ask user: still relevant?
- Any task older than 14 days → suggest removing or converting to a goal
- Surface this in the weekly message: 'You have X tasks older than a week — want to clean up?'"
```

**Implementation:** Could add timestamps to tasks (e.g., `- [ ] Do X <!-- added:2026-03-01 -->`)
or track in a separate metadata field.

### 7. Server-Side Date Injection (Highest Impact)

**Problem (documented in memory):** OpenClaw's system prompt only says `Time zone: Asia/Tokyo`
but doesn't include the actual current date. The agent must call `session_status` to learn
the date, but M2.5 doesn't do this reliably. Result: agent thinks it's January 2025.

**This is the root cause of most date-related errors in check-ins.**

**Fix:** Already partially done (`envelopeTimezone: "user"` in config), but the config
generator should also inject the date explicitly into every cron prompt:

```python
def _inject_date_context(prompt: str, tenant: Tenant) -> str:
    """Prepend current date/time context to a cron prompt."""
    import zoneinfo
    from datetime import datetime

    user_tz = str(getattr(tenant.user, "timezone", "") or "UTC")
    try:
        tz = zoneinfo.ZoneInfo(user_tz)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    now = datetime.now(tz)
    date_line = (
        f"Current date and time: {now.strftime('%A, %B %d, %Y at %H:%M')} ({user_tz})\n\n"
    )
    return date_line + prompt
```

Then wrap every cron prompt:
```python
"message": _inject_date_context(_build_morning_briefing_prompt(tenant), tenant),
```

### 8. Evening Check-in Should Load Morning Section

**Problem:** Evening check-in loads `nbhd_journal_context` but doesn't explicitly load
today's morning-report section. So it can't verify whether morning priorities were addressed.

**Fix:** Add step to evening prompt:
```
"1. Load today's full daily note (`nbhd_daily_note_get` with today's date)
2. Read the morning-report section — note the 'Top 3 Priorities' and 'Open Tasks'
3. Cross-reference: were any of those completed today?
4. Load the tasks document — verify current state of each task
5. THEN write the evening check-in with verified status for each item"
```

## Implementation Priority

| # | Fix | Impact | Effort | Priority |
|---|-----|--------|--------|----------|
| 7 | Server-side date injection | Critical | Low | P0 |
| 4 | Calendar date math in prompt | High | Low | P0 |
| 1 | Evening verification step | High | Low | P1 |
| 8 | Evening loads morning section | High | Low | P1 |
| 2 | Morning stale context check | Medium | Low | P1 |
| 3 | Cross-check between sections | Medium | Low | P2 |
| 5 | Heartbeat dedup | Medium | Low | P2 |
| 6 | Task aging | Medium | Medium | P3 |
