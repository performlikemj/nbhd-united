"""
Base Django settings for NBHD United — OpenClaw Control Plane.
"""

import os
from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_extensions",
    # django_celery_beat removed — using QStash for scheduling
    "djstripe",
    # Local apps
    "apps.tenants",
    "apps.billing",
    "apps.orchestrator",
    "apps.router",
    "apps.integrations",
    "apps.journal",
    "apps.automations",
    "apps.dashboard",
    "apps.cron",
    "apps.platform_logs",
    "apps.lessons",
    "apps.actions",
    "apps.finance",
    "apps.fuel",
    "apps.byo_models",
    "apps.insights",
    "apps.common",
]

MIDDLEWARE = [
    "config.middleware.RequestTimingMiddleware",
    "config.cache_middleware.ETagMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.tenants.middleware.TenantContextMiddleware",
    "apps.tenants.middleware.UserTimezoneMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://nbhd:nbhd@localhost:5432/nbhd_united"),
}

# Custom user model
AUTH_USER_MODEL = "tenants.User"

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.tenants.authentication.PersonalAccessTokenAuthentication",
        "apps.tenants.authentication.JWTAuthenticationWithRLS",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
}

# Simple JWT
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "SIGNING_KEY": env("JWT_SECRET", default=SECRET_KEY),
    "TOKEN_OBTAIN_SERIALIZER": "apps.tenants.serializers.EmailTokenObtainPairSerializer",
}

# CORS
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[])
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = (*default_headers,)
# Cache CORS preflight (OPTIONS) responses for a day so authenticated browser
# requests don't pay the preflight round-trip on every call. Frontend is on a
# separate origin (Azure SWA → Container App), so preflights happen often.
CORS_PREFLIGHT_MAX_AGE = 86400

# QStash (replaces Celery — scheduled & on-demand tasks via webhooks)
QSTASH_CURRENT_SIGNING_KEY = env("QSTASH_CURRENT_SIGNING_KEY", default="")
QSTASH_NEXT_SIGNING_KEY = env("QSTASH_NEXT_SIGNING_KEY", default="")
QSTASH_TOKEN = env("QSTASH_TOKEN", default="")

# Deploy hook auth used by CI to trigger protected endpoints
DEPLOY_SECRET = env("DEPLOY_SECRET", default="")

# Upstash Redis (general cache / rate limiting)
UPSTASH_REDIS_URL = env("UPSTASH_REDIS_URL", default="")

# Native Redis URL (rediss://default:TOKEN@HOST:PORT) — used by django-redis.
# NOTE: This is NOT the same as UPSTASH_REDIS_URL (the REST API endpoint).
REDIS_URL = env("REDIS_URL", default="")

# Cache — use Redis when available (shared across workers & container revisions),
# fall back to in-process memory for local dev without Redis.
#
# Upstash closes idle connections after ~30s. Two-layer protection:
#   1. `health_check_interval=25s` so the pool pre-pings before the idle close.
#   2. `Retry` on `ConnectionError`/`TimeoutError` so when (1) misses — e.g. a
#      burst of parallel requests all reach for stale connections at once —
#      redis-py transparently retries on a fresh connection instead of raising.
# If both fail, `IGNORE_EXCEPTIONS=True` + the decorator's BYPASS path keep
# user-facing 500s off the table.
if REDIS_URL:
    from redis.backoff import ExponentialBackoff  # noqa: E402
    from redis.exceptions import ConnectionError as _RedisConnectionError  # noqa: E402
    from redis.exceptions import TimeoutError as _RedisTimeoutError  # noqa: E402
    from redis.retry import Retry  # noqa: E402

    _REDIS_RETRY = Retry(ExponentialBackoff(cap=1, base=0.05), retries=2)
    _REDIS_RETRY_ERRORS = [_RedisConnectionError, _RedisTimeoutError]

    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                "SOCKET_CONNECT_TIMEOUT": 3,
                "SOCKET_TIMEOUT": 3,
                "IGNORE_EXCEPTIONS": True,
                "CONNECTION_POOL_KWARGS": {
                    "max_connections": 20,
                    "retry_on_timeout": True,
                    "retry_on_error": _REDIS_RETRY_ERRORS,
                    "retry": _REDIS_RETRY,
                    "socket_keepalive": True,
                    "health_check_interval": 25,
                },
            },
        }
    }
    # When IGNORE_EXCEPTIONS=True at the cache level, django-redis logs the
    # underlying error but returns None to callers. Make sure those errors
    # surface in logs so we can spot Upstash trouble.
    DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True
    DJANGO_REDIS_LOGGER = "nbhd.cache.redis"

# Stripe (dj-stripe)
STRIPE_LIVE_SECRET_KEY = env("STRIPE_LIVE_SECRET_KEY", default="")
STRIPE_TEST_SECRET_KEY = env("STRIPE_TEST_SECRET_KEY", default="")
STRIPE_LIVE_MODE = env.bool("STRIPE_LIVE_MODE", default=False)
DJSTRIPE_WEBHOOK_SECRET = env("DJSTRIPE_WEBHOOK_SECRET", default="")
DJSTRIPE_FOREIGN_KEY_TO_FIELD = "id"

# Telegram (shared bot)
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_BOT_USERNAME = env("TELEGRAM_BOT_USERNAME", default="NbhdUnitedBot")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", default="")
# Admin Telegram chat ID for health alerts (operator notifications)
ADMIN_TELEGRAM_CHAT_ID = env.int("ADMIN_TELEGRAM_CHAT_ID", default=0)
# Personal OpenClaw gateway for admin alerts (Cloudflare tunnel)
ADMIN_OPENCLAW_GATEWAY_URL = env("ADMIN_OPENCLAW_GATEWAY_URL", default="")
ADMIN_OPENCLAW_GATEWAY_TOKEN = env("ADMIN_OPENCLAW_GATEWAY_TOKEN", default="")
CF_ACCESS_CLIENT_ID = env("CF_ACCESS_CLIENT_ID", default="")
CF_ACCESS_CLIENT_SECRET = env("CF_ACCESS_CLIENT_SECRET", default="")
ROUTER_RATE_LIMIT_PER_MINUTE = env.int("ROUTER_RATE_LIMIT_PER_MINUTE", default=30)
# Shared internal API key for runtime auth between Django and tenant containers.
# All containers use the same key (stored in Azure Key Vault). This is safe
# because tenant containers are internal-only (external: false) — not reachable
# from the public internet.
NBHD_INTERNAL_API_KEY = env("NBHD_INTERNAL_API_KEY", default="")

# Disable daemon-thread side effects (USER.md push from envelope registry,
# QStash publish in journal post_save, etc.) for synchronous execution.
# Production: false → threads run in background so request handlers don't
# block on file-share writes. Tests + dev: set to true so test teardown
# doesn't race with leftover daemon threads holding DB connections.
NBHD_DISABLE_BACKGROUND_THREADS = env.bool("NBHD_DISABLE_BACKGROUND_THREADS", default=False)

# LINE Messaging API (shared bot)
LINE_CHANNEL_ACCESS_TOKEN = env("LINE_CHANNEL_ACCESS_TOKEN", default="")
LINE_CHANNEL_SECRET = env("LINE_CHANNEL_SECRET", default="")
LINE_BOT_ID = env("LINE_BOT_ID", default="")  # e.g. "@nbhd-united"

# Anthropic API (shared key for all OpenClaw instances)
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
BRAVE_API_KEY = env("BRAVE_API_KEY", default="")
OPENCLAW_GOOGLE_PLUGIN_ID = env("OPENCLAW_GOOGLE_PLUGIN_ID", default="")
OPENCLAW_GOOGLE_PLUGIN_PATH = env(
    "OPENCLAW_GOOGLE_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-google-tools",
)
OPENCLAW_JOURNAL_PLUGIN_ID = env("OPENCLAW_JOURNAL_PLUGIN_ID", default="")
OPENCLAW_JOURNAL_PLUGIN_PATH = env(
    "OPENCLAW_JOURNAL_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-journal-tools",
)
OPENCLAW_USAGE_PLUGIN_ID = env(
    "OPENCLAW_USAGE_PLUGIN_ID",
    default="nbhd-usage-reporter",
)
# Backward-compatibility alias for container/image wiring.
OPENCLAW_USAGE_REPORTER_PLUGIN_ID = env(
    "OPENCLAW_USAGE_REPORTER_PLUGIN_ID",
    default="",
)
OPENCLAW_USAGE_REPORTER_PLUGIN_PATH = env(
    "OPENCLAW_USAGE_REPORTER_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-usage-reporter",
)
OPENCLAW_REDDIT_PLUGIN_ID = env("OPENCLAW_REDDIT_PLUGIN_ID", default="nbhd-reddit-tools")
OPENCLAW_REDDIT_PLUGIN_PATH = env(
    "OPENCLAW_REDDIT_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-reddit-tools",
)
OPENCLAW_SETTINGS_PLUGIN_ID = env("OPENCLAW_SETTINGS_PLUGIN_ID", default="nbhd-settings-tools")
OPENCLAW_SETTINGS_PLUGIN_PATH = env(
    "OPENCLAW_SETTINGS_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-settings-tools",
)
# Routing-context plugin — injects workspace catalogue into the system prompt
# (before_prompt_build) + rejects degenerate model output (before_agent_finalize
# / message_sending). Unconditional in production so every tenant gets the
# guardrails. Tests disable via OPENCLAW_ROUTING_CONTEXT_PLUGIN_ID="".
# See CONTINUITY_workspace-routing-fix.md.
OPENCLAW_ROUTING_CONTEXT_PLUGIN_ID = env(
    "OPENCLAW_ROUTING_CONTEXT_PLUGIN_ID",
    default="nbhd-routing-context",
)
OPENCLAW_ROUTING_CONTEXT_PLUGIN_PATH = env(
    "OPENCLAW_ROUTING_CONTEXT_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-routing-context",
)
COMPOSIO_REDDIT_AUTH_CONFIG_ID = env("COMPOSIO_REDDIT_AUTH_CONFIG_ID", default="")

OPENCLAW_CONTAINER_SECRET_BACKEND = env(
    "OPENCLAW_CONTAINER_SECRET_BACKEND",
    default="keyvault",
)
AZURE_KV_SECRET_ANTHROPIC_API_KEY = env(
    "AZURE_KV_SECRET_ANTHROPIC_API_KEY",
    default="anthropic-api-key",
)
AZURE_KV_SECRET_OPENAI_API_KEY = env(
    "AZURE_KV_SECRET_OPENAI_API_KEY",
    default="openai-api-key",
)
AZURE_KV_SECRET_TELEGRAM_BOT_TOKEN = env(
    "AZURE_KV_SECRET_TELEGRAM_BOT_TOKEN",
    default="telegram-bot-token",
)
AZURE_KV_SECRET_NBHD_INTERNAL_API_KEY = env(
    "AZURE_KV_SECRET_NBHD_INTERNAL_API_KEY",
    default="nbhd-internal-api-key",
)
AZURE_KV_SECRET_TELEGRAM_WEBHOOK_SECRET = env(
    "AZURE_KV_SECRET_TELEGRAM_WEBHOOK_SECRET",
    default="telegram-webhook-secret",
)
AZURE_KV_SECRET_LINE_CHANNEL_ACCESS_TOKEN = env(
    "AZURE_KV_SECRET_LINE_CHANNEL_ACCESS_TOKEN",
    default="line-channel-access-token",
)
AZURE_KV_SECRET_LINE_CHANNEL_SECRET = env(
    "AZURE_KV_SECRET_LINE_CHANNEL_SECRET",
    default="line-channel-secret",
)
AZURE_KV_SECRET_BRAVE_API_KEY = env(
    "AZURE_KV_SECRET_BRAVE_API_KEY",
    default="brave-api-key",
)
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", default="")
AZURE_KV_SECRET_OPENROUTER_API_KEY = env(
    "AZURE_KV_SECRET_OPENROUTER_API_KEY",
    default="openrouter-api-key",
)

# Per-tenant OpenRouter sub-keys (PR #1.6).
#
# OPENROUTER_API_BASE: API root for /v1/keys (POST/DELETE), /v1/key (GET).
# AZURE_KV_SECRET_OPENROUTER_MANAGEMENT_KEY: central KV secret holding the
#   OR management key. Distinct from the regular API key — must be created
#   manually in the OpenRouter dashboard and written to KV by an operator.
# OPENROUTER_PER_TENANT_KEYS_ENABLED: feature flag. When False, provisioning
#   skips sub-key creation and containers continue to use the shared
#   OPENROUTER_API_KEY. When True, new tenants get a sub-key + per-tenant
#   env-var injection. Existing tenants are migrated via the
#   ``backfill_openrouter_keys`` management command.
OPENROUTER_API_BASE = env("OPENROUTER_API_BASE", default="https://openrouter.ai/api/v1")
AZURE_KV_SECRET_OPENROUTER_MANAGEMENT_KEY = env(
    "AZURE_KV_SECRET_OPENROUTER_MANAGEMENT_KEY",
    default="openrouter-management-key",
)
OPENROUTER_PER_TENANT_KEYS_ENABLED = env.bool(
    "OPENROUTER_PER_TENANT_KEYS_ENABLED",
    default=False,
)
# GRAVITY_ENABLED: product-level kill switch for the Gravity (finance) module.
# Fail-safe OFF by default: while False, finance is paused platform-wide
# regardless of any tenant's stored ``finance_enabled`` flag — no finance plugin
# is loaded into containers, no finance state is injected into USER.md, the
# weekly check-in / synthesis don't run, and the UI doesn't offer it. This is a
# deliberate privacy pause: financial figures currently egress to the LLM
# provider raw (the redactor masks identities, not amounts) with no retention
# guarantee configured. Re-enable (set the env var True) only once on-device /
# zero-retention inference or pre-egress amount-masking is in place.
# dev + test settings override this to True so the existing suite + local dev
# exercise the feature; production inherits the False default.
GRAVITY_ENABLED = env.bool("GRAVITY_ENABLED", default=False)
AZURE_KV_SECRET_SOUL_MD = env(
    "AZURE_KV_SECRET_SOUL_MD",
    default="nbhd-soul-md",
)
AZURE_KV_SECRET_AGENTS_MD = env(
    "AZURE_KV_SECRET_AGENTS_MD",
    default="nbhd-agents-md",
)

# Azure
AZURE_SUBSCRIPTION_ID = env("AZURE_SUBSCRIPTION_ID", default="")
AZURE_RESOURCE_GROUP = env("AZURE_RESOURCE_GROUP", default="rg-nbhd-prod")
AZURE_LOCATION = env("AZURE_LOCATION", default="westus2")
AZURE_CONTAINER_ENV_ID = env("AZURE_CONTAINER_ENV_ID", default="")
AZURE_ACR_SERVER = env("AZURE_ACR_SERVER", default="nbhdunited.azurecr.io")
OPENCLAW_IMAGE_TAG = os.environ.get("OPENCLAW_IMAGE_TAG", "latest")
AZURE_KEY_VAULT_NAME = env("AZURE_KEY_VAULT_NAME", default="kv-nbhd-prod")
AZURE_PROVISIONER_CLIENT_ID = env("AZURE_PROVISIONER_CLIENT_ID", default="")
AZURE_STORAGE_ACCOUNT_NAME = env("AZURE_STORAGE_ACCOUNT_NAME", default="")

# Stripe pricing — single plan
STRIPE_PRICE_ID = env("STRIPE_PRICE_STARTER", default="")

# Frontend URL (for redirects)
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:3000")

# Optional URL for the 2-minute walkthrough embedded in the Day-0
# welcome email. Empty (default) → the walkthrough block is omitted
# from the email body, so we can ship without a video and swap one
# in later via env var without touching code.
WELCOME_VIDEO_URL = env("WELCOME_VIDEO_URL", default="")

# Recipient for operational platform alerts (LINE quota pre-warn, etc.).
# Already referenced by env-var in apps/tenants/migrations/0044_set_owner_exempt.py;
# also exposed here so app code can read it via settings rather than os.environ.
PLATFORM_OWNER_EMAIL = env("PLATFORM_OWNER_EMAIL", default="")

# Password reset link TTL — 7 days (Django default is 3). Picked so a
# user who receives a campaign-driven reset email and opens it on a
# Wednesday isn't locked out by the weekend. Applies to every reset
# flow, not just campaigns; 7 days is a reasonable security ceiling
# for emailed reset links.
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24 * 7
USAGE_DASHBOARD_SUBSCRIPTION_PRICE = env.float(
    "USAGE_DASHBOARD_SUBSCRIPTION_PRICE",
    default=12.0,
)
SUPABASE_MONTHLY_COST = env.float("SUPABASE_MONTHLY_COST", default=25.0)

# API base URL (for OAuth callback redirects)
API_BASE_URL = env("API_BASE_URL", default="http://localhost:8000")

# Invite code for gated signup (set to gate registration; leave empty for open signup)
PREVIEW_ACCESS_KEY = env("PREVIEW_ACCESS_KEY", default="")

# OAuth client credentials
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")
SAUTAI_OAUTH_CLIENT_ID = env("SAUTAI_OAUTH_CLIENT_ID", default="")
SAUTAI_OAUTH_CLIENT_SECRET = env("SAUTAI_OAUTH_CLIENT_SECRET", default="")

# Composio (managed OAuth integrations)
COMPOSIO_API_KEY = env("COMPOSIO_API_KEY", default="")
COMPOSIO_GMAIL_AUTH_CONFIG_ID = env("COMPOSIO_GMAIL_AUTH_CONFIG_ID", default="")
COMPOSIO_GCAL_AUTH_CONFIG_ID = env("COMPOSIO_GCAL_AUTH_CONFIG_ID", default="")
COMPOSIO_REDDIT_AUTH_CONFIG_ID = env("COMPOSIO_REDDIT_AUTH_CONFIG_ID", default="")
COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS = env.bool(
    "COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS",
    default=True,
)

# Custom test runner — disconnects the CronJob → reconciler signal during
# test runs so the publish_task sync fallback (no QSTASH_TOKEN) doesn't
# accumulate DB connections + outbound HTTP attempts on every CronJob save.
# See ``config/test_runner.py`` for the full rationale.
TEST_RUNNER = "config.test_runner.QuietCronSignalRunner"
