# Read-reminders micro-fix handback

## Change

- Extended the datebook MUST-call read contract to cover reminders, to-dos, and task-completion questions.
- Updated both envelope contract assertions to require the expanded clause.
- No other product behavior changed.

## Envelope budget

Measurements used identical mocked reminder counts, freshness, and busy-day inputs before and after the text change.

| Measurement | Before | After | Delta | 1,200-char headroom after |
|---|---:|---:|---:|---:|
| MUST-call clause | 156 | 192 | +36 | — |
| Eight-day envelope | 1,083 | 1,119 | +36 | 81 |
| Overflow envelope | 1,089 | 1,125 | +36 | 75 |

No section rebalancing was needed. The focused hard-bound and overflow tests also pass with the expanded clause.

## Verification

- `ruff format --check apps/datebook/envelope.py apps/datebook/test_b2a.py` — PASS (2 files already formatted)
- `ruff check apps/datebook/envelope.py apps/datebook/test_b2a.py` — PASS
- Focused envelope suite — PASS (2 tests)
- `make docker-gate` — PASS
  - Backend: 7,851 tests passed; 2 skipped
  - Config validator: PASS
  - Security audit: PASS
  - Frontend lint: PASS with 4 existing warnings and 0 errors
  - Frontend static build: PASS

## Gate tail

```text
○  (Static)  prerendered as static content
●  (SSG)     prerendered as static HTML (uses generateStaticParams)

=== FRONTEND LEG: PASS ===

=== DOCKER CI-PARITY GATE: PASS ===
```
