# Reminder due/alarm preservation trace

## Outcome

No backend hop drops an already-present `items[].due` or `items[].alarm`. The live dateless reminder came from an alarm-only model payload: the command validator accepted the alarm and defaulted the omitted due to `{"kind":"none"}`. That value then remained faithful through the approval card, command creation, device claim, and iOS decoding.

The runtime create path now deterministically derives a zoned due from an absolute alarm when, and only when, the item has no `due` key. The approval card therefore receives the real date/time, gate expiry can clamp to it, and the device receives both the requested alarm and a dated reminder.

## Per-hop verdict

| Hop | Verdict | Evidence |
|---|---|---|
| Plugin create tool | Faithful | `requestCreate` sends `payload: {items: input.items}` without an item mapper or cleaner. A mocked fetch asserted deep equality and identical `JSON.stringify` output for a due+alarm item. |
| Runtime command validation | Existing due+alarm preserved | The COMMAND-path cleaner in `apps/datebook/services.py` allows, validates, and copies both fields. The unrelated sync-page cleaners are not used here. Before this fix, an omitted reminder due was defaulted to `due:none`. |
| Gate creation / stored `PendingAction.action_payload` | Faithful | The gate deep-copies the validated payload. Destination resolution removes only destination fields. The regression enables Layer-1 authoring: the title becomes `Call [PERSON_1]`, while the nested due and alarm remain exact. |
| `_rehydrated_command_fields` | Faithful | Owner rehydration restores `Call Alice` without changing due or alarm. |
| Approval -> `create_device_command` | Faithful | Approval passes the rehydrated payload directly into command creation. |
| Stored `DeviceCommand.payload` | Faithful | Device-command authoring masks registered human-text fields only; due and alarm are untouched. |
| Device claim / owner rehydration | Faithful | The claim returns the exact due+alarm objects asserted below. |
| iOS claim decoder and EventKit executor (read-only trace) | Faithful consumer | Missing due intentionally decodes as `.none`; the card reads only due. EventKit assigns `dueDateComponents` from due and adds `EKAlarm` independently from alarm. Thus an alarm-only backend payload deterministically produces a dateless reminder. No iOS files were changed. |

The suspected rounds did not introduce a field drop. Approval UX round #1454 changed descriptions/destination handling but does not remove due or alarm. Expiry-clamp round #1462 moved target derivation into the gate; it reads reminder due, so it made an existing alarm-only omission consequential for clamping without stripping either field.

## Drop point and fix

The decisive drop point is before the backend: the model sometimes chooses `alarm` for a named time and omits `due`. The prior runtime normalization then made the omission explicit as `due:none`.

The fix is at pre-gate command validation:

- Absent `due` + absolute alarm -> derive `due = {kind: zoned, due_at: trigger_at, tz_id: parsed trigger timezone}`.
- The trigger string is retained exactly as `due_at`.
- Fixed offsets become Swift Foundation-compatible identifiers such as `UTC+09:00`; a bare `+09:00` would be rejected by `TimeZone(identifier:)` on iOS.
- Due-only remains unchanged and does not invent an alarm.
- Both present remain verbatim, even when due and alarm conflict; the presence of an explicit due prevents derivation.
- Explicit `due:none` plus alarm remains unchanged.
- Relative-alarm-only behavior remains unchanged.

The create-tool descriptions now also require a named due date/time to populate `items[].due`, describe a named time as zoned, and state that alarm is an optional explicit alert rather than a substitute for due.

## Exact claimed command payloads

The end-to-end regression observed and asserted this exact `command.payload` for an explicit, deliberately conflicting due+alarm item. It proves that both existing fields survive storage, PII authoring/rehydration, approval, command creation, and claim verbatim:

```json
{
  "items": [
    {
      "title": "Call Alice",
      "due": {
        "kind": "zoned",
        "due_at": "2099-08-16T08:00:00+09:00",
        "tz_id": "Asia/Tokyo"
      },
      "alarm": {
        "kind": "absolute",
        "trigger_at": "2099-08-16T09:00:00+09:00"
      }
    }
  ]
}
```

The alarm-only regression observed and asserted this exact derived `command.payload`; it is suitable as the nested payload for an iOS replay:

```json
{
  "items": [
    {
      "title": "Pack my bags",
      "alarm": {
        "kind": "absolute",
        "trigger_at": "2099-08-16T08:00:00+09:00"
      },
      "due": {
        "kind": "zoned",
        "due_at": "2099-08-16T08:00:00+09:00",
        "tz_id": "UTC+09:00"
      }
    }
  ]
}
```

## Regression coverage

| Check | Result |
|---|---:|
| Failing-first focused Django/schema run | 5 run; 2 expected failures (`due:none` and missing schema guidance) |
| Post-fix focused Django/schema run | 5/5 pass |
| New due trace module | 5/5 pass |
| Adjacent datebook/schema regression set | 69/69 pass |
| Mock-only datebook plugin Node suite | 8/8 pass |
| Full Docker CI-parity gate | PASS: 7,870 tests, 35 skipped; frontend lint/build PASS |

The five due-trace tests cover alarm-only normalization without input mutation; due-only/both-present/explicit-none preservation; every backend hop for explicit due+alarm; every backend hop for alarm-only derived due; and a due-in-two-hours expiry clamp.

## Docker gate tail

`make docker-gate` exited 0. Its final result was:

```text
Ran 7870 tests in 1125.370s

OK (skipped=35)
Config validator: PASS
Security audit: PASS
=== BACKEND LEG: PASS ===
=== FRONTEND LEG: PASS ===
=== DOCKER CI-PARITY GATE: PASS ===
```

The frontend lint step completed with four pre-existing warnings and zero errors. `npm ci` reported five dependency audit findings; the gate does not run `npm audit`, and no dependency files changed in this round.
