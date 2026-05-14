# Tools

## Communication

- **Telegram** is the primary channel. All messages come through Telegram DM.

## Managed Skills

- `skills/nbhd-managed/daily-journal/` — Daily reflection journal
- `skills/nbhd-managed/weekly-review/` — End-of-week synthesis

## Freshness model

USER.md (and the rest of the bootstrap files) reflects state as of the start of this turn. Any `nbhd_*` runtime tool you call during this turn writes to the database immediately; that change won't appear in USER.md until the *next* turn.

**Trust your tool result over USER.md for state you just modified.** Never tell the user "I have no recent X" based on USER.md if you logged one yourself this turn. Confirm what you logged in the reply, even when the user's main topic was something else.

## Notes

Add any personal tool preferences or environment notes here.
