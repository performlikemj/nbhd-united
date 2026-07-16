# Tools

## Communication

- Messages reach you from the **NBHD app (iOS)**, **Telegram**, or **LINE**. The NBHD app is the primary product surface — most users are there.
- Reply on the same surface the message arrived on. Each conversational turn is tagged with a `[chat via …]` marker naming the active channel; treat that marker as the source of truth for where the user is.
- Telegram and LINE are fully supported. Never assume a message came from Telegram.

## Managed Skills

- `skills/nbhd-managed/daily-journal/` — Daily reflection journal
- `skills/nbhd-managed/weekly-review/` — End-of-week synthesis

## Freshness model

USER.md (and the rest of the bootstrap files) reflects state as of the start of this turn. Any `nbhd_*` runtime tool you call during this turn writes to the database immediately; that change won't appear in USER.md until the *next* turn.

**Trust your tool result over USER.md for state you just modified.** Never tell the user "I have no recent X" based on USER.md if you logged one yourself this turn. Confirm what you logged in the reply, even when the user's main topic was something else.

## Notes

Add any personal tool preferences or environment notes here.
