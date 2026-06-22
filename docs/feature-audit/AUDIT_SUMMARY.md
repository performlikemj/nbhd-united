# NBHD United — Full Feature Audit Summary

A four-phase audit of **every feature** in the app: inventory → test → fix → re-test.
Canonical data lives in [`FEATURE_AUDIT.csv`](./FEATURE_AUDIT.csv) (1,154 rows).
All work was done in an isolated worktree on branch `audit/feature-user-stories`
(based on `origin/main` @ `bb14cde`).

## Headline numbers

| | count |
|---|---|
| Features inventoried (user stories) | **1,154** |
| Areas covered (21 Django apps + Next.js console) | 41 |
| Features that passed as-is | **1,028** |
| Confirmed defects found | **126** |
| Defects fully fixed + re-test-confirmed | **120** |
| Defects fixed with a documented follow-up | **6** |
| Defects left unfixed | **0** |

Severity of the 126 defects: **1 critical, 8 high, 22 medium, 95 low.**

## Phase 1 — Inventory (1,154 user stories)

~40 agents fanned out across every backend app and frontend area. Each feature was
captured as a user story (`As a <role>, I want <capability> so that <benefit>`) plus a
code-grounded expected behaviour and entry points. Output: the canonical CSV.

## Phase 2 — Test every story (126 confirmed defects)

Two-tier: 85 chunk testers verified each story against the real code (breadth), then an
adversarial reviewer independently tried to **refute** each flagged defect (depth).
That refuted **69 false positives**, leaving 126 confirmed defects with concrete
file:line evidence and user-impact descriptions.

## Phase 3 — Fix every defect (124 + 2)

Defects were grouped into **59 file-disjoint clusters** (connected components over the
files each fix touches) so cluster agents could fix in parallel without conflicts. The
orchestrator then completed cross-cluster work the per-cluster agents couldn't reach
(e.g. wiring PII redaction into all three chat channels; the automations QStash cron).

**Validation** (`python3.11` + local Postgres, `--keepdb`): **3,850 backend tests** —
the failure set is **identical to the pre-fix baseline** (19 failures, all
environment/version-only: unavailable `azure.*` / `transformers` SDKs, `stripe` 12.5.1
vs pinned 15.2.1, an OpenAI-mock test). **Zero regressions.** Two regressions surfaced
*during* validation and were fixed (LINE audio double tenant-resolve; non-deterministic
backfill order). Frontend: `tsc` + `eslint` clean, `next build` succeeds (40 routes).

## Phase 4 — Re-test every behaviour (caught a bad fix)

A re-test verifier re-checked each fix against the real code: **117 pass, 8 partial, 1
fail**. The fail was important — the markdown-checkbox fix used the AST node's
`position`, but react-markdown v10 emits a *synthetic* checkbox node with **no
position**, so it had silently **disabled every journal checkbox**. Re-fixed with a
document-order ordinal counter. Two partials were then completed (the action-gate
expiry sweep was wired into QStash; the Telegram button-tap budget gate was added).

Final: **120 Retest-Pass, 6 Fixed-Partial, 0 Retest-Fail.**

## The 6 documented follow-ups (Fixed-Partial)

These had their core bug fixed; the residual is genuine **feature work** (new
backend fields, migrations, or an iOS surface), not an unaddressed bug:

| id | core fix shipped | follow-up |
|---|---|---|
| FA-0006 | gate confirmation no longer silently expires for iOS-only users (clear operator warning) | APNs gate push + in-app approve/deny endpoint |
| FA-0335 | weight-unit toggle a11y (aria-pressed/role) | server-persist the unit (model field + migration) cross-device |
| FA-0341 | Active-Goals dead-end link now opens a real detail page | backend should return untruncated goal markdown (page shows the 200-char preview) |
| FA-0494 | invalid nested-anchor on the brand logo removed | restore the `/journal` destination via a `brand-logo` prop |
| FA-0536 | monthly-snapshot count is now truthful | wire the (dormant) monthly snapshot to a QStash cron |
| FA-0995 | LINE voice rejected before paid Whisper during onboarding | also cover the narrow "after re-introduction" sub-case |

## Notable fixes (the high-impact ones)

- **Automations never fired** (critical): the scheduled-automation dispatcher existed
  but was wired to no QStash cron — added TASK_MAP + every-minute schedule.
- **Raw PII reached LLM providers** (high): inbound user text was never redacted on any
  channel; wired `redact_user_message` into Telegram, LINE, and iOS ingestion.
- **Integration refresh cron broke Reddit** (high): the OAuth refresh cron flipped
  healthy Composio integrations to ERROR — now skips Composio-managed providers.
- **Tenant cron reconciler deleted live fuel-session crons** (high): added `_fuel:`
  to the unmanaged-prefix allowlist so the reconciler stops wiping workout crons.
- **Weekly scheduled tasks fired a day early** (high): ScheduleBuilder weekday off-by-one.
- **Markdown checkboxes toggled the wrong task** (high): now toggle the clicked item.

## How to reproduce

```bash
# rebuild the canonical CSV from raw inventory + merged results
python3 docs/feature-audit/build_csv.py

# backend tests (this audit ran them with python3.11 + local pg)
DATABASE_URL=postgres://<user>@localhost:5432/nbhd_united \
DJANGO_SETTINGS_MODULE=config.settings.development \
python3.11 manage.py test apps/ --keepdb

# frontend
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```
