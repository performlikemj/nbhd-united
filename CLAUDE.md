# NBHD United

Multi-tenant SaaS platform. Each subscriber gets a private AI assistant (OpenClaw) reached through the iOS app, Telegram, or LINE, running in its own Azure Container App. This repo is the control plane (Django) + subscriber console (Next.js frontend).

## How to use this file

This file is a router, not the manual. Deep context lives in `docs/agents/` — each doc encodes hard-won production lessons, and skipping them repeats old incidents. **Read the matching doc BEFORE starting work in its area:**

| When you are... | Read first |
|---|---|
| Touching tenants, containers, provisioning, messaging flow, OpenClaw config | `docs/agents/architecture.md` |
| Changing message routing, file-share writes, crons, revisions, timezones, transactions | `docs/agents/invariants.md` — permanent rules; each one broke production once |
| Debugging production, reading logs, "assistant silent", timeouts, wake/hibernation | `docs/agents/debugging.md` |
| Committing, pushing, merging PRs, deploying, writing migrations | `docs/agents/workflow.md` |
| Writing Django/backend code | `docs/agents/backend.md` |
| Writing frontend code or anything visual | `docs/agents/frontend.md` + `DESIGN.md` |
| Running an autonomous/scheduled loop (`/goal`, `/loop`, `/schedule`), or fanning out sub-agents | `docs/agents/loops.md` |

Skills available as slash commands on this machine: `/deploy` (commit→push→verify), `/production-logs`, `/rotate-keys` (secrets never appear in output), `/yardtalk-push`.

## Tech stack

- **Backend**: Django 6 + DRF, Python 3.12 (local venv MUST match — it once drifted a whole Django major and a local green meant nothing; `make setup` rebuilds it at parity, and a hook flags drift on test/migration runs — but it is silent when clean, and silence only means "matched origin/main as of your last fetch"), QStash for ALL scheduling (never Celery), PostgreSQL 16 via Supabase us-west-1
- **Frontend**: Next.js 14 static export (`out/`, no SSR), TypeScript, Tailwind, TipTap editor
- **Infra**: Azure Container Apps (`rg-nbhd-prod`), Key Vault `kv-nbhd-prod`, ACR `nbhdunited.azurecr.io`, Static Web Apps
- **Billing**: Stripe via dj-stripe · **Messaging**: Telegram Bot API, LINE Messaging API
- **AI runtime**: OpenClaw (separate image, `Dockerfile.openclaw`), LiteLLM/OpenRouter for models

```
Django control plane (nbhd-django-westus2)     Per-tenant: oc-<prefix> container
  ├── Console API (DRF)                          ├── OpenClaw runtime
  ├── Channel routers → oc-* containers          ├── File share ws-<prefix>
  ├── Stripe webhooks → provisioning             └── Identity mi-nbhd-<prefix>
  └── QStash crons                             Frontend: Azure Static Web App
```

Django apps: `actions agents automations billing byo_models common core cron dashboard finance friends fuel insights integrations journal lessons orchestrator pii platform_logs router telegram_bot tenants`.

## Key commands

```bash
make run / make test / make lint / make migrate       # dev server · full tests · ruff check · migrate
make tenants / make health                            # list tenants · health check
make provision TENANT_ID=<uuid>                       # (and deprovision)
cd frontend && npm run dev / npm run build            # dev server · static export
python manage.py test apps.<app>.<module> --noinput   # targeted tests while iterating
```

## Iron rules (always active — rationale in docs/agents/)

- `main` is protected: PR branches only (`feat/` `fix/` `refactor/` `docs/`). Stage specific files (`git add -A`/`.` is hook-blocked). No `--no-verify`. No force-push.
- Cross-branch work → `git worktree add .claude/worktrees/<name>`, never checkout-switch a dirty tree.
- Before pushing backend code: `.venv/bin/ruff format <files>` and `manage.py makemigrations --check --dry-run` — CI gates that `make lint` does NOT cover.
- Merging: `gh pr view <n> --json baseRefName` must be `main`; afterwards verify main actually advanced. `gh pr merge --auto` merges instantly (no required checks) — watch CI yourself if green-before-merge matters.
- QStash, not Celery. Never SQLite on the per-tenant file share. Every inbound handler calls `claim_inbound_event` first. Message-routing changes cover ALL channel paths (Telegram poller + webhook + LINE).
- Test in production (no staging): after every deploy, verify the user-facing symptom via logs/probes — never conclude success from an exit code or a MERGED badge.
- Never print secrets or dump env vars into output. Never delete Azure resources without explicit confirmation.
- Env var names in `config/settings/production.py` must match the Azure Container App env vars (a hook reminds you on edit).

## Commit convention

Prefixes: `feat:` `fix:` `fix(scope):` `refactor:` `docs:` `merge:` — concise, focused on the why.
Plan complex features in `CONTINUITY_<feature>.md` first; implement phase by phase; verify between phases.
