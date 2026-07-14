# NBHD United — Managed OpenClaw Platform

**Control plane for managed OpenClaw instances.** Each $5/month subscriber gets their own private AI assistant via Telegram, powered by OpenClaw running in isolated Azure containers.

## Architecture

This is **NOT** an AI runtime — [OpenClaw](https://github.com/nichochar/openclaw) is the runtime. This repo is the orchestration layer:

```
┌─────────────────┐
│  Telegram Users  │
└────────┬────────┘
         │
┌────────▼────────┐     ┌──────────────┐     ┌──────────────┐
│  Message Router  │────▶│  OpenClaw A   │     │  OpenClaw N   │
│  (this service)  │     │  (container)  │ ... │  (container)  │
└────────┬────────┘     └──────┬───────┘     └──────┬───────┘
         │                     │                     │
┌────────▼────────┐     ┌──────▼─────────────────────▼──────┐
│  Stripe Billing  │     │         Azure Key Vault           │
│  (dj-stripe)    │     │  (tenant-scoped OAuth tokens)     │
└─────────────────┘     └───────────────────────────────────┘
```

### Components

| Component | What it does |
|-----------|-------------|
| **Tenants** | User accounts, subscription status, container mapping |
| **Billing** | Stripe subscription ($5/mo), webhook → provisioning triggers |
| **Orchestrator** | Azure Container Apps SDK — create/delete OpenClaw instances |
| **Router** | Single Telegram bot, routes messages to correct OpenClaw container |
| **Integrations** | OAuth flows → tokens stored in Azure Key Vault |
| **Dashboard** | DRF API for frontend (tenant status, usage, connections) |

### Key Design Decisions

- **One container per user** — true isolation, no shared state
- **Scale-to-zero** — Azure Container Apps idles inactive containers
- **Single Telegram bot** — router maps `chat_id → container` and forwards
- **Key Vault for secrets** — Azure RBAC enforces tenant isolation at platform level
- **OpenClaw config template** — generated per tenant with locked `allowFrom`

## Tech Stack

- **Django 5.1** + DRF — REST API
- **Celery + Redis** — async provisioning tasks
- **PostgreSQL 16** — tenant registry, usage tracking
- **dj-stripe** — Stripe billing integration
- **Azure Container Apps** — OpenClaw instance hosting
- **Azure Key Vault** — tenant-scoped secret storage

## Quick Start

```bash
# Clone and enter
cd nbhd-united

# Create the virtualenv at CI parity (python 3.12, installs from origin/main's pins)
make setup
source .venv/bin/activate

# NEVER run `pip-compile` on macOS. requirements.txt is a Linux-generated lockfile;
# re-compiling it here silently drops ~37 CUDA/triton pins that the PII container
# needs, and nothing fails until deploy. A git hook blocks it. To add a dependency,
# hand-edit requirements.txt; to regenerate it properly, run pip-compile in the
# Linux container.

# Configure
cp .env.example .env
# Edit .env — set AZURE_MOCK=true for local dev

# Start services
docker compose up -d  # PostgreSQL + Redis

# Run migrations
python manage.py migrate

# Create superuser
python manage.py createsuperuser

# Run dev server
python manage.py runserver
```

Or use the Makefile:
```bash
make setup       # venv + deps
make docker-up   # postgres + redis
make migrate     # run migrations
make run         # dev server
make test        # run tests
```

## Management Commands

```bash
# List all tenants
python manage.py list_tenants
python manage.py list_tenants --status active

# Check container health
python manage.py check_health

# Manual provisioning
python manage.py provision_tenant <tenant-uuid>
python manage.py deprovision_tenant <tenant-uuid>
```

## Project Structure

```
config/              Django settings (base/development/production)
apps/
  tenants/           User model, tenant model, registration
  billing/           Stripe webhooks, usage tracking, budget caps
  orchestrator/      Azure Container Apps lifecycle, config generation
  router/            Telegram message routing to OpenClaw instances
  integrations/      OAuth flows, Key Vault token storage
  dashboard/         DRF API for frontend
templates/
  openclaw/          OpenClaw workspace templates (AGENTS.md, etc.)
infra/               Terraform modules (placeholder)
frontend/            Next.js subscriber console (separate build)
```

## Environment Variables

See `.env.example` for all configuration. Key ones:

| Variable | Purpose |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Shared Telegram bot token |
| `TELEGRAM_WEBHOOK_SECRET` | Required non-empty webhook secret for Telegram webhook validation |
| `STRIPE_TEST_SECRET_KEY` | Stripe test key used when `STRIPE_LIVE_MODE=False` |
| `STRIPE_LIVE_SECRET_KEY` | Stripe live key used when `STRIPE_LIVE_MODE=True` |
| `STRIPE_PRICE_STARTER` | Stripe price ID for the single "starter" subscription tier (read by `settings.STRIPE_PRICE_ID`) — must be from the same account as the keys/webhook |
| `DJSTRIPE_WEBHOOK_SECRET` | Signing secret of the live-account webhook endpoint → `/api/v1/billing/webhook/` (see `docs/infrastructure/STRIPE_WEBHOOK_SETUP.md`) |
| `ANTHROPIC_API_KEY` | Shared API key for all OpenClaw instances |
| `OPENAI_API_KEY` | Shared OpenAI API key for Whisper/voice transcription defaults |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription for Container Apps |
| `AZURE_KEY_VAULT_NAME` | Key Vault for tenant secrets |
| `FRONTEND_URL` | Subscriber console URL used for redirects and onboarding links |
| `AZURE_MOCK` | Set `true` for local dev without Azure |

## License

Proprietary — NBHD United
