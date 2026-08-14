# Handback — Reminder word routing

## What changed

- `apps/datebook/envelope.py`
  - The delivery-ready `## Calendar & Reminders` contract now sends bare or ambiguous user-authored `"remind me"` requests to `nbhd_datebook_add_apple_reminder` by default.
  - `nbhd_cron_create_pure_reminder` is reserved for explicit in-chat pings, nudges, or messages and inherently conversational recurring check-ins.
  - Genuine ambiguity resolves to the approval-gated Apple reminder.
  - The clause remains behind `datebook_delivery_ready()`; non-ready tenants are unchanged.
- `runtime/openclaw/plugins/nbhd-automation-tools/index.js`
  - Additive description text identifies the cron reminder as a chat ping, not Apple Reminders, and points datebook-ready `"remind me"` asks to `nbhd_datebook_add_apple_reminder`.
  - Tool schemas are unchanged.
- Tests pin delivery-ready inclusion, non-ready omission, budget survival, and the plugin-description contract.
- Siri's cron-pinned fast chain was not changed.

## Envelope budget

Measured with the same deterministic fixed eight-free-day fixture before and after the contract change:

| Measurement | Before | After | Delta |
|---|---:|---:|---:|
| Default rendered section | 702 chars | 1,048 chars | +346 |
| Required/uncullable floor (`max_chars=0`) | 529 chars | 875 chars | +346 |
| Headroom below the 1,200-char hard cap | 498 chars | 152 chars | -346 |

The hard cap remains 1,200 characters. Existing bounded and overflow tests pass, and both routing lines survive culling.

## Verification

- Focused Django suite: 16 tests passed.
- Ruff format/check and `git diff --check`: passed.
- `make docker-gate`: passed end to end.
  - Backend lint, formatting, secret scan, migration drift, system checks, and full Django suite passed (7,842 tests collected).
  - Frontend lint passed with 0 errors and 4 existing warnings; the production static build passed.

## Convergence

- Envelope contract: active on the next managed-envelope/`USER.md` refresh after the Django deploy.
- OpenClaw cron description: active with the next OpenClaw image containing the updated `nbhd-automation-tools` plugin.

No production access, push, or PR was performed.
