# Scheduled Task Management

Always get explicit user confirmation before creating or modifying any scheduled task.

## How scheduled tasks run

User-facing crons (reminders, alarms, custom tasks) and system crons (Morning Briefing, etc.) **both** use `sessionTarget: "isolated"` + `payload.kind: "agentTurn"` on this fleet. The agent runs a one-turn isolated session at fire time and delivers any user-facing message via `nbhd_send_to_user`.

Why not `sessionTarget: "main"` for "context-aware" reminders? Two reasons enforced by the runtime:

1. **`sessionTarget: "main"` REQUIRES `payload.kind: "systemEvent"`** — the gateway throws `main cron jobs require payload.kind="systemEvent"` and the call fails with HTTP 500. The combination `main + agentTurn` is invalid.
2. **`main + systemEvent + wakeMode:"now"` runs through the heartbeat scheduler**, which is gated by `agents.defaults.heartbeat.activeHours`. Outside that window every fire is silently skipped (`status: "skipped"`, `error: "quiet-hours"`, job auto-disables). For ad-hoc reminders that can fire any time of day, this is the wrong tool.

The isolated + agentTurn pattern fires regardless of heartbeat hours, and the agent's `payload.message` is the full instruction it reads at fire time. Treat that message as a letter to your future self — the original conversation will not be in active context.

### System crons (Morning Briefing, Evening Check-in, etc.)

Same isolated + agentTurn shape. System tasks have a **foreground / background** flag:

- **Foreground (default)** — when the task finishes, it pushes a 2-3 sentence summary into the main session so the assistant knows what just happened. The push is *conditional*: it only fires on runs where the task actually sent the user a message. Heartbeat-style tasks that often return silently will only sync on the runs that produced output.
- **Background** — runs silently and never reports back. Use this for noisy maintenance jobs like Background Tasks. The user can toggle it from their Scheduled Tasks page.

The Phase 2 sync is implemented as a one-shot cron the agent creates at the end of its run. These are named `_sync:<task name>` and are hidden from the user-facing UI. They self-clean: the systemEvent text instructs the main session to call `cron remove _sync:<task name>` after noting the summary. Don't manually create, modify, or delete `_sync:*` jobs.

### Messaging rule — NON-NEGOTIABLE

Every cron MUST deliver its message through **`delivery: {"mode": "none"}` + a call to `nbhd_send_to_user`** at fire time. There is no exception.

**NEVER** set `delivery.mode` to `"announce"`, `"telegram"`, or `"line"`, and **NEVER** add a `delivery.channel`. Those use OC's built-in channel delivery, which on this fleet:

1. **Silently fails to deliver at all** — there is no Telegram bot token at the OC channel layer, so an `announce`/channel cron is broken and errors at fire time (`Delivering to Telegram requires target <chatId>`), and the user gets *nothing*; and
2. **Bypasses the iOS app entirely** — built-in delivery goes container→Telegram/LINE and never touches the backend, so no iOS push notification is sent and iPhone-only users never see the message. Only the `nbhd_send_to_user` path reaches the backend, which is what fires the iOS push.

If you omit `delivery`, OC defaults to `announce` (same broken result) and multi-channel users hit `delivery.channel is required when multiple channels are configured`. So `delivery: {"mode": "none"}` is not optional — it is the only correct value.

```
❌ "delivery": {"mode": "announce"}                          // fails to send AND no iOS push
❌ "delivery": {"mode": "telegram", "channel": "telegram"}   // same
❌  (no delivery block)                                      // OC defaults to announce → broken
✅ "delivery": {"mode": "none"}   + the agentTurn message calls nbhd_send_to_user
```

The native `message` tool also does not work in subscriber containers — `nbhd_send_to_user` is the one and only way to reach the user.

**Journal writes are MANDATORY when the cron prompt asks for them.** Use `nbhd_daily_note_set_section` and `nbhd_daily_note_append` exactly as the cron prompt instructs. Do not assume normal memory hooks will cover it — they will not, and the Journal app will be empty if you skip the explicit calls.

**Link the message to the document you wrote.** When a cron writes a journal document (a morning report writes today's daily note, etc.), end the `nbhd_send_to_user` message with a `[[journal-link: kind|slug|title]]` marker on its **own final line** so the iOS app renders a tappable "View in Journal" chip that opens exactly that document. The `slug` MUST be the value the journal tool echoed, or **today's ISO date** for a daily note — never invented. It works on every delivery channel: the chip renders in the iOS app (even for a Telegram/LINE user, when they open the app), and the marker is silently stripped from the Telegram/LINE text. See the [`[[journal-link:]]` chip](tools-reference.md#linking-a-reply-to-a-journal-document-the-journal-link-chip) in tools-reference for the full format and ✅/❌ examples.

```
✅  "...here's your morning report.\n\n[[journal-link: daily|2026-07-13|Morning Report]]"   // taps through to today's note
❌   marker on the same line as prose, or a made-up slug                                     // won't parse / opens nothing
```

## Two flavours of scheduling

Pick the right shape for the user's intent:

- **Recurring task** — repeats on a pattern ("every weekday at 8am", "every Monday morning"). Use `schedule: {kind: "cron", expr: "...", tz: "..."}`. Counts toward the 10-task cap. Requires explicit approval before creation. Does **not** auto-delete — manage lifecycle via `cron remove`.
- **One-off reminder** — fires once at a specific moment ("remind me in 20 minutes", "ping me at 4pm today", "tomorrow morning"). Use `schedule: {kind: "at", at: "..."}`. Does NOT count toward the 10-task cap. Auto-deletes after firing. Lighter approval — confirm in text, no buttons needed.

If the user's request is ambiguous, ask: *"Is this a one-time reminder or something you want me to repeat?"*

## Canonical `cron add` shape (both flavours)

```json
{
  "name": "<short descriptive label>",
  "schedule": { ... },
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "<self-contained instruction the future-you reads at fire time>"
  },
  "wakeMode": "now",
  "delivery": { "mode": "none" }
}
```

`schedule` is the only thing that changes between recurring and one-off:

- Recurring: `{"kind": "cron", "expr": "<5-field>", "tz": "<userTimezone>"}`
- One-off: `{"kind": "at", "at": "<ISO 8601 with offset, e.g. 2026-06-18T09:00:00+09:00>"}`

Relative durations like `"20m"` are **not** accepted by `cron add` — see [Two ways to schedule](#two-ways-to-schedule-and-what-each-one-accepts) below.

Every other field is the same. **Do not deviate from this shape** — the variants that *look* sensible (`main` session for context, `announce` delivery to a channel) are runtime-rejected or fire-time-skipped on this fleet. See the [shape invariants](#shape-invariants) section below for the proofs.

## Creating a recurring task

1. Call `cron list` — check for duplicates first.
2. Present a draft with approval buttons:
   ```
   *Morning Email Check*
   ⏰ Every day at 8:00 AM
   📋 Check inbox and summarize important emails

   [[button:✅ Create it|cron_approve]]
   [[button:✏️ Change something|cron_edit]]
   [[button:❌ Never mind|cron_reject]]
   ```
3. Only call `cron add` after the user approves.
4. Submit the canonical shape with `schedule: {"kind": "cron", "expr": "<5-field>", "tz": "<userTimezone>"}`. The `payload.message` must contain everything your future self needs (the user's intent, the verbatim text to send, plus an explicit "call `nbhd_send_to_user`" instruction) — the conversation that created the cron will not be in active context at fire time.

### Writing the cron expression

**Exactly 5 fields**, in this order:

```
minute  hour  day-of-month  month  day-of-week
```

A 6-field expression (seconds precision) is rejected — seconds are not supported here.

**day-of-week is `0` = Sunday**, `1` = Monday, … `6` = Saturday. `7` also means Sunday. Names work too, and are harder to get wrong: `SUN`, `MON`, `TUE`, `WED`, `THU`, `FRI`, `SAT`, including ranges like `MON-FRI` and lists like `MON,WED,FRI`.

> ⚠️ This is **not** the convention the fuel/workout tools use. There, `0` is Monday. In a cron expression `0` is Sunday. When in doubt, write the name.

```
0 8 * * 1-5     ✅ weekdays at 8:00        (Mon-Fri)
0 8 * * MON-FRI ✅ the same thing, unambiguous
0 9 * * 0       ✅ Sundays at 9:00
0 9 * * 6,0     ✅ weekends at 9:00        (Sat + Sun)
0 9 * * 8       ❌ rejected — there is no day 8
0 0 9 * * 1     ❌ rejected — 6 fields
```

`tz` is optional: leave it out and it defaults to the user's own timezone, which is what you want almost every time. If you do pass it, use the Area/Location form (`Asia/Tokyo`). Never an `Etc/*` name — their sign is inverted (`Etc/GMT+9` is UTC *minus* 9) and they are rejected.

## Creating a one-off reminder

For one-time reminders, skip the buttons and just confirm in text **AND invoke `cron add` in the same turn**. The confirmation alone does NOT create the reminder — emitting the acknowledgement and yielding without calling the tool means nothing is scheduled and the user will not be pinged.

Step 1 — send the confirmation message:

> "Sure — I'll remind you to take out the laundry in 20 minutes. ✓"

Step 2 — **in the same turn**, invoke the tool. The easiest correct call is `nbhd_cron_create_pure_reminder`, which accepts a relative duration:

```json
{
  "name": "laundry reminder",
  "schedule": {"kind": "at", "at": "20m"},
  "text": "Time to take out the laundry."
}
```

The equivalent through raw `cron add` needs an absolute timestamp and the full canonical shape:

```json
{
  "name": "laundry reminder",
  "schedule": {"kind": "at", "at": "2026-06-18T09:20:00+09:00"},
  "sessionTarget": "isolated",
  "payload": {
    "kind": "agentTurn",
    "message": "Reminder for the user: take out the laundry. Send via nbhd_send_to_user."
  },
  "wakeMode": "now",
  "delivery": {"mode": "none"}
}
```

**The invocation is a TOOL call, not a chat message.** Do NOT typeset the parameters or paraphrase them as prose. The text confirmation goes to the user; the tool call goes to the gateway. Both happen before you yield.

### Two ways to schedule, and what each one accepts

The two surfaces do **not** accept the same `schedule.at`. Getting this wrong is the single most common way a "remind me in 20 minutes" silently fails.

| | `nbhd_cron_create_*` (typed tools) | raw `cron add` |
|---|---|---|
| `"20m"`, `"2h"`, `"1d"` | ✅ accepted — the backend converts it to an absolute time before storing | ❌ rejected: `Invalid schedule.at: expected ISO-8601 timestamp (got 20m)` |
| `"2026-06-18T09:00:00+09:00"` | ✅ accepted | ✅ accepted |
| `"2026-06-18T09:00:00"` (no offset) | ❌ rejected with an explanation | ⚠️ **accepted and silently read as UTC** — for a Tokyo user this fires 9 hours off |

So:

- Use `nbhd_cron_create_pure_reminder` (or the other typed tools) for "in N minutes/hours" — you can pass the duration straight through and never do offset arithmetic.
- If you use raw `cron add`, you must compute the absolute time yourself and **always** include the user's offset (`+09:00`, `-04:00`, …). A bare timestamp is the one input nothing catches for you.

The gateway auto-deletes one-off crons after they fire successfully — no cleanup needed from your side. If you need to cancel one before it fires, use `cron remove <name>`.

### One-off caps (anti-abuse)

Before adding a one-off, call `cron list` and count jobs with `schedule.kind == "at"`:

- **20 pending one-offs** is the per-tenant ceiling you must respect. At or above this, decline politely: *"You have a lot of pending reminders already — want me to cancel some first, or wait until a few fire?"*
- Avoid creating bursts (e.g. "remind me every 5 minutes for the next 2 hours" should be a recurring `every` schedule, not 24 separate `at` jobs).

The platform also enforces hard backstops you should never reach: a logged warning at 50 pending one-offs, and automatic reaping of the newest crons back to 200 if the count somehow climbs that high. These are abuse detectors — operating in their range means you have stopped following the soft cap above.

## Shape invariants

The runtime enforces hard rules at `cron add` time. Violating any of them returns HTTP 500 with the masked message `"tool execution failed"` — the real error lives only in OC container logs. Keep these straight:

1. **`sessionTarget: "main"`** REQUIRES `payload.kind: "systemEvent"` (and `payload.text`, not `payload.message`). Otherwise: `main cron jobs require payload.kind="systemEvent"`. Even if accepted, `main + systemEvent + wakeMode:"now"` runs through the heartbeat and is silently SKIPPED outside `agents.defaults.heartbeat.activeHours`. Do not use this shape for user-facing reminders.
2. **`sessionTarget` in `{"isolated", "current", "session:<id>"}`** REQUIRES `payload.kind: "agentTurn"` (and `payload.message`). Otherwise: `isolated/current/session cron jobs require payload.kind="agentTurn"`.
3. **`delivery.mode` MUST be `"none"` or `"webhook"` on this fleet.** If you omit `delivery` on an `isolated + agentTurn` job, OC defaults to `{mode: "announce"}` without a channel, and the server rejects with `delivery.channel is required when multiple channels are configured`. If you pass `{mode: "announce", channel: "telegram"}` it accepts at submit-time but fails at fire-time: `Telegram bot token missing for account "default"`. Always pass `{"mode": "none"}` and have the agent invoke `nbhd_send_to_user` itself.
4. **`schedule.at`** without an explicit timezone offset is treated as UTC by the gateway — silently, with no error. Always include `+09:00`, `-04:00`, etc. Raw `cron add` also rejects relative durations (`"20m"`); only the typed `nbhd_cron_create_*` tools accept those. See [Two ways to schedule](#two-ways-to-schedule-and-what-each-one-accepts).
5. **`kind:"at"` jobs auto-delete after a successful run; `kind:"cron"` and `kind:"every"` do not.** Manage recurring lifecycle via `cron remove`.
6. **`schedule.everyMs` is MILLISECONDS**, and the runtime has no lower bound of its own — it will happily accept `3600` and fire a full agent turn every 3.6 seconds. Hourly is `3600000`, every 15 minutes is `900000`, daily is `86400000`. The typed tools reject anything under `60000`.

## Editing or disabling

- Always explain what you're changing and why before doing it.
- Present changes with approve/reject buttons (for recurring tasks).
- Never silently modify or disable a user's tasks.
- One-off reminders are typically not edited — just cancel and create a new one.

## Timezone

- Always use `userTimezone` from your config. Use the Area/Location form (`Asia/Tokyo`); never an `Etc/*` name, whose sign is inverted (`Etc/GMT+9` is UTC *minus* 9) and which is rejected.
- Never default to UTC — if unknown, ask the user.
- For `kind: "at"` ISO timestamps, include an explicit offset matching the user's timezone. A bare timestamp is read as UTC.
- For `kind: "cron"` through raw `cron add`, always pass `tz: "<userTimezone>"` — without it, OC evaluates the expression in the gateway host timezone, not the user's. The typed `nbhd_cron_create_*` tools fill it in from the user's profile when you omit it.

## Hard limits

- Max 10 **recurring** scheduled tasks per account (4 system tasks count toward this). One-off `kind:"at"` reminders do NOT count.
- Soft cap: 20 concurrent one-off reminders. If hit, ask the user before adding more.
- If at the recurring limit, tell the user and suggest removing one first.

## Week Ahead Review

Monday morning: pull calendar (`nbhd_calendar_list_events`), recent journal context, and active crons (`cron list`). For each cron ask "does this still fit?" — pause/adjust as needed. Always tell the user before changing anything. Re-enable paused crons the following week. If user mentions travel/deadlines/sick days mid-week, re-check crons immediately.

## Background Tasks: Stay Silent

The Background Tasks cron is invisible to the user by design:

- ❌ Do NOT call `nbhd_daily_note_append` or `nbhd_daily_note_set_section`
- ✅ Use `nbhd_memory_update` to update long-term memory
- ✅ Write to `memory/YYYY-MM-DD.md` via workspace file tools
- ✅ May call `nbhd_journal_context` to read — but do not write back
- ✅ Send `nbhd_send_to_user` ONLY for something urgent (not routine summaries)
