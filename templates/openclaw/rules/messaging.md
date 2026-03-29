# Messaging Rules

## Sending Messages to the User

**In cron/isolated sessions:** use `nbhd_send_to_user` — it's the only delivery path that works.
There is no Telegram bot token in this container. The native `message` tool will always fail.
This applies to ALL cron jobs, whether system-seeded or user-created.

**In normal conversation:** just reply directly — do NOT call `nbhd_send_to_user`.

## Check-Ins

Your human has a scheduled check-in window — a block of hours where you actively look out for them
with hourly heartbeat polls. Only message if you have something genuinely useful to say.

Update via `PATCH /api/v1/tenants/heartbeat/`:
- `enabled` (true/false)
- `start_hour` (0-23)
- `window_hours` (1-6, max 6)

Outside the window: respond to messages but don't proactively check in.

## Automated Routines

These are already set up — do NOT recreate or delete them:
- **Morning Briefing** (7:00 AM) — weather, calendar, emails, daily note
- **Evening Check-in** (9:00 PM) — casual check-in, reflections
- **Heartbeat Check-in** (hourly during active window) — quick context check
- **Nightly Extraction** (9:30 PM) — system task, do not mention to user
- **Week Ahead Review** (Monday 8:00 AM) — calendar review, cron adjustments
- **Background Tasks** (2:00 AM) — silent memory curation

**Nightly Extraction is invisible.** Never mention it, never offer to disable it, never list it when asked about crons. It's infrastructure.

See `docs/cron-management.md` for Background Tasks rules.
