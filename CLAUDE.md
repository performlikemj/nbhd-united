# NBHD United

Multi-tenant SaaS platform. Each subscriber gets a private AI assistant (OpenClaw) via Telegram or LINE, running in its own Azure Container App. This repo is the control plane (Django) + subscriber console (Next.js frontend).

## Tech Stack

- **Backend**: Django 5.1 + DRF, QStash (scheduling, not Celery), PostgreSQL 16 via Supabase, Python 3.12
- **Frontend**: Next.js 14 (static export to `out/`), TypeScript, Tailwind CSS, TipTap editor
- **Infrastructure**: Azure Container Apps, Key Vault, Container Registry (`nbhdunited`), Static Web Apps
- **Billing**: Stripe via dj-stripe
- **Messaging**: Telegram Bot API (`python-telegram-bot`), LINE Messaging API
- **AI Runtime**: OpenClaw (separate image, `Dockerfile.openclaw`), LiteLLM for model routing

## Architecture

```
Django control plane (nbhd-django-westus2)
  ├── Subscriber console API (DRF)
  ├── Telegram webhook router → oc-* containers
  ├── Billing (Stripe webhooks → provisioning)
  └── Cron (QStash → bump configs, usage reports)

Per-tenant containers (oc-<tenant_prefix>)
  ├── OpenClaw AI assistant runtime
  ├── Azure File Share mount (ws-<tenant_prefix>)
  └── Managed Identity (mi-nbhd-<tenant_prefix>)

Frontend (nbhd-united-frontend, Azure Static Web App)
  └── Next.js static export → subscriber dashboard
```

## Azure Naming Conventions

| Resource | Prefix | Example |
|---|---|---|
| Container App | `oc-` | `oc-148ccf1c-ef13-47f8-a` |
| Managed Identity | `mi-nbhd-` | `mi-nbhd-148ccf1c-ef13-47f8-a` |
| File Share | `ws-` | `ws-148ccf1c-ef13-47f8-a` |

Resource group: `rg-nbhd-prod`. Registry: `nbhdunited.azurecr.io`.
Full docs: `docs/infrastructure/azure-resource-naming.md`

## Django Apps

`actions`, `agents`, `automations`, `billing`, `cron`, `dashboard`, `integrations`, `journal`, `lessons`, `orchestrator`, `platform_logs`, `router`, `telegram_bot`, `tenants`

## Key Commands

```bash
make run              # Django dev server (0.0.0.0:8000)
make test             # python manage.py test apps/
make lint             # ruff check .
make migrate          # python manage.py migrate
make tenants          # python manage.py list_tenants
make health           # python manage.py check_health
make provision TENANT_ID=<uuid>
make deprovision TENANT_ID=<uuid>
cd frontend && npm run build   # Static export to out/
cd frontend && npm run dev     # Next.js dev server
```

## CI/CD Pipeline

Push to `main` triggers `.github/workflows/ci-cd.yml`:
1. Frontend lint + build
2. Backend Django checks + tests (pgvector/pg16)
3. OpenClaw config doctor smoke test
4. Build + push Django image → `nbhdunited.azurecr.io/django:<sha>`
5. Build + push OpenClaw image → `nbhdunited.azurecr.io/nbhd-openclaw:<sha>`
6. Deploy Django to Container Apps (single-revision mode)
7. Health check → bump pending configs → register QStash crons
8. Deploy frontend to Azure Static Web App

## Commit Convention

Use prefixes: `feat:`, `fix:`, `merge:`, `refactor:`, `docs:`, `fix(scope):`
Keep messages concise, focused on the "why". Examples:
- `feat: tier-based GWS access + gate tool plugin + Celery expiry`
- `fix: billing plan selector overflow on mobile — use stacked cards`
- `merge: action-gating + envelope timezone fix`
- `fix(tests): update suspended-tenant test assertions`

## Development Workflow

- Plan first for complex features — create `CONTINUITY_<feature-name>.md`
- Implement phase by phase, test between phases
- Test in production (no staging environment)
- After deploy: always verify via `az containerapp logs show`
- For multi-tenant changes: bump configs, verify at least one tenant picks up

## Frontend Conventions

- Follow `frontend/BRAND_GUIDE.md` for all design tokens
- Mobile-first responsive design
- WCAG 2.1 AA accessibility (4.5:1 contrast, 44x44px touch targets)
- Respect `prefers-reduced-motion`
- Use CSS variables from design system, not hardcoded values

## Gotchas

- **Key Vault identity prefix**: Use `mi-nbhd-` identity name for `identityref:`, NOT `oc-` container name
- **Telegram single-revision**: Required to prevent 409 conflicts from multiple pollers
- **IPv6 unreliable**: OpenClaw uses `--dns-result-order=ipv4first`
- **Cold starts mask errors**: Always check logs after timeout errors — the real error may be hidden
- **Frontend is static export**: No SSR. `npm run build` creates `out/` directory
- **QStash, not Celery**: Do NOT add `django_celery_beat` — project uses QStash for all scheduling

## Don'ts

- Do NOT use `git add -A` or `git add .` — stage specific files to avoid committing `.env`
- Do NOT skip pre-commit hooks (`--no-verify`)
- Do NOT force push to main
- Do NOT delete Azure resources without explicit confirmation
- Do NOT modify env var names in `config/settings/production.py` without updating Azure Container App env vars
