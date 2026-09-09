# Cron feed, console redactor, and chat delegation

Branch `fix/cron-feed-redactor-subagent`, base `fbb00fdf`.
Directive: `DIRECTIVE_cron_feed_redactor_subagent.md`; the user's round-2
review supersedes its original inactive/unknown-tenant 422 requirement.

## Current state

Round-2 implementation, caller audit and verification are complete. Both Linux
Docker legs pass. REPORT.md carries results, source/test references and gate tail.

- D: use the exact lowercase bracket-slug, node fatal-frame and entrypoint
  shell rules supplied with the seven-day aggregate survey. Preserve explicit
  timestamp/httpx/access/JSON rules, whole-field masking and fail-closed errors.
  Every surveyed prefix has synthetic pass coverage; prose/emoji/time cases
  drop. 125 redactor tests pass. Commit `a70fcae8` is on the actual branch.
- C3: app feed persists without an eligible push token. Restore inactive tenant
  to HTTP 200/blocked/tenant_not_active and include inactive user in the same
  branch with reason user_inactive. Unknown tenant returns 404. Every non-200
  and blocked-200 response emits one content-free reject log. All seven
  production resolver callers and both direct-test caller modules are audited
  in REPORT.md. Shared recorder guard prevents tokenless app push scheduling;
  additional Friends/journal regressions verify feed rows and no sends.
- B4: existing tenant gate renders inline chat offload instructions, with no
  cron-index subagent row, wildcard change or fleet enablement. All-gates
  render is 24,333 characters; cap 26,000 and ceiling 25,950 remain unchanged.
  Ungated bytes are pinned to the pre-change SHA-256. 19 bridge tests pass.

## Decisions and scope

- Selecting a surface also labels provisioning profiles; it is not an
  entitlement check. No-transport profiles now say NBHD app. Cron entitlement
  checks occur before persistence. Linked Telegram/LINE ordering stays intact;
  revoked tokens no longer steal their route. Eval sinks stay isolated.
- Core readiness, sautai readiness, system notices, Friends digests and journal
  summaries all record app feed rows through the same eligibility-guarded
  recorder. Interactive gates use an independent resolver and are unaffected.
- Expected suspended/inactive non-delivery returns 200 to suppress retries.
  Reject logging still counts these non-deliveries; it never includes message
  bodies or custom job text (custom names are hashed).
- Prefixes are an operational convention, not authenticated provenance.
  Unknown prose drops; content after admitted prefixes must use masked fields.
- No tenant data, raw logs or secrets read. The supplied aggregate prefix survey,
  code and synthetic fixtures were sufficient. Read CLAUDE.md plus architecture,
  invariants, debugging, workflow, backend and telemetry docs.
  CONTINUITY_rules_delivery.md is absent; existing directive/source/tests supplied
  its available context.

## Verification and Git

All changed Python files formatted; make lint passes; migration check reports
No changes detected. Focused Django: 701 pass; rules/workspace/budget: 36 pass; Django redaction:
10 pass. Exactly one round-2 Docker gate passed both Linux legs: 8,771 backend
tests in 694.760s, 36 skipped, no failures/errors. Config validation/security audit
and frontend lint/TypeScript/static build pass. All 22 changed source/test files
match the successful snapshot. Test/gate containers removed.
C3 commit `eaf29acf` is on the actual branch. Round-2 logs use `/tmp/cron-feed-redactor-round2-*`.
Tests use an isolated pgvector/Postgres 16 container and synthetic credentials.

The worktree index is writable in round 2. Three scoped Conventional Commits
are on the branch, staged by explicit paths: D `a70fcae8`, C3 `eaf29acf`, and
B4 at HEAD (`fix(orchestrator): deliver gated offload instructions inline in chat`).
REPORT.md, the directive and this ledger are included in B4. The ignored ledger
is force-added by its exact path. commit-patches/ remains untracked: its prior
exports are obsolete; use the actual branch commits. No push/PR. Nothing remains
blocked within the requested local scope.

## Deployment handoff

Orchestrator owns integration, push/PR and deployment. D needs the new runtime
image rolled out; C3 needs Django deployment. After deployment, refresh MJ's
per-tenant config/workspace when main is quiet so B4 reaches chat. Keep the
existing SUBAGENT_TENANT_IDS allowlist. Validate with synthetic probes and
operational metadata only.
