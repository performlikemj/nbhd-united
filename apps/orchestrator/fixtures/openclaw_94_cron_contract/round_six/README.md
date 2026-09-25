# Round six shape matrix

75 synthetic captures from the exact local fleet image; 70 accepted cases map to 43 proven normalized control shapes. Each accepted shape has an unchanged-writer round trip and two zero-mutation, stable-ID reconciliation passes. Raw offset/UTC one-shots require preparation; their normalized shapes are proven by the prepared captures.

All six CronPattern models are derived through the real typed pre-save receiver, rendered and signed by the unchanged selector. Each is exercised with cron, every and at schedules, both with and without fallbacks. Individual controls, restricted tools, text aliases, explicit main agent, destination-free announce, nullable optionals and legacy numeric/atMs/expr one-shots are also captured.

Only message/description text and validated schedule data vary within a proven shape. Control values and their combinations remain literal. Unknown values/combinations, other tool lists or model variants need new real-image evidence; a passing component does not admit untested combinations.

| Captured case | Migration disposition | Reason / preparation |
|---|---|---|
| `agent-main` | ACCEPT | fixture-proven |
| `announce-telegram` | ACCEPT | fixture-proven |
| `announce` | ACCEPT | fixture-proven |
| `at-expr-stable` | ACCEPT | authored instant → stable ISO |
| `at-ms-stable` | ACCEPT | authored instant → stable ISO |
| `at-numeric-stable` | ACCEPT | authored instant → stable ISO |
| `at-offset-stable` | ACCEPT | authored instant → stable ISO |
| `at-offset` | ACCEPT | authored instant → stable ISO |
| `at-utc-stable` | ACCEPT | authored instant → stable ISO |
| `at-utc` | ACCEPT | authored instant → stable ISO |
| `control-fallbacks` | ACCEPT | fixture-proven |
| `control-lightContext` | ACCEPT | fixture-proven |
| `control-model` | ACCEPT | fixture-proven |
| `control-timeoutSeconds` | ACCEPT | fixture-proven |
| `control-toolsAllow` | ACCEPT | fixture-proven |
| `cron-top-hour` | ACCEPT | fixture-proven |
| `cron-tz` | ACCEPT | fixture-proven |
| `delivery-default` | ACCEPT | fixture-proven |
| `delivery-empty` | ACCEPT | fixture-proven |
| `delivery-explicit` | BLOCKED_UNSUPPORTED | delivery_accountId, delivery_threadId, delivery_to |
| `description` | ACCEPT | fixture-proven |
| `disabled` | BLOCKED_UNSUPPORTED | disabled_not_projected |
| `every-anchor` | BLOCKED_UNSUPPORTED | interval_anchor |
| `every` | ACCEPT | fixture-proven |
| `light-false` | ACCEPT | fixture-proven |
| `null-all-optionals` | ACCEPT | fixture-proven |
| `null-anchor` | ACCEPT | fixture-proven |
| `null-fallbacks` | ACCEPT | fixture-proven |
| `null-lightContext` | ACCEPT | fixture-proven |
| `null-model` | ACCEPT | fixture-proven |
| `null-optionals` | ACCEPT | fixture-proven |
| `null-timeoutSeconds` | ACCEPT | fixture-proven |
| `null-toolsAllow` | ACCEPT | fixture-proven |
| `system-isolated` | BLOCKED_UNSUPPORTED | unsupported_declaration |
| `system-main` | BLOCKED_UNSUPPORTED | system_event_no_deliver |
| `text-alias` | ACCEPT | fixture-proven |
| `tools-string` | ACCEPT | fixture-proven |
| `tools-wildcard` | ACCEPT | fixture-proven |
| `typed-daily_briefing-at-fallbacks` | ACCEPT | authored instant → stable ISO |
| `typed-daily_briefing-at` | ACCEPT | authored instant → stable ISO |
| `typed-daily_briefing-cron-fallbacks` | ACCEPT | fixture-proven |
| `typed-daily_briefing-cron` | ACCEPT | fixture-proven |
| `typed-daily_briefing-every-fallbacks` | ACCEPT | fixture-proven |
| `typed-daily_briefing-every` | ACCEPT | fixture-proven |
| `typed-domain_summary-at-fallbacks` | ACCEPT | authored instant → stable ISO |
| `typed-domain_summary-at` | ACCEPT | authored instant → stable ISO |
| `typed-domain_summary-cron-fallbacks` | ACCEPT | fixture-proven |
| `typed-domain_summary-cron` | ACCEPT | fixture-proven |
| `typed-domain_summary-every-fallbacks` | ACCEPT | fixture-proven |
| `typed-domain_summary-every` | ACCEPT | fixture-proven |
| `typed-pure_reminder-at-fallbacks` | ACCEPT | authored instant → stable ISO |
| `typed-pure_reminder-at` | ACCEPT | authored instant → stable ISO |
| `typed-pure_reminder-cron-fallbacks` | ACCEPT | fixture-proven |
| `typed-pure_reminder-cron` | ACCEPT | fixture-proven |
| `typed-pure_reminder-every-fallbacks` | ACCEPT | fixture-proven |
| `typed-pure_reminder-every` | ACCEPT | fixture-proven |
| `typed-quote_user_intent-at-fallbacks` | ACCEPT | authored instant → stable ISO |
| `typed-quote_user_intent-at` | ACCEPT | authored instant → stable ISO |
| `typed-quote_user_intent-cron-fallbacks` | ACCEPT | fixture-proven |
| `typed-quote_user_intent-cron` | ACCEPT | fixture-proven |
| `typed-quote_user_intent-every-fallbacks` | ACCEPT | fixture-proven |
| `typed-quote_user_intent-every` | ACCEPT | fixture-proven |
| `typed-task_hygiene-at-fallbacks` | ACCEPT | authored instant → stable ISO |
| `typed-task_hygiene-at` | ACCEPT | authored instant → stable ISO |
| `typed-task_hygiene-cron-fallbacks` | ACCEPT | fixture-proven |
| `typed-task_hygiene-cron` | ACCEPT | fixture-proven |
| `typed-task_hygiene-every-fallbacks` | ACCEPT | fixture-proven |
| `typed-task_hygiene-every` | ACCEPT | fixture-proven |
| `typed-workout_congrats-at-fallbacks` | ACCEPT | authored instant → stable ISO |
| `typed-workout_congrats-at` | ACCEPT | authored instant → stable ISO |
| `typed-workout_congrats-cron-fallbacks` | ACCEPT | fixture-proven |
| `typed-workout_congrats-cron` | ACCEPT | fixture-proven |
| `typed-workout_congrats-every-fallbacks` | ACCEPT | fixture-proven |
| `typed-workout_congrats-every` | ACCEPT | fixture-proven |
| `wake-heartbeat` | ACCEPT | fixture-proven |

Also blocked before mutation: unknown declaration fields/policies, unsupported session/thinking/pacing/retention controls, unmanaged recurrences, conflicting aliases or authored instants, state-only one-shots, empty/invalid tool lists and every unproven normalized shape. Generated anchor/stagger observations are ignored only with canonical evidence that they are unpinned. Null optional pins are unpinned.

The standalone controls are recorded values (`v4-flash`, `v4-pro`, timeout 120, tools `nbhd_send_to_user`, light context true); typed fixtures record their actual model IDs, timeouts and restricted lists. Other values are not implicitly accepted.

Fixtures are synthetic; CLI projection evidence does not establish production delivery. Production was not accessed.
