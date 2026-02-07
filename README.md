# Neighborhood United — Managed AI Agent Platform

**Your AI, Your Context, Your Privacy.**

A multi-tenant AI agent platform that gives every user their own private AI assistant accessible through Telegram. Built for communities — especially those in low-income and non-tech neighborhoods.

## Architecture

- **Django 5.1** — REST API with DRF
- **Celery + Redis** — Async task processing
- **PostgreSQL 16** — Primary database with tenant isolation
- **LiteLLM + OpenRouter** — Multi-model AI routing (free → paid tiers)
- **dj-stripe** — Stripe billing integration
- **python-telegram-bot** — Telegram as primary interface

See [architecture docs](../docs/community-agent-platform/) for full details.

## Quick Start

```bash
# 1. Clone and enter
cd nbhd-united

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install pip-tools
pip-compile requirements.in
pip-sync requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your settings

# 5. Start services
docker compose up -d  # PostgreSQL + Redis

# 6. Run migrations
python manage.py migrate

# 7. Create superuser
python manage.py createsuperuser

# 8. Run dev server
python manage.py runserver
```

Or use the Makefile:
```bash
make setup       # venv + deps
make docker-up   # postgres + redis
make migrate     # run migrations
make run         # dev server
```

## Project Structure

```
config/          → Django settings, URLs, WSGI/ASGI, Celery
apps/
  tenants/       → Tenant & user management, auth
  agents/        → Agent sessions, messages, memory
  billing/       → Stripe plans & usage tracking
  telegram_bot/  → Telegram webhook & handlers
  integrations/  → OAuth per-user integrations (future)
```

## Key Concepts

- **1 User = 1 Tenant = 1 Agent** — Simple mental model
- **Telegram-first** — No web UI needed, button-driven UX
- **Tiered models** — Free users get free models, paid get premium
- **pip-tools workflow** — `requirements.in` is source of truth

## Environment Variables

See `.env.example` for all required configuration.

## License

Proprietary — Neighborhood United
