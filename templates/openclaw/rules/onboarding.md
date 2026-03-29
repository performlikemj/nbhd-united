# Onboarding — First Sessions

## Timezone Setup

Check config for `userTimezone`. If UTC or empty, ask once (check memory for `timezone_asked` first):

1. Ask casually where they're based — in whatever language you've been chatting in
2. Offer common timezone buttons, prioritized by conversation language
3. Include an "Other" option
4. Confirm before saving
5. On confirmation, call `nbhd_update_profile` with the timezone
6. Write `timezone_asked: true` to memory

Rules: ask only once, don't nag, never infer from message timestamps.

## Location

If no `location_city` in USER.md, ask once in early conversation:

> "What city are you in? I use it for weather in your morning briefings."

Look up coordinates via `web_search`, call `nbhd_update_profile` with `location_city`, `location_lat`, `location_lon`. Write `location_asked: true` to memory.
