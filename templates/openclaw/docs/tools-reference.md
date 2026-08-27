<!-- CRON-ONLY: chat sessions cannot read this file; it is listed in the cron preamble. Tool descriptions are the authoritative reference. -->

# Tools Reference

## Journal-link marker

To point at a journal document or project touched this turn, put `[[journal-link: kind|slug|title]]` on its own line. Use only the exact slug returned by a journal tool this turn; never invent one. The marker must be the last line, or immediately before a final `[[quick-replies: ...]]` line.

## Reddit

**Session-start check:** In a cron session where Reddit tools are available, run `nbhd_reddit_status` silently. If connected, tell the user Reddit is ready and ask which subreddit(s) to use when none are specified.

Never call `nbhd_reddit_post` or `nbhd_reddit_reply` until you have shown the exact draft and the user has explicitly approved it. A scheduled or background turn cannot obtain that approval.

## Fuel

For Fuel plans/fill-ins, first use `tool_search` for exact `nbhd_fuel_search_exercises` and call it per accessory/mobility group; then find/call `nbhd_fuel_create_plan`/`nbhd_fuel_update_plan`. Plans four weeks or longer rotate accessories every 1–2 weeks.
