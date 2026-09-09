# Cron feed, redactor, and chat delegation — round 2

Branch: `fix/cron-feed-redactor-subagent`; base: `fbb00fdfda08fcb0d3a3ac52bb6140e53f365cc9`.
Round 2 restores the deliberate scheduler response contracts. No tenant rows,
real tenant logs, or secrets were read; prefix coverage uses the supplied
seven-day aggregate survey and synthetic fixtures.

## C3 — app feed and response contracts

- `apps/router/cron_delivery.py:358`: eligible app device → Telegram → LINE →
  app feed; explicit eval sinks still win. A transportless user can receive a
  feed row without allowing notifications.
- `apps/router/cron_delivery.py:406`: one content-free reject log for every
  non-200 response AND every `status=blocked` response. Reasons are allowlisted;
  custom job names are hashed, and tenant IDs are shortened to eight characters.
- `apps/router/cron_delivery.py:460`: unknown tenant returns **404** with
  `{"error":"tenant_not_found"}`. Inactive/suspended tenant returns **200** with
  `{"status":"blocked","reason":"tenant_not_active"}`; inactive user shares
  that branch with reason `user_inactive`. Expected non-delivery must not cause
  QStash/cron retries. Neither branch persists or sends.
- `apps/router/cron_delivery.py:694`: successful app persistence returns
  `delivered_to=["app_feed"]`, retaining the requested tenant-owned thread.
- `apps/router/push_views.py:347` shares device eligibility between routing and
  fan-out. `apps/router/proactive_context.py:262` guards app push scheduling:
  no eligible token means return the persisted row without invoking a worker.
- Tests: `apps/router/test_cron_delivery.py:83` exercises a real unknown-tenant
  response and its single log; the status table now expects 404.
  `apps/router/test_proactive_push.py:646` asserts exact 200/blocked bodies,
  separate reasons, one content-free log, no feed row and no push.
  `apps/cron/tests/test_suspension.py:226` is restored to its original 200
  contract (therefore no remaining diff in that file).
  Existing no/revoked-token, requested-thread/feed, eval isolation and routing
  regressions remain. `apps/friends/test_pr6.py:352` and
  `apps/journal/test_extraction.py:191` now verify tokenless feed delivery with
  no push, Telegram or LINE send.

### Exhaustive resolve_user_channel caller audit

Searched the entire repository with `rg -n 'resolve_user_channel'` (excluding
obsolete exported patches), then checked actual Python calls with AST. There
are **seven production call sites**; imports and historical prose are not calls.

| Caller (call line) | Effect for an active user with no transport/token |
| --- | --- |
| `apps/router/cron_delivery.py:826`, `CronDeliveryView._resolve_channel` | Returns app; endpoint entitlement branch runs first, then persists to the requested feed thread. Shared recorder guard prevents push scheduling. |
| `apps/core/services.py:798`, `notify_meditation_ready` | Previously skipped; now records an app readiness notice. Active-tenant check remains. App branch never invokes Telegram/LINE; recorder eligibility guard prevents tokenless push. Core regression asserts feed plus no push/send. |
| `apps/integrations/sautai_notify.py:45`, `notify_sautai_plan_ready` | Previously skipped; now records an app meal-plan readiness notice. Active-tenant check remains. App branch and recorder guard prevent transport/push calls. Updated sautai regression covers this. |
| `apps/router/system_notify.py:58`, `_resolve_channel` | System notice uses `_send_app` and persists a feed row; returns whether persistence succeeded. Shared guard prevents tokenless push; no Telegram/LINE branch executes. |
| `apps/friends/digest.py:87`, `_deliver_text` | Weekly digest now persists an app feed row and reports delivery on persistence. Shared guard prevents tokenless push; no messaging send occurs. New regression covers all three send boundaries. |
| `apps/journal/extraction.py:648`, `run_extraction_for_tenant` | Extraction/reconciliation already ran; now its nonempty summary also reaches the app feed. Shared guard prevents tokenless push; no messaging summary sender runs. Existing linked-but-unconfigured messaging fallback is unchanged. Updated regression checks persistence and all send boundaries. |
| `apps/tenants/envelope.py:50`, `_resolve_delivery_channel_label` | Profile/USER.md now labels a transportless user `NBHD app` instead of omitting the label, including provisioning renders. Labels have no send side effects; they are not entitlement checks. Both tenant and orchestrator profile fixture suites cover the labels. |

Interactive gates are **not callers**: `apps/actions/messaging.py:392`
`_resolve_gate_channel` has independent originating-channel / Telegram / LINE /
datebook-gateway-or-device selection. This change neither enables a gate nor
invokes a tokenless gate push. The actions cron-messaging suite is included.
Persona channel comments also are not resolver calls; no persona gate changes
arise from C3. Delivery eligibility remains the callers' responsibility; this
selector also supports provisioning labels and is not an authorization API.

For completeness, the only direct **test callers** are:

- `apps/router/test_proactive_push.py:372–437`: `test_token_beats_telegram_and_line`,
  `test_app_token_wins_over_telegram`, `test_token_only_resolves_to_app`,
  `test_telegram_only_resolves_to_telegram`, `test_line_only_resolves_to_line`,
  `test_telegram_beats_line_without_token`, `test_no_transport_resolves_to_app_feed`,
  `test_revoked_token_does_not_override_linked_messaging`. They assert unchanged
  linked ordering, eligible-device precedence, revoked-device fallback and the
  new app-feed default; they do not send.
- `apps/router/test_eval_sink_channel.py:55–138`:
  `test_eval_sink_tenant_with_no_channel_resolves_to_the_sink`,
  `test_real_tenant_with_no_transport_has_app_feed`,
  `test_synthetic_demo_tenant_keeps_normal_channel_behavior`,
  `test_synthetic_demo_without_a_channel_is_not_an_eval_sink`,
  `test_eval_sink_preempts_a_linked_channel`, `test_eval_sink_preempts_a_registered_device`,
  `test_eval_sink_always_wins_regardless_of_linked_surfaces`,
  `test_without_the_flag_the_app_first_order_holds`. The non-sink transportless
  expectations become app; explicit eval behavior stays isolated. These calls
  only assert selection and never send.

## D — surveyed operational prefixes, complete masking, fail closed

- `runtime/openclaw/redact-stdout.js:72`: admit exactly the required lowercase
  bracket slug shape `^\[[a-z][a-z0-9_:./-]{0,40}\] `, node fatal frame shape
  `^\[\d+:0x[0-9a-f]+\] `, and the nine named entrypoint shell prefixes.
  Explicit timestamp/logger, httpx, access, startup and structured-JSON rules
  remain. Unknown prose, uppercase/spaced brackets, malformed JSON and decoder
  failures drop. Admitted fields still undergo whole-value masking.
- `runtime/openclaw/redact-stdout.test.mjs:279`: synthetic pass table includes
  all 23 prefixes from the supplied seven-day survey, including tool-policy,
  registry, shutdown, watcher, entrypoint, bracket tools-invoke, curl/rm/cp,
  node fatal frames and openrouter startup lines. Drop cases include Here,
  Still, Ping!, Got, 🏡 and 3am; slug boundary and field-masking checks added.
- **125 Node tests pass** using the documented command:
  `NBHD_REDACT_STDOUT_DISABLE_AUTOINSTALL=1 node --test runtime/openclaw/redact-stdout.test.mjs`.
  The bare command intentionally fails the test file's environment precondition;
  the documented command passes. Operational prefixes remain a convention:
  call sites must put free content in masked fields.

## B4 — inline gated chat delegation

- `apps/orchestrator/personas.py:540` defines the inline >30-second offload
  contract, rendered at `:706` only by the existing subagent tenant gate:
  spawn first, acknowledge immediately, no fork, read-only helper, one completion
  send to the requesting thread, bridge backstop, honest timeout/failure handling.
- `apps/orchestrator/config_generator.py:231` removes the dead subagent cron
  index row. No wildcard, fleet allowlist or workspace rule compatibility change.
- `apps/orchestrator/test_rules_delivery.py:76` covers gated persona renders;
  `:100` pins ungated bytes to the pre-change SHA-256. Workspace-index and
  reminder-budget tests cover the removed row and all-gates render. Budget
  remains 24,333 characters against ceiling 25,950 / cap 26,000.
- **19 Node bridge tests pass**. Django rules/budget/redaction results below.

## Round 2 verification and commits

- Formatting: all changed Python files formatted; `make lint`: PASS.
- `manage.py makemigrations --check --dry-run`: `No changes detected`.
- Focused Django suites: **701 pass** (93.660s). Modules: router `test_push`,
  `test_proactive_push`, `test_proactive_context`, `test_cron_delivery`,
  `test_cron_delivery_placeholder_at_rest`, `test_reply_text`, `tests_line`,
  `test_eval_sink_channel`, `test_system_notify`; Core `tests`; integrations
  `test_sautai_client`; cron `tests.test_suspension`; orchestrator
  `test_cron_envelope`; tenants `test_envelope`; Friends `test_pr6`; journal
  `test_extraction`; actions `test_cron_messaging`.
- Rules/workspace/budget: **36 pass**; Django redaction: **10 pass**
  (combined run: 46 tests, 0.622s). Modules: orchestrator `test_rules_delivery`,
  `test_workspace_rules`, `test_reminder_capability`, `test_log_redaction`.
  Django runs use `manage.py test <modules> --noinput --keepdb`, an isolated
  synthetic Postgres database, and background threads disabled.
- **Docker gate: PASS (both Linux legs)**. Exactly one round-2 invocation,
  after checking no `nbhd-docker-gate-*` container existed:

  ```sh
  TMPDIR=/Users/mjjones/Library/Caches/nbhd-docker-gate-tmp \
  DOCKER_GATE_CACHE=/Users/mjjones/Library/Caches/nbhd-docker-gate make docker-gate
  ```

  Gate result lines (backend summary plus final leg results):

  ```text
  Ran 8771 tests in 694.760s
  OK (skipped=36)
  Config validator: PASS
  Security audit: PASS
  === BACKEND LEG: PASS ===
  === FRONTEND LEG: PASS ===
  === DOCKER CI-PARITY GATE: PASS ===
  ```

  Full synthetic log: `/tmp/cron-feed-redactor-round2-docker-gate.log`.
  All 22 changed source/test files match the successful Docker snapshot.
  Frontend lint, TypeScript and static build pass. Test/gate containers removed.
- Three scoped commits on the actual branch: D `a70fcae8`; C3 `eaf29acf`;
  B4 is the final commit (`HEAD`), titled
  `fix(orchestrator): deliver gated offload instructions inline in chat`,
  including this report, the directive and continuity ledger.
- Worktree index is writable; all staging used explicit paths.
  `commit-patches/` remains untracked and contains obsolete round-1 exports;
  use the actual branch commits. No push or PR performed. Nothing remains
  blocked or unfinished within the requested local scope.

## Deployment handoff

Orchestrator owns integration, push/PR and deployment. D requires rolling the
updated OpenClaw runtime image; C3 requires Django deployment. After deployment,
refresh MJ's per-tenant config/workspace when main is quiet so B4 reaches chat.
Preserve the existing tenant allowlist. Verify with synthetic probes and
operational metadata only. `CONTINUITY_rules_delivery.md` remains absent from
this checkout; the directive and source/tests supplied that context.
