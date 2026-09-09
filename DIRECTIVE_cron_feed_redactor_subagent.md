# DIRECTIVE: cron feed-row delivery, tenant log redactor, sub-agent instruction (backend lane 1)

Owner: MJ · Author: Fable (orchestrator) · Executor: codex (gpt-6-astra, basecamp) · Date: 2026-09-09
Repo: nbhd-united · Branch: `fix/cron-feed-redactor-subagent` · Worktree: `~/Projects/.codex-worktrees/cron-feed-redactor` (basecamp)
Ledger: `CONTINUITY_cron_feed_redactor_subagent.md`

Three independent, small fixes found while investigating a lost iOS chat reply (2026-09-08). Each is its own
commit. Nothing else.

## D — tenant console redactor lets content through

Evidence (content-free): at 2026-09-08 11:48:49Z the `oc-1fed11d7-…` console log showed several lines of an
assistant reply (markdown list items of the form `- Label …`, `Label: …`, `[Label] …`) between `[nbhd:redact]
non-operational line dropped (N chars)` lines. Synthetic probing shows the classifier in
`runtime/openclaw/redact-stdout.js` (~line 139) passes arbitrary bracket-, timestamp-, key/value- and
manifest-like prefixes; other bypasses: field masking keeps first/last characters (~79), any successful field
replacement skips classification (~245), fatal-message matching admits broad text (~175), exceptions emit the
original line (~253, ~297).

Fix: invert the model — a line is emitted ONLY if it matches an explicit allow-list of operational shapes
(OpenClaw `[component]` prefixes, `[nbhd:*]` lines, httpx/access lines, timestamps + known loggers, JSON with known
keys); everything else becomes `non-operational line dropped (N chars)`. Field masking replaces the whole value.
Exceptions in the redactor drop the line, never echo it. Keep the existing counters. Tests: a table of ≥25
synthetic lines (assistant prose, markdown bullets with amounts, `Label: value`, `[Note] text`, JSON blobs, plus
every legitimate operational shape currently in the codebase's own log calls) with expected pass/drop; run the
existing redactor tests too. Do not read real tenant logs.

## C3 — cron deliveries rejected with 422 when no channel is linked

Evidence: ~81 `Unprocessable Entity: /api/v1/integrations/runtime/<t>/send-to-user/` in 7 days (vs ~25–45 × 200
per day); the model then files platform issues (`missing_capability tool=nbhd_send_to_user summary=Evening
check-in… / Morning Briefing… / Weekly Reflect…`). Source: `apps/router/cron_delivery.py` — the ONLY 422 path is
channel resolution returning `None` (~line 503: no eval sink, no `DeviceToken` row, no Telegram chat id, no LINE
user id; ~402/409/416). Consequence: an app-only user who never allowed notifications gets NO briefing at all — the
row is not even written to the app feed. Also a mismatch: the resolver accepts any token row (~409) while push
eligibility excludes revoked tokens / inactive users (`apps/router/push_views.py` ~375).

Fix: (1) when the tenant's channel is `app` (or unknown) and the user is active, ALWAYS persist the app feed row
(`record_proactive_outbound` / the same path a successful app delivery uses, with the requested `thread_id`) and
return 200 with `delivered_to=["app_feed"]`; attempt push only if an eligible token exists; 422 remains only for
inactive/unknown tenants. (2) Align resolver eligibility with push eligibility. (3) Add ONE content-free reject log
line on every non-200 branch: `cron_delivery: rejected tenant=<8> job=<name> reason=<no_channel_linked|inactive|…>
status=<n>`. (4) Do not change Telegram/LINE behaviour. Tests: app-only tenant with no token → 200 + feed row +
no push attempt; revoked-token tenant → same; inactive tenant → 422 + log line; existing
`apps/router/test_push.py`, `test_proactive_push.py`, cron_delivery tests stay green. Migration only if truly
needed (`manage.py makemigrations --check --dry-run`).

## B4 — sub-agent offload instruction never reaches chat

Evidence: `templates/openclaw/rules/subagents.md` (the ">30 s → `sessions_spawn` + 'On it — I'll let you know when
it's ready'" rule) is indexed only by `_cron_rule_index` (`apps/orchestrator/config_generator.py` ~233); chat
`templates/openclaw/AGENTS.md:122` says "There are no files to read in chat"; NO session has a `read` tool
(tool policy = group:openclaw + group:plugins). `SUBAGENT_TENANT_IDS` in prod = one tenant (MJ). So the tool is
unlocked for MJ with no instruction to use it, and cron spawning is blocked anyway (`subagent-bridge/index.js` ~127).

Fix: render a short inline block into the CHAT `AGENTS.md` ONLY when `subagents_enabled(tenant)` (same gate that
unlocks the tools), carrying the substance of `rules/subagents.md`: delegate only work likely >~30 s (multi-step
research, long-document analysis, large generation); call `sessions_spawn` BEFORE starting; reply immediately
"On it — I'll let you know when it's ready."; never `context: "fork"`; helper is read-only and reports back; the
completion event → one `nbhd_send_to_user` to the requester's thread (the bridge backstops this — do not duplicate
its machinery). Keep within `BOOTSTRAP_MAX_CHARS` / `_ALL_GATES_CEILING` (raise cap+ceiling together only if the
gated block cannot fit; see `test_rules_delivery_r0_all_gates_budget`). Remove the now-dead `rules/subagents.md`
from the cron index (cron cannot spawn). Do NOT touch the `*` wildcard (fleet enablement is a separate decision).
Tests: gated tenant render contains the block and no `rules/` pointer; non-gated render unchanged byte-for-byte;
budget tests green; `test_rules_delivery.py` updated for the removed cron index row.

## Acceptance

- Each item = one commit, tests included, `make lint` + `.venv/bin/ruff format <files>` +
  `manage.py makemigrations --check --dry-run` clean, then `make docker-gate` (both Linux legs) green — one gate at
  a time on this machine; if a `nbhd-docker-gate` container is already running, wait for it.
- `CONTINUITY_cron_feed_redactor_subagent.md` written and committed: state, decisions, evidence, what MJ must do
  after deploy (B4 needs a per-tenant config refresh for MJ's tenant when main is quiet).

## Rules

- Read `CLAUDE.md`, `docs/agents/debugging.md`, `docs/agents/workflow.md`, `CONTINUITY_rules_delivery.md` first.
- Privacy ladder: code + synthetic fixtures only; never read tenant rows, real logs, or secrets.
- GIT SAFETY: work ONLY in the worktree above. Never touch the main checkout, never stash, never demand an empty
  stash, never `git add -A`/`.`, never `--no-verify`, never rebase/force. Stage by path. Scoped Conventional
  Commits. Do NOT push and do NOT open a PR — the orchestrator does.
- Report (final message AND `REPORT.md` in the worktree root): per item what changed with file:line, tests
  added/changed, gate output tail, anything not done and why.
