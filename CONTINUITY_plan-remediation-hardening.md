# Planning Ledger: Remediation Hardening

Parent: `CONTINUITY.md`
Root: `CONTINUITY.md`

## Objective
Plan and sequence remediation for review findings across Stripe billing, webhook/auth hardening, frontend onboarding behavior, and logout/session handling.

## Work Breakdown
| ID | Task | Ledger | Status | Owner | Depends On |
|----|------|--------|--------|-------|------------|
| 1 | Stripe API key wiring + configuration guards for checkout/portal | CONTINUITY_remediation-hardening.md | complete | codex | — |
| 2 | Telegram webhook secret hardening (reject when secret missing, safer compare) | CONTINUITY_remediation-hardening.md | complete | codex | — |
| 3 | Frontend onboarding side-effect fix (`useEffect`-based auto-onboard) | CONTINUITY_remediation-hardening.md | complete | codex | — |
| 4 | Logout/session revocation (refresh token invalidation + client call) | CONTINUITY_remediation-hardening.md | complete | codex | 1 |
| 5 | Remove hardcoded onboarding domain in router fallback copy | CONTINUITY_remediation-hardening.md | complete | codex | — |
| 6 | Coverage + docs pass (tests, env/docs updates, regression check) | CONTINUITY_remediation-hardening.md | complete | codex | 1,2,3,4,5 |

## Delegation Rules
- Create child task ledgers only when execution starts.
- Keep changes minimal and grouped by bounded risk area.
- Add/extend tests with each remediation before moving to the next item.

## Dependency Graph
1,2,3,5 can run in parallel  
4 depends on 1  
6 depends on 1,2,3,4,5

## Collected Rollups
- [2026-02-10] Review findings identified 2 high-priority and 3 medium/low-priority remediations requiring coordinated backend+frontend changes.
- [2026-02-10] All remediation items 1-6 implemented and validated (`manage.py test apps`, frontend lint/build, django check).

## Decisions
- Sequence execution by risk: P1 fixes first (Stripe wiring, webhook secret enforcement), then behavior correctness (onboarding effect, logout revocation), then messaging/config cleanup.
- Treat logout revocation as backend+frontend contract work, not frontend-only token clearing.

## Blockers
- None currently.

## State
- Done:
  - Consolidated review findings and mapped them to remediation work items.
  - Completed remediation items 1-6 with tests and docs updates.
- Now:
  - Awaiting user review/approval.
- Next:
  - Roll remediation changes into next deployment cycle after environment secret updates.
