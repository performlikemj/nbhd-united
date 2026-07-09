# NBHD United — Reference Documentation

A companion **reference set** for understanding the codebase, running security audits, and planning improvements. It complements — does not replace — two existing bodies of docs:

- [`docs/agents/`](../agents/) — terse, agent-facing *playbooks* (architecture, invariants, workflow, debugging). Read those for "how do I safely change X." These reference docs are the fuller "how does X actually work."
- [`docs/`](../) — historical design directives, specs, and postmortems (per-feature).

**Audit baseline:** generated against `main` as of the commit this branch was rebased onto (see `git log`). Reference-doc `path:line` citations were captured at the sweep's start; the **security docs under [`docs/security/`](../security/)** reflect the same current `main`. Treat line numbers as close pointers, not exact anchors, and confirm against live code before acting.

## Start here

**New engineer** → [System overview in `agents/architecture.md`](../agents/architecture.md) → [`GLOSSARY.md`](../GLOSSARY.md) → the reference doc for your area → [`agents/invariants.md`](../agents/invariants.md) before your first change.

**Security auditor** → [`data-model.md`](data-model.md) (what data exists, tenant-scoping) + [`api-surface.md`](api-surface.md) (attack surface) → then the [`docs/security/`](../security/) analyses → cross-reference the per-subsystem docs below for mechanism detail.

## Reference docs

| Doc | Covers |
|---|---|
| [messaging-and-channels.md](messaging-and-channels.md) | Inbound/outbound message flow across Telegram, LINE, iOS, web; dedup gate; transcription; senders; progress stream; push. |
| [tenant-runtime-and-provisioning.md](tenant-runtime-and-provisioning.md) | Tenant lifecycle; Azure container/share/identity provisioning; OpenClaw config generation + bump rollout; hibernation/wake; tool policy; action-gating. |
| [identity-billing-integrations.md](identity-billing-integrations.md) | User/Tenant identity; JWT→RLS auth; account flows; Stripe subscription lifecycle → provisioning; BYO LLM credentials; third-party integrations. |
| [content-pillars.md](content-pillars.md) | Journal, Lessons, Insights, Core, Automations — what they store and how the console vs the assistant runtime reach them. |
| [domain-modules.md](domain-modules.md) | Fuel (fitness), Finance (Gravity), Friends (Neighborhood) — incl. the cross-tenant sharing model. |
| [platform-services.md](platform-services.md) | QStash cron; the PII redaction engine (mechanics); platform logs; shared utils (tenant_tz); dashboard. |
| [frontend.md](frontend.md) | Next.js static-export console: routes, client-side auth/session, data fetching, caching, build constraints. |
| [infrastructure-and-deployment.md](infrastructure-and-deployment.md) | Dockerfiles, CI/CD pipeline, settings/env contract, Azure topology, Key Vault + managed identity. |
| [data-model.md](data-model.md) | **Catalog** — every model across all apps, tenant-scoping, RLS state, PII-bearing tables. |
| [api-surface.md](api-surface.md) | **Catalog** — every HTTP endpoint, its auth/permission, tenant-scope, and trust boundary. |

## Security analyses — [`docs/security/`](../security/)

| Doc | Covers |
|---|---|
| [multi-tenant-isolation.md](../security/multi-tenant-isolation.md) | How tenant isolation actually works today (RLS posture, app-layer filters, friends chokepoint) + leak vectors. |
| [authn-authz-and-api-surface.md](../security/authn-authz-and-api-surface.md) | Auth/trust boundaries: JWT/PAT, the internal-key bearer model, webhook signatures, unauth endpoints. |
| [pii-and-llm-egress.md](../security/pii-and-llm-egress.md) | PII redaction as a control; what reaches the LLM; at-rest posture after encryption-at-rest Phase 0. |
| [secrets-identity-supply-chain.md](../security/secrets-identity-supply-chain.md) | Key Vault, managed identity, bearer-secret rotation, dependency/supply-chain risk. |
| [input-handling-and-injection.md](../security/input-handling-and-injection.md) | Prompt-injection surface, webhook/input validation, SSRF, SQL, share-write safety. |

See also [`../security/README.md`](../security/README.md) (threat-model overview) and [`../IMPROVEMENTS.md`](../IMPROVEMENTS.md) (prioritized roadmap).
