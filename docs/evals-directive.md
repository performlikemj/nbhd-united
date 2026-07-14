# NBHD United — Evals Directive

**Master design for the platform evaluation system.** Companion to the per-suite code. This doc is the contract every suite conforms to; a suite that violates a standing invariant here is a bug, not a variant.

## 0. Why this exists

The platform has had recurring failures that every individual health check missed because **liveness is not correctness**: a container answers `/health/` 200 while the assistant is silent; a deploy serves stale code while all checks are green; a redaction regression ships because nothing measured it; a personality change never shipped because there was no way to prove it didn't regress. Evals close the gap between "the process is up" and "the product works and the models behave."

## 1. Standing invariants (violating one is a defect)

1. **No real-user content in the eval pipeline — ever.** Suites exercise synthetic tenants and synthetic fixtures. `EvalResult.details` and every eval log line carry counts, ids, latencies, scores, and rubric labels — never message text, never a decrypted value, never PII. This is what makes evals compatible with encryption-at-rest by construction. A reviewer rejects any diff that could route real content into an eval sink.
2. **Backend computes evidence; the LLM judges.** Deterministic assertions (did it call the tool, is the reply non-empty, did the marker survive redaction) are Python code. Only genuinely subjective dimensions (helpfulness, warmth) go to a judge, and the judge scores against a versioned rubric.
3. **A green eval must be a true eval.** No assertion may be weakened to force green. A defense not yet built is `xfail`/`skip` with a reason naming the missing remediation — never a passing test that implies a guarantee we don't have. Vacuous/tautological assertions are defects.
4. **One chassis, one schema.** Every suite runs through `apps/evals` (`EvalRun`/`EvalResult`) and, in production, through a zero-arg QStash task in `TASK_MAP` (the crypto-smoke/backfill idiom: no-body publish, PASS/FAIL log line, failure raises into the DLQ). Trends are one query; the weekly digest is one page.
5. **Synthetic tenants are invisible to the business.** `Tenant.is_synthetic` excludes them from every revenue, donation, campaign, growth, and infra-cost aggregation. Cover ALL sites (this is a "cover every channel"-class invariant). That is ALL it means: it is a reporting flag, never a behavior flag (invariant 6).
6. **`is_eval_sink` is the only gate for eval-sink behavior.** Delivery suppression (the internal `eval` channel), memory/digest/proactive-context exclusion, and the eval target guards all read `Tenant.is_eval_sink` — nothing keys off `is_synthetic`. The two flags are independent: a synthetic tenant keeps normal assistant behavior unless the sink flag is explicitly set. Overloading `is_synthetic` for this once silently degraded the App Store Review demo account — a synthetic tenant that must behave EXACTLY like a real one for a human reviewer — by recording its assistant's replies as eval evidence instead of sending them.

## 2. The four suites

### Suite 1 — Journey canaries (is the product working?)
Two synthetic tenants provisioned exactly like real ones (real container, real DEK, real redaction).
- `eval-journey`: scheduled probes drive the *real* user path end-to-end — message→drain→OpenClaw→reply-delivered with round-trip SLO (30 min); force-hibernate→message→wake→reply under SLO (daily, the historically fragile path); cron-fire delivery (daily); journal write→search (daily).
- `eval-behavior`: reserved for Suite 2 (workspace reset between runs so behavior runs never pollute the uptime tenant's memory).
Failure → Mailgun alert to the platform owner + DLQ.

### Suite 2 — Model behavior (are the assistants acting right?)
YAML scenario fixtures: persona + multi-turn script + **hard assertions** (deterministic: must call `cron.add`; must NOT echo the planted fake-PII marker; must not reveal system internals) + **soft rubric dimensions** (helpfulness, warmth/SOUL adherence, boundary behavior). Runner drives them against the behavior tenant's real container; code checks hard assertions; a **pinned judge (Claude Opus 4.8 via OpenRouter, rubric v2, capped spend)** scores soft dimensions 1–5 with rationale. Runs nightly AND before any OpenClaw image bump / fleet prompt / config rollout, scored against a trailing baseline. Advisory gate first; hard rollout-blocker once trusted.

### Suite 3 — Deterministic corpora in CI (never regress a past incident)
Labeled synthetic corpora asserting exact behavior of the safety pipelines: PII redaction at egress (every past incident → a case) and upload-security ingress hardening. Fast subset on every PR beside the existing predicate/RedactedStr guards; full corpus nightly via QStash. Incidents become permanent tripwires.

### Suite 4 — Production SLOs (metadata only) + weekly readout
Nightly snapshot from Log Analytics + DB metadata: reply-latency p50/p95 (created→replied), DLQ depth, CryptoError count, decrypt-audit events by principal (an owner-read spike nobody triggered is itself a finding), wake times, cron success rate, unanswered-question rate, the 20s-timeout-burst pattern. Per-metric thresholds; breaches flagged. Monday cron → one-page Mailgun digest + an admin console page rendering trends.

## 3. Decisions locked (2026-07-11, MJ)
- Standing infra: two hibernatable synthetic-tenant containers + a hard monthly OpenRouter ceiling (~$10–20) for the behavior suite + judge.
- Alerts: Mailgun email to the platform owner (LINE is decommissioning; iOS push later). **Verify the Mailgun key is valid before relying on it — it was invalid during the comeback campaign.**
- Judge: Claude Opus 4.8 via OpenRouter (swapped from Claude Sonnet 5 on MJ's 2026-07-12 decision; `RUBRIC_VERSION` bumped behavior-v1 → behavior-v2 in lockstep), pinned, rubric versioned so judges can be swapped without losing comparability.

## 4. Build order
Wave A foundation (this) → B journeys ∥ C corpora → D behavior suite → E SLO + digest + admin page. Fable-5 orchestrates, reviews every diff with an independent Fable-5 adversarial pass, runs the integration train between waves, and live-verifies in prod after each deploy.

## 5. Review gate
Every eval PR clears TWO independent reviews before merge: the orchestrator's, and a fresh Fable-5 reviewer whose first question is always "does this eval actually test what it claims, or is it green theater?" — plus the §1 invariants as a checklist.
