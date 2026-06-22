# Adversarial Review of the Audit Findings

A second, deliberately hostile pass over the audit's own conclusions — biased to
**disprove**, not confirm. Data: [`adversarial_findings.json`](./adversarial_findings.json)
(the 50 genuine issues) + [`false_positives.json`](./false_positives.json) (the 69 dismissals it re-examined).

## Method — three angles + a refute-by-default arbiter

1. **Attack the fixes** — try to *break* each of the 126 fixes (wrong, incomplete, regression, new bug).
2. **Resurrect the dismissals** — try to *prove* each of the 69 "false positives" was actually a real bug we waved away.
3. **Hunt false negatives** — re-scan 12 high-risk areas (auth, billing, tenant isolation, PII, money, actions, …) for defects the first pass missed.

Every surfaced candidate then went through an independent arbiter instructed to
**default to "not a problem"** — so the adversary couldn't over-claim either.

**146 agents. 69 candidates → 50 verified genuine** (the arbiter killed 19 over-claims).
By severity: **1 critical, 6 high, 10 medium, 33 low.** By source: **25 bugs the first pass MISSED**, **25 problems with the fixes** (incomplete / regressed).

## The headline catch — a CRITICAL bug the first pass missed

**`SignupView` minted tokens with `RefreshToken.for_user()`**, which omits the custom
`pw_iat` claim. Because `create_user` stamps `password_last_changed_at`, the force-logout
auth check rejected the token — so **every new email/password signup's token was dead on
arrival** (the first authenticated request 401'd with "Session expired due to a security
update"). The codebase had already fixed this exact class for password-reset and OAuth;
signup was the one path missed, and no test replayed a signup token against a protected
endpoint. Fixed by minting via `EmailTokenObtainPairSerializer.get_token`.

## Other high-severity finds

- **Raw PII in logs** — the *redactor itself* logged detected card numbers / passwords /
  IBANs in cleartext to Azure logs (PCI-DSS). Now logs only `span_len`.
- **BYO error loop dead in prod** — `mark_credential_error` excluded `PENDING`, the
  de-facto working state (no verifier exists), so the "reconnect" banner never showed;
  and the error path never reconciled the container, leaving the assistant fully dark.
- **Finance query tool 500'd on every call** — the plugin read `args[0]` (the toolCallId)
  instead of `args[1]` (the params); the assistant's mandated grounding query was dead.
- **Markdown checkbox — third fix** — the Phase-4 ordinal retry STILL mis-mapped (regex ≠
  remark-gfm grammar). Now reads the `<li>` node's source position, **empirically verified**
  against the installed parser on 6 tricky inputs (empty/blockquote/indented/nested tasks).

## Disposition of the 50 genuine findings

| disposition | count | notes |
|---|---|---|
| Hand-fixed (critical + 6 high) | 8 refs | signup, PII-logs, BYO loop, finance plugin, markdown, billing reset, provisioning idempotency |
| Workflow-fixed (medium + low) | 40 | incl. BYO container reconcile, week-aware plan PATCH, templates.md durability, is_capped enforcement, PII-map locking, iOS reply de-dup |
| No-op (already covered) | 1 | pii_entity_map race — closed by the pii#2 lock |
| **Reverted / deferred** | **2** | two LOW-severity behaviour changes that broke established tests and are genuinely ambiguous — kept the stable behaviour, flagged for a product decision: lessons edge-pruning (FA-0792), chat-progress oldest-vs-newest (router-chat#2) |

## Validation

Full backend suite — **4,036 tests** (python3.11 + local pg). Failure set is **identical
to the known environment-only baseline** (unavailable `azure.*`/`transformers` SDKs, `stripe`
12.5.1 vs pinned 15.2.1, an OpenAI-mock test) — **zero real regressions**. Frontend `tsc`
+ `eslint` + `next build` clean (40 routes). The fix-agents wrote several broken *tests*
(wrong API usage) that this validation caught and repaired; the *fixes* were sound.

## Honest caveat

The adversarial fix-agents themselves introduced 2 regressions and ~16 broken tests — all
caught by the full-suite validation and fixed/reverted here. The lesson reinforced from
Phase 4: **run the real suite after every fix wave; agent-written tests are not trustworthy
until executed.**
