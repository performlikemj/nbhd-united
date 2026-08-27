<!-- CRON-ONLY: chat sessions cannot read this file; it is listed in the cron preamble. Chat delivery of these rules is via tool descriptions/responses and AGENTS.md. -->
# Week Ahead Review

Once a week, make yourself aware of the user's upcoming week before running your usual automations. The goal is to catch conflicts (travel, busy periods, sick days) before scheduled crons fire and create noise.

## When to do it

- **Proactive:** first morning on Monday (or first available workday of the week)

## Steps

1. **Pull context for the week ahead:**
   - Recent `memory/YYYY-MM-DD.md` entries (last 7 days)
   - `nbhd_calendar_list_events` for upcoming 7 days (and longer if travel is hinted)
   - Recent journal context (`nbhd_journal_context`) and any explicit plan notes
   - Current active cron jobs (`cron list`)

2. **For each enabled cron, ask:** "Does this still make sense this week?"

3. **If it doesn't, do one of:**
   - **Pause** it for the whole week (`cron disable`)
   - **Narrow it** (`cron edit`) to avoid conflict windows
   - **Redirect it** (change location/topic in the cron prompt)

4. **Leave a short note** in `memory/week-ahead/YYYY-WXX.md`:
   - What changed, why, and when you'll review again

5. **Send a brief user-facing heads-up** only if the change is meaningful:
   - *"I skipped your weekend event search for this week because you're out of town Friday–Sunday."*

## Quick checklist

- [ ] No "always run" cron is blindly trusted this week
- [ ] Travel, visitor, and busy-period conflicts are handled before they cause noise
- [ ] All decisions are logged in `memory/week-ahead/YYYY-WXX.md`

See `docs/cron-management.md` for cron job rules.
