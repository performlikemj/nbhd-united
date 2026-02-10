# Planning Ledger: Google OAuth MVP

Parent: CONTINUITY.md
Root: CONTINUITY.md

## Objective
Deliver the MVP value chain: Telegram bot -> read Gmail -> check Google Calendar, using self-managed Google OAuth without adding a third-party integration vendor.

## Work Breakdown
| ID | Task | Ledger | Status | Owner | Depends On |
|----|------|--------|--------|-------|------------|
| 1 | OAuth hardening + identity enrichment (state checks, provider email capture, callback error mapping) | CONTINUITY_google-oauth-hardening.md | completed | codex | - |
| 2 | Automated token refresh scheduling (QStash cron task + failure state transitions) | CONTINUITY_google-oauth-refresh-scheduling.md | completed | codex | 1 |
| 3 | Token retrieval + access boundary (read tokens from Key Vault safely for runtime use) | (not created) | planned | codex | 1 |
| 4 | Runtime capability wiring (Gmail read + Calendar lookup path consumed by assistant runtime) | (not created) | planned | codex | 2, 3 |
| 5 | End-to-end validation (integration tests + conversational happy-path + revocation/expired-token paths) | (not created) | planned | codex | 4 |
| 6 | Production rollout checklist (env config, consent screen verification, staged release, observability) | (not created) | planned | codex | 5 |

## Delegation Rules
- Create child task ledgers only when execution starts.
- Keep control-plane OAuth tasks separate from runtime capability wiring.
- Child task rollups append here first, then roll up to CONTINUITY.md.

## Dependency Graph
1 -> 2  
1 -> 3  
2,3 -> 4 -> 5 -> 6

## Collected Rollups
- [2026-02-10] Baseline assessment: OAuth authorize/callback/token storage already exists; scheduled refresh and runtime consumption are not wired.
- [2026-02-10] Task 1 complete: provider-bound OAuth state validation, provider email enrichment, callback error mapping, and new view tests (`apps.integrations` + related suite passing).
- [2026-02-10] Task 2 complete: scheduled refresh task implemented + cron registration + token loading helper; integration suites passing.

## Decisions
- Build Google OAuth in-house for MVP; defer Composio unless scope expands to many providers.
- Scope MVP integrations to Gmail + Google Calendar.
- Reuse existing QStash cron infrastructure for refresh automation.
- Use Django proxy boundary for MVP Gmail/Calendar access; revisit direct runtime token handling after stabilization.

## Blockers
- @dependency: Google Cloud consent screen + redirect URI setup must be completed per environment.

## State
- Done: Gap assessment and practical timeline (4-8 dev days) completed.
- Now: Tasks 1 and 2 are complete.
- Next: Start Task 3 token retrieval/access-boundary implementation.
