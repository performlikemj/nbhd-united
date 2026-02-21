# PKM Loop Automation Setup

This doc wires review automation behavior in the current system.

## Existing automations in NBHD Unified

- `daily_brief`: existing automation kind in `apps/automations`
- `weekly_review`: existing automation kind in `apps/automations`

This repo currently supports DB-backed automation schedules (`apps/automations`) and a global due-run scheduler.

## Weekly review automation enhancement (already exists)

`apps/automations/services.py` now sends a PKM-oriented synthetic prompt for weekly automation runs.

Expected behavior:
- Ingests weekly context (`nbhd_journal_context` style flow)
- Produces a grouped weekly-draft proposal
- Optionally includes monthly-check branch on the 1st of month

## Required schedules (time is user-local)

If user wants explicit local-time timing and no external scheduler editing UI exists,
set these expressions in the cron UI / runtime scheduler:

### Daily reflection
- Cron: `0 21 * * *`
- Kind: `daily_brief`
- Rationale: end-of-day pass around 21:00 local time.

### Weekly review
- Cron: `0 19 * * 0` (Sunday evening) **or** `0 9 * * 1` (Monday morning)
- Kind: `weekly_review`
- Rationale: first reflective checkpoint of week.

### Monthly goals check
- Cron: `0 9 1 * *`
- Kind: `weekly_review` (mode checks day-of-month)
- Rationale: 1st of month goals reset/reprioritization.

> The first run day can be shifted per user preference (Sunday PM ↔ Monday AM window).

## Practical mapping inside NBHD

1. Create/update automations using admin or user-facing automations API/UI.
2. Use local timezone field per tenant so scheduled expressions stay at local clock time.
3. Keep `weekly_review` as the recurring weekly anchor; route monthly pass inside skill by checking date:
   - if `today.day == 1`, add `Monthly goals check` section before write confirmation.

## If only script-based cron is available

- Ensure global run task is configured at least every minute to process due runs:
  - `apps.automations.tasks.run_due_automations_task`
- Then schedule per-automation records in DB for tenant-specific times as above.
