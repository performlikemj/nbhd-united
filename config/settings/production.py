"""Production settings (Azure)."""

from .base import *  # noqa: F401,F403
from .base import DATABASES, env

DEBUG = False

# Database — transaction-mode pooling compatibility.
#
# Production runs Django against Supabase via Supavisor. Setting the
# DATABASE_URL env var to the transaction-mode pooler endpoint (port 6543)
# is what actually swaps the connection mode; this setting is the
# Django-side companion that disables server-side cursors so QuerySet
# .iterator() doesn't fall over.
#
# Background: in transaction-mode pooling Postgres backend connections
# are released per transaction, not per client socket. Django's default
# .iterator() opens a named server-side cursor and consumes it across
# multiple transactions, which doesn't survive the connection swap.
# DISABLE_SERVER_SIDE_CURSORS=True makes .iterator() materialize the
# queryset client-side instead. Safe here because the only production
# .iterator() caller (apps/insights/tasks.py — finance-eligible tenants)
# is a tiny set; the others are migrations and ops commands.
#
# Why this matters: 2026-05-15 we observed Supavisor pool exhaustion
# (`EMAXCONNSESSION max clients reached in session mode - pool_size: 15`).
# All 15 backend conns were idle-but-pinned by Django sockets that
# session-mode pooling refused to release. Transaction mode + this
# setting is the canonical Django-on-Supabase pattern.
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True

# Persistent client connections to the Supavisor pooler.
#
# With Django's default CONN_MAX_AGE=0, every request opened a fresh
# psycopg connection to the cross-region pooler: TCP + TLS + SCRAM is
# 5-6 round trips ≈ 600-900ms — measured 2026-06-10 as the bulk of a
# fixed ~1.4s server-side TTFB floor on even 401/404 responses.
#
# Safe with transaction-mode pooling: a persistent CLIENT socket does
# not pin a BACKEND connection (backends are leased per transaction).
# This is the opposite of the 2026-05-15 EMAXCONNSESSION incident,
# which was session-mode pinning. Upper bound on client sockets is
# gunicorn workers × threads + poller, well under Supavisor's client
# limit. Health checks recycle sockets the pooler silently dropped.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=600)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True

# Disable psycopg3 client-side prepared statements under transaction-mode
# pooling. In transaction mode (port 6543) Supavisor leases a different
# backend per transaction, so a statement PREPAREd on backend A is not
# guaranteed to exist when the next execution lands on backend B —
# psycopg3's default (prepare a query after 5 executions) would then raise
# `prepared statement "_pg3_N" does not exist`. Setting prepare_threshold
# to None turns off auto-preparation entirely; the per-statement cost is
# negligible and it makes the 5432→6543 cutover safe. Harmless on the
# direct/session connection too (it just never prepares), and on local/CI
# (psycopg3 there as well). The EMAXCONNSESSION incidents (2026-05-15,
# 2026-06-12 silent Telegram drops) traced to the secret being pointed at
# the SESSION pooler (5432) instead of 6543 — once corrected, this guard
# keeps transaction mode from surfacing a prepared-statement regression.
DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"]["prepare_threshold"] = None

# TCP keepalives on the client→Supavisor socket.
#
# The DB is in Supabase ap-southeast-2 (Sydney) while Django runs in Azure
# westus2 — a trans-Pacific hop measured at ~152ms per round trip, so a fresh
# psycopg connect (TCP + TLS + SCRAM ≈ 5-6 round trips) costs ~900ms. CONN_MAX_AGE
# is meant to amortize that across a connection's 600s life, but it only pays off
# if the socket survives between requests. With gthread (2×8 = up to 16
# thread-local connections) and bursty/low single-client traffic (e.g. the iOS
# chat poll every ~30s), any given thread's socket sits idle for minutes — long
# enough for an intermediate NAT/load-balancer (Azure outbound SNAT idle ~4min)
# to silently reap it. CONN_HEALTH_CHECKS then finds a dead socket and reconnects,
# charging the ~900ms handshake to that request's FIRST query (the JWT auth user
# lookup) — which is exactly the >1s "Slow DB Query" Sentry flagged on
# /api/v1/chat/messages/ (2026-06-24). Postgres' own keepalive is server→client
# (tcp_keepalives_idle=1800) and doesn't keep the client socket warm.
#
# Client-side keepalives send a probe after 30s of idleness, resetting the NAT
# idle timer so the persistent connection actually persists and CONN_MAX_AGE
# delivers reuse. libpq params, passed straight through by psycopg3.
DATABASES["default"]["OPTIONS"]["keepalives"] = 1
DATABASES["default"]["OPTIONS"]["keepalives_idle"] = 30
DATABASES["default"]["OPTIONS"]["keepalives_interval"] = 10
DATABASES["default"]["OPTIONS"]["keepalives_count"] = 5

# Security
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Email — Mailgun via SMTP. Credentials come from the Mailgun dashboard
# under Sending → Domain settings → SMTP credentials (login is usually
# postmaster@<MAILGUN_SENDER_DOMAIN>). If EMAIL_HOST_USER is unset we
# fall back to the console backend so misconfiguration is loud rather
# than silently dropping mail.
if env("EMAIL_HOST_USER", default=""):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = env("EMAIL_HOST", default="smtp.mailgun.org")
    EMAIL_PORT = env.int("EMAIL_PORT", default=587)
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
    EMAIL_HOST_USER = env("EMAIL_HOST_USER")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD")
    EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL",
    default="NBHD United <noreply@neighborhoodunited.org>",
)

# Steward Phase 1. These names intentionally mirror the Azure Container App
# env vars verbatim (invariant §10). Empty ingest secret is fail-closed at the
# endpoint; empty delivery/dead-man values safely disable that optional channel.
STEWARD_INGEST_SECRET = env("STEWARD_INGEST_SECRET", default="")
STEWARD_TELEGRAM_BOT_TOKEN = env("STEWARD_TELEGRAM_BOT_TOKEN", default="")
STEWARD_TELEGRAM_CHAT_ID = env("STEWARD_TELEGRAM_CHAT_ID", default="")
STEWARD_ALERT_EMAIL = env("STEWARD_ALERT_EMAIL", default="")
STEWARD_DEADMAN_URL = env("STEWARD_DEADMAN_URL", default="")

# YardTalk one-time purchase + validation. The Price id is a Container App
# config value; the HMAC key is a Key Vault-backed secretref.
YARDTALK_STRIPE_PRICE_ID = env("YARDTALK_STRIPE_PRICE_ID", default="")
YARDTALK_LICENSE_RECEIPT_SECRET = env("YARDTALK_LICENSE_RECEIPT_SECRET", default="")

# Eval journey canaries (Wave B) — mirror of the base.py names so the deploy's
# Azure Container App env vars are documented here. EVAL_JOURNEY_TENANT_ID +
# EVAL_JOURNEY_PAT must be set for the journey probes; EVAL_BEHAVIOR_TENANT_ID is
# plumbed now but only used from Wave D. The probes target the synthetic tenant
# by id — never a hardcoded UUID.
EVAL_JOURNEY_TENANT_ID = env("EVAL_JOURNEY_TENANT_ID", default="")
EVAL_BEHAVIOR_TENANT_ID = env("EVAL_BEHAVIOR_TENANT_ID", default="")
EVAL_JOURNEY_PAT = env("EVAL_JOURNEY_PAT", default="")
# Wave D behavior suite PAT (secretref eval-behavior-pat on the Container App).
# Mirrors the eval-journey-pat wiring; provisioned as an ops step. See base.py.
EVAL_BEHAVIOR_PAT = env("EVAL_BEHAVIOR_PAT", default="")

# sautai Phase 0 M2M bridge (docs/sautai-phase0-contract.md). Both names MUST
# match the Azure Container App env vars exactly (invariant §10). No default
# host/secret ON PURPOSE: an unset value makes the runtime proxy + QStash worker
# fail loud ("sautai integration is not configured") rather than silently POST a
# user's email to the wrong host. SAUTAI_PLATFORM_SECRET is platform-level
# (control-plane to control-plane) and is NEVER delivered to a tenant container.
SAUTAI_M2M_BASE_URL = env("SAUTAI_M2M_BASE_URL", default="")
SAUTAI_PLATFORM_SECRET = env("SAUTAI_PLATFORM_SECRET", default="")

# Cron ghost-sweep rollout gate. The Azure Container App env var name MUST
# match CRON_GHOST_SWEEP_TENANTS exactly (invariant §10).
CRON_GHOST_SWEEP_TENANTS = env("CRON_GHOST_SWEEP_TENANTS", default="")

# Proactive-delivery retry-suppression rollout gate. The Azure Container App
# env var name MUST match NBHD_DELIVERY_DEDUP_TENANTS exactly (invariant §10).
NBHD_DELIVERY_DEDUP_TENANTS = env("NBHD_DELIVERY_DEDUP_TENANTS", default="")

# CORS — production uses the explicit allowlist from CORS_ALLOWED_ORIGINS in base.py.
# Do NOT set CORS_ALLOW_ALL_ORIGINS here (that is dev-only).

# Logging — stdout/stderr goes to Container Apps Log Analytics.
# The `redact_byo_paste_body` filter is a defensive backstop to keep
# BYO subscription tokens out of access logs (primary defense lives in
# the BYO views — they never log request bodies).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "filters": {
        "redact_byo_paste_body": {
            "()": "apps.byo_models.logging_filters.RedactBYOPasteBody",
        },
        # Scrubs the shared Telegram bot token out of every record on its way to
        # stdout. httpx logs each outbound Bot API request at INFO with the token
        # embedded in the URL path; without this it streams into Log Analytics.
        "redact_telegram_token": {
            "()": "apps.router.logging_filters.RedactTelegramToken",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["redact_byo_paste_body", "redact_telegram_token"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
