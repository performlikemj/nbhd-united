<!-- CRON-ONLY: chat sessions cannot read this file; it is listed in the cron preamble. Chat delivery of these rules is via tool descriptions/responses and AGENTS.md. -->

For Fuel plans/fill-ins, first use `tool_search` for exact `nbhd_fuel_search_exercises` and call it per accessory/mobility group; then find/call `nbhd_fuel_create_plan`/`nbhd_fuel_update_plan`. Plans four weeks or longer rotate accessories every 1–2 weeks.

## Background workout cron

- On training days, the background workout cron runs silently without sending a user message and writes a `fuel` section into today's daily note.
- Keep that section to 4–6 lines: today's workout (activity, category, duration; or rest day), plan progress, last night's sleep and recovery note when poor, and whether yesterday was completed, missed, or rest.
- Morning briefings weave the planned workout and relevant sleep context into the briefing naturally without repeating the section verbatim.
- Evening check-ins use the section to check whether a planned workout was logged, acknowledge progress when done, or ask casually when it was not.
- Week-ahead reviews factor upcoming training days into the preview and flag conflicts with travel or busy periods.
