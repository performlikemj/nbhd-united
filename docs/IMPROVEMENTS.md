# NBHD United — Improvement Roadmap

Prioritized, actionable improvements from the 2026-07-09 documentation + security sweep. Grouped by priority; each item carries rough **impact** and **effort** so you can pick by ROI. Security items reference the register in [`security/README.md`](security/README.md).

The platform is in good shape overall — the messaging pipeline, provisioning lifecycle, billing, and the PII pipeline are thoughtfully built, and the recent encryption-at-rest Phase 0 work closed real gaps. The items below are where the leverage is now.

## P0 — Security must-fix (weeks, not months)

| # | Action | Why | Impact | Effort |
|---|---|---|---|---|
| 1 | **Run `manage.py check_friends_rls` in prod; reconcile the RLS docs to the verdict.** | Settles whether the Friends DB backstop actually binds (SEC-5/SEC-9). One command; removes the single biggest ambiguity in the whole isolation story. | High | XS |
| 2 | **Stop persisting the Google refresh token + client_secret on the share** (SEC-1). Fetch short-lived access tokens on demand via a runtime callback (mirror the BYO CLI wrapper pattern). | Durable Google-account takeover is reachable by prompt injection today. | High | M |
| 3 | **Default-deny unscoped PAT on sensitive views** (or drop the scopes UI) (SEC-3, SEC-16). | A "sessions:write" token is a full-account bearer credential; the UI implies otherwise. | High | S |
| 4 | **Redact the onboarding `USER.md` write** (SEC-4) — route through `RedactionSession` / `push_user_md(force=True)`. | Every tenant's real name/city/interests land unredacted on the LLM-readable share, permanently. | High | S |
| 5 | **Rotate the Telegram bot token; land the redaction filter or finish decommissioning** (SEC-2). | Token is in Log Analytics history in plaintext. | Med (channel sunsetting) | S |

## P1 — Structural hardening (make the default safe)

The theme: several risks exist because a control is *by convention* rather than *enforced by construction*. Convert them.

- **CI guard: every `AllowAny` runtime view must call the internal-key validator** (SEC-10). An AST/regex test mirroring how invariant #1 is pinned. Turns "someone forgets → ships open" into a build failure. *(High / S)*
- **Decide the RLS posture and commit to it** (SEC-5). Either (a) run the runtime as the confirmed non-BYPASSRLS `app_user` and re-enable RLS broadly for a real net, or (b) keep the app-layer posture but add a **tenant-filter lint/queryset guard** and stop the docs claiming a DB backstop. Today it's the worst of both: no net *and* docs that imply one. *(High / M–L)*
- **Add non-blocking dependency scanning** (SEC-12): `pip-audit` + `npm audit --audit-level=high` as reporting CI steps; un-mute `transformers` alerts or document the exception. *(Med / S)*
- **Constant-time compare `DEPLOY_SECRET`** everywhere and **scope-narrow it** — it gates fleet-wide destructive ops from ~20 call sites as one static, never-rotated secret. Consider per-operation-class secrets or short-lived signed requests. *(Med / S–M)*
- **Secret lifecycle**: delete per-tenant KV secrets on deprovision + add an orphan-secret sweeper (SEC-11); add rotation automation for the shared gateway token (SEC-26). *(Med / M)*
- **Make action-gating a real capability check** before wiring any destructive skill (SEC-7) — bind approval to the specific tool call in code, not model-trust. *(High / M — do before GWS write skills ship)*
- **Add explicit SSRF/egress controls** (SEC-6): verify the OpenClaw default, then pin `ssrf`/`allowPrivateNetwork` in generated config and/or an NSG on the container subnet. *(Med / M)*
- **Alert on fail-open redaction** (SEC-14): the pipeline forwards raw text on any exception — page on `_pipeline_load_error` and on redaction-exception rate. *(Med / S)*

## P2 — Modernization & platform health

The codebase started years ago; several foundations have already been modernized (Django 6.0.7, OpenClaw 5.28 + toolSearch, Supabase→Azure-region DB, QStash over Celery). Remaining:

- **Build the encryption-at-rest crypto substrate** (`apps/crypto`, directive Phase 1) so the `pii_entity_map` reversal key and finance/health columns can be encrypted (SEC-8). This is the highest-value remaining privacy investment; the directive already sequences it well. *(High / L)*
- **Non-root Django container** (SEC-21): add a `USER` directive — the OpenClaw image already shows the pattern. Cheap defense-in-depth for the process that holds `ADMIN_DATABASE_URL`. *(Low / XS)*
- **Prune dead dependencies**: remove `python-telegram-bot` (unused; all traffic is raw httpx) (SEC-22), and revisit the ~1.1 GB PII/torch stack's footprint as Telegram sunsets. *(Low / S)*
- **Automated cross-tenant IDOR test** over the ~90-endpoint console API (SEC-25) — a parametrized "tenant A cannot read tenant B's object" suite. Given isolation is app-layer, this is the regression net that matters most. *(High / M)*
- **Retire Telegram-path code** as the iOS-first decommission proceeds — reduces the messaging surface (dedup, poller, single-revision constraints) and removes the SEC-2 token entirely. *(Med / M)*

## P3 — Consistency, correctness, cleanup

- **Doc corrections** (some already applied on this branch): `CLAUDE.md` still says "Django 5.1" (now 6.0.7); migration `0059` docstring falsely claims `disable_rls` was removed; the encryption-at-rest directive and `pii-redaction-security.md` describe superseded (pre-#1083/#1074) behavior. Reconcile.
- **Unify the internal-auth header convention** (SEC-23) — one header set + status code; fix the gate handler that fail-opens the tenant-id cross-check.
- **Add the `report_content` visibility check** (SEC-13); extend the CI chokepoint AST guard to cover `friends_callbacks.py`-style hand-rolled cross-tenant queries (SEC-29).
- **Deterministic retention janitor** for the PII-heaviest long-lived tables (probabilistic pruning can over-retain on low-traffic tenants).

## Strategic bets (things worth deciding deliberately)

These aren't bugs — they're forks in the road that compound if left implicit.

1. **Pick an isolation philosophy and make the code enforce it.** The platform is one forgotten `.filter(tenant=...)` away from a cross-tenant leak, mitigated only by discipline. Either lean fully into database-enforced RLS, or lean fully into a typed, un-bypassable tenant-scoped manager (e.g. a base manager that refuses unscoped queries) + the IDOR test suite. Half-measures cost you the incident.
2. **Treat the `oc-*` container as hostile-capable.** It is LLM-driven, reads its own file share, and holds real credentials. The strongest single hardening is *nothing sensitive in plaintext on the share* — that one principle subsumes SEC-1, SEC-4, and much of the injection surface. Make "the container can read this" the test for every share write.
3. **Publish an honest data-flow / privacy posture doc.** Redaction is genuinely strong, but there are deliberate exceptions (Siri fast-path, meditation compose, ZDR-vs-technical guarantees). A precise "what leaves the boundary and under what guarantee" statement protects you legally and forces the exceptions to stay intentional. The reference + security docs here are the raw material.
4. **Make secrets rotatable without a fleet restart.** Today the highest-blast-radius secrets are shared and static. Design for rotation now, while the fleet is small.
5. **Invest the saved surface from Telegram's sunset** into deepening the iOS/web path rather than maintaining channel parity you no longer need.

---
*This roadmap is a snapshot; the register in [`security/README.md`](security/README.md) is the source of truth for finding status.*
