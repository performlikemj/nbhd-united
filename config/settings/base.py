"""
Base Django settings for NBHD United — OpenClaw Control Plane.
"""
import os
from pathlib import Path
from datetime import timedelta

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
]

MIDDLEWARE = [
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
        "apps.tenants.authentication.JWTAuthenticationWithRLS",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
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

# QStash (replaces Celery — scheduled & on-demand tasks via webhooks)
QSTASH_CURRENT_SIGNING_KEY = env("QSTASH_CURRENT_SIGNING_KEY", default="")
QSTASH_NEXT_SIGNING_KEY = env("QSTASH_NEXT_SIGNING_KEY", default="")
QSTASH_TOKEN = env("QSTASH_TOKEN", default="")

# Upstash Redis (general cache / rate limiting)
UPSTASH_REDIS_URL = env("UPSTASH_REDIS_URL", default="")

# Native Redis URL (rediss://default:TOKEN@HOST:PORT) — used by django-redis.
# NOTE: This is NOT the same as UPSTASH_REDIS_URL (the REST API endpoint).
REDIS_URL = env("REDIS_URL", default="")

# Cache — use Redis when available (shared across workers & container revisions),
# fall back to in-process memory for local dev without Redis.
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
            },
        }
    }

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
ROUTER_RATE_LIMIT_PER_MINUTE = env.int("ROUTER_RATE_LIMIT_PER_MINUTE", default=30)
NBHD_INTERNAL_API_KEY = env("NBHD_INTERNAL_API_KEY", default="")
NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED = env.bool(
    "NBHD_INTERNAL_API_KEY_FALLBACK_ENABLED", default=True
)

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
    default="",
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
AZURE_KV_SECRET_BRAVE_API_KEY = env(
    "AZURE_KV_SECRET_BRAVE_API_KEY",
    default="brave-api-key",
)
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", default="")
AZURE_KV_SECRET_OPENROUTER_API_KEY = env(
    "AZURE_KV_SECRET_OPENROUTER_API_KEY",
    default="openrouter-api-key",
)
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

# Stripe pricing controls
STRIPE_PRICE_IDS = {
    "starter": env("STRIPE_PRICE_STARTER", default=""),
    "premium": env("STRIPE_PRICE_PREMIUM", default=""),
    "byok": env("STRIPE_PRICE_BYOK", default=""),
}

# Programmatic plan controls for checkout (frontend is also filtered, but this keeps payments safe).
# Empty means no paid plans are offered.
ENABLED_STRIPE_TIERS = env.list(
    "ENABLED_STRIPE_TIERS",
    default=[],
)

# Frontend URL (for redirects)
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:3000")
USAGE_DASHBOARD_SUBSCRIPTION_PRICE = env.float(
    "USAGE_DASHBOARD_SUBSCRIPTION_PRICE",
    default=12.0,
)

# API base URL (for OAuth callback redirects)
API_BASE_URL = env("API_BASE_URL", default="http://localhost:8000")

# Invite code for gated signup (set to gate registration; leave empty for open signup)
PREVIEW_ACCESS_KEY = env("PREVIEW_ACCESS_KEY", default="")

# OAuth client credentials
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")
SAUTAI_OAUTH_CLIENT_ID = env("SAUTAI_OAUTH_CLIENT_ID", default="")
SAUTAI_OAUTH_CLIENT_SECRET = env("SAUTAI_OAUTH_CLIENT_SECRET", default="")

# Composio (managed OAuth for Google integrations)
COMPOSIO_API_KEY = env("COMPOSIO_API_KEY", default="")
COMPOSIO_GMAIL_AUTH_CONFIG_ID = env("COMPOSIO_GMAIL_AUTH_CONFIG_ID", default="")
COMPOSIO_GCAL_AUTH_CONFIG_ID = env("COMPOSIO_GCAL_AUTH_CONFIG_ID", default="")
COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS = env.bool(
    "COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS",
    default=True,
)
