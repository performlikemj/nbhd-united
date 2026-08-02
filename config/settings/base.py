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
    "apps.yardtalk",
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
    "apps.core",
    "apps.byo_models",
    "apps.insights",
    "apps.friends",
    "apps.common",
    # apps.pii is a library module (no models); registered so its management
    # commands (denylist_degenerate_pii) are discoverable.
    "apps.pii",
    # apps.crypto is a library module (no models) — the envelope-encryption
    # key service (encryption-at-rest Phase 1). Registered for app discovery
    # only; ready() intentionally stays empty (pre-warm is Phase 1 PR4, and
    # must never run during migrate — see apps/crypto/apps.py).
    "apps.crypto",
    # apps.evals — the production eval system (see docs/evals-directive.md).
    # Platform-level tables (eval_runs / eval_results), not tenant-scoped;
    # ready() intentionally stays empty (it runs during migrate, before this
    # app's own tables exist — see apps/evals/apps.py).
    "apps.evals",
    # apps.steward — portfolio-scoped deterministic expectations/watchtower.
    "apps.steward",
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

# Isolated test-DB name for the /integrate train (unset = Django default test_<name>):
_test_db_name = env("DJANGO_TEST_DB_NAME", default=None)
if _test_db_name:
    DATABASES["default"].setdefault("TEST", {})["NAME"] = _test_db_name

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
    "DEFAULT_THROTTLE_RATES": {
        "yardtalk_license_validate": "30/minute",
    },
}

# Simple JWT
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "SIGNING_KEY": env("JWT_SECRET", default=SECRET_KEY),
    "TOKEN_OBTAIN_SERIALIZER": "apps.tenants.serializers.EmailTokenObtainPairSerializer",
    # NOTE: refresh-token rotation (ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION)
    # is intentionally NOT enabled here. Both clients are now rotation-READY (web
    # api.ts + iOS RemoteAPI persist the rotated refresh and single-flight), but
    # flipping the flag safely needs a coordinated rollout that this change does not
    # do: (1) frontend-first deploy ordering so open/old web bundles don't discard
    # the rotated token and get force-logged-out; (2) the iOS refresh-retry loop must
    # re-read the keychain between attempts so a lost-response double-spend can't
    # silently sign the user out; (3) a scheduled `flushexpiredtokens` to reap the
    # OutstandingToken/BlacklistedToken rows rotation creates; (4) cross-tab refresh
    # coordination on web. Enable as a deliberate follow-up, not a drive-by.
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

# Steward Phase 1 — portfolio-scoped deterministic evidence ingestion,
# direct urgent delivery, and the external dead-man. Every value is optional
# at process boot; ingest fails closed with 503 while its secret is empty.
STEWARD_INGEST_SECRET = env("STEWARD_INGEST_SECRET", default="")
STEWARD_TELEGRAM_BOT_TOKEN = env("STEWARD_TELEGRAM_BOT_TOKEN", default="")
STEWARD_TELEGRAM_CHAT_ID = env("STEWARD_TELEGRAM_CHAT_ID", default="")
STEWARD_ALERT_EMAIL = env("STEWARD_ALERT_EMAIL", default="")
STEWARD_DEADMAN_URL = env("STEWARD_DEADMAN_URL", default="")
STEWARD_GITHUB_TOKEN = env("STEWARD_GITHUB_TOKEN", default="")
STEWARD_ASC_KEY_ID = env("STEWARD_ASC_KEY_ID", default="")
STEWARD_ASC_ISSUER_ID = env("STEWARD_ASC_ISSUER_ID", default="")
STEWARD_ASC_PRIVATE_KEY = env("STEWARD_ASC_PRIVATE_KEY", default="").replace("\\n", "\n")

# Core AI on-device model (iOS 27 bring-your-own model) delivery.
# Django serves only the small JWT-gated manifest (GET /api/v1/coreai/model/manifest/);
# the big model files are hosted off-Django (Azure Blob / CDN) under COREAI_MODEL_BASE_URL.
# The manifest JSON is produced by `manage.py generate_coreai_manifest`. Leave the base URL
# empty to disable on-device-model delivery (the endpoint 404s and the app falls back).
COREAI_MODEL_BASE_URL = env("COREAI_MODEL_BASE_URL", default="")
COREAI_MODEL_MANIFEST_PATH = env(
    "COREAI_MODEL_MANIFEST_PATH",
    default=str(BASE_DIR / "apps" / "router" / "coreai_manifest.json"),
)

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
YARDTALK_STRIPE_PRICE_ID = env("YARDTALK_STRIPE_PRICE_ID", default="")
YARDTALK_LICENSE_RECEIPT_SECRET = env(
    "YARDTALK_LICENSE_RECEIPT_SECRET",
    default=SECRET_KEY,
)

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

# Neighborhood DB backstop (PR8). When on, trusted server-side background work
# that reads the friends cross-tenant tables (scrub / position refresh / envelope
# render / chat push) marks its connection service-role so the FORCE-RLS policies
# on shared_lessons / lesson_share_grants / friend_messages don't hide rows from
# it. Those policies are INERT while the app's Postgres role bypasses RLS
# (superuser / service_role) — they only begin enforcing if the app connects as a
# non-BYPASSRLS role (see `manage.py check_friends_rls`). The accessor
# (apps/friends/access.py) is always the primary boundary; this is defense in
# depth. Turn off only to skip the extra session-var round-trips once the app
# role is confirmed to bypass RLS.
FRIENDS_DB_BACKSTOP = env.bool("FRIENDS_DB_BACKSTOP", default=True)

# LINE Messaging API (shared bot)
LINE_CHANNEL_ACCESS_TOKEN = env("LINE_CHANNEL_ACCESS_TOKEN", default="")
LINE_CHANNEL_SECRET = env("LINE_CHANNEL_SECRET", default="")
LINE_BOT_ID = env("LINE_BOT_ID", default="")  # e.g. "@nbhd-united"

# Anthropic API (shared key for all OpenClaw instances)
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
BRAVE_API_KEY = env("BRAVE_API_KEY", default="")

# Gemini TTS — Core pillar meditation render (server-side, key stays here).
# Secret lives in Key Vault; set GEMINI_API_KEY on the Container App. Never echo it.
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_TTS_MODEL = env("GEMINI_TTS_MODEL", default="gemini-2.5-flash-preview-tts")
# PRIMARY model that AUTHORS the meditation manifest (OpenRouter, JSON mode) — the
# web orb's compose path. compose.py fronts a low-cost fallback chain with this id
# (then DeepSeek V4 Flash, then Pro). Default is Gemma 4 31B: it's the cheap roster
# model OpenRouter lists for structured outputs (English-native, non-reasoning), so
# it steers reliably on this structured task; the DeepSeek reasoning models are the
# fallbacks. See apps/core/compose.py.
CORE_COMPOSE_MODEL = env("CORE_COMPOSE_MODEL", default="openrouter/google/gemma-4-31b-it")
# Bounded-parallel TTS calls per render — kept low to respect low-tier per-minute
# rate caps (concurrent calls burst past the cap; the 429 backoff handles the rest).
CORE_RENDER_CONCURRENCY = env.int("CORE_RENDER_CONCURRENCY", default=2)
# Render-wide soft deadline (seconds): no NEW TTS call starts past this, keeping
# the synchronous QStash-triggered render under the gunicorn ~300s budget (worst
# case ~240 + one in-flight 45s call + ~20s concat/transcode < 300).
CORE_RENDER_DEADLINE_SECONDS = env.int("CORE_RENDER_DEADLINE_SECONDS", default=240)
# A RENDERING session older than this (minutes) is treated as a dead claim and
# may be re-taken — recovers a render whose worker was killed mid-flight.
CORE_RENDER_STALE_MINUTES = env.int("CORE_RENDER_STALE_MINUTES", default=15)
CORE_RENDER_MAX_ATTEMPTS = env.int("CORE_RENDER_MAX_ATTEMPTS", default=3)
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
OPENCLAW_SAUTAI_PLUGIN_ID = env("OPENCLAW_SAUTAI_PLUGIN_ID", default="nbhd-sautai-tools")
OPENCLAW_SAUTAI_PLUGIN_PATH = env(
    "OPENCLAW_SAUTAI_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-sautai-tools",
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
# Activity-stream plugin — narrates agent tool-use/composing to the control plane
# for the in-app "thinking" state + iOS-27 Siri Live Activity (HER_SIRI_ARCHITECTURE
# §4.3). OPT-IN: ID defaults to "" so it's built into the image but inert (no fleet
# load) until enabled by setting OPENCLAW_ACTIVITY_STREAM_PLUGIN_ID="nbhd-activity-stream"
# — flip on once the client consumes `phase`/`phase_detail`.
OPENCLAW_ACTIVITY_STREAM_PLUGIN_ID = env("OPENCLAW_ACTIVITY_STREAM_PLUGIN_ID", default="")
OPENCLAW_ACTIVITY_STREAM_PLUGIN_PATH = env(
    "OPENCLAW_ACTIVITY_STREAM_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-activity-stream",
)
# Stream-progress plugin — per-step partial assistant text (pseudo-streaming) to
# the same chat/progress endpoint (text + monotonic seq) so a polling client can
# render text as the turn composes instead of waiting for the whole reply. OPT-IN
# like activity-stream: ID defaults to "" so it's built into the image but inert
# (no fleet load) until enabled by setting
# OPENCLAW_STREAM_PROGRESS_PLUGIN_ID="nbhd-stream-progress" — flip on once the
# client consumes `partial_text`/`partial_seq`.
OPENCLAW_STREAM_PROGRESS_PLUGIN_ID = env("OPENCLAW_STREAM_PROGRESS_PLUGIN_ID", default="")
OPENCLAW_STREAM_PROGRESS_PLUGIN_PATH = env(
    "OPENCLAW_STREAM_PROGRESS_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-stream-progress",
)
# Document taint guard plugin — instruction isolation + egress taint gate for
# uploaded documents/photos (docs/upload-security-threat-model.md
# P0-1/P0-2/P1-2). Unconditional in production so every tenant gets the
# guard, same as nbhd-routing-context — the pdf/image tools are fleet-wide.
# Tests disable via OPENCLAW_DOC_TAINT_GUARD_PLUGIN_ID="".
OPENCLAW_DOC_TAINT_GUARD_PLUGIN_ID = env(
    "OPENCLAW_DOC_TAINT_GUARD_PLUGIN_ID",
    default="nbhd-doc-taint-guard",
)
OPENCLAW_DOC_TAINT_GUARD_PLUGIN_PATH = env(
    "OPENCLAW_DOC_TAINT_GUARD_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-doc-taint-guard",
)
# Automation-tools plugin — the agent's typed cron-create tools
# (nbhd_cron_create_pure_reminder / _quote_user_intent / _domain_summary), loaded
# per-tenant on ``experimental_typed_crons``.
#
# These lines are load-bearing and were MISSING until 2026-07-14: nothing read
# these names from the environment, so ``getattr(settings, ...)`` in
# config_generator always fell back to its hardcoded literal, and
# ``scripts/openclaw_config_doctor_smoke.sh``'s ``export
# OPENCLAW_AUTOMATION_PLUGIN_ID=""`` was a SILENT NO-OP. It went unnoticed only
# because the tenant flag defaulted False, so the plugin never reached a smoke
# config. Flipping that default surfaced it as a doctor failure ("plugin path not
# found") — the plugin ships in the image (Dockerfile.openclaw), but CI has no
# /opt/nbhd/plugins tree. Every sibling plugin ID is env-readable; this one now is
# too, which is what makes the smoke disable actually disable.
OPENCLAW_AUTOMATION_PLUGIN_ID = env(
    "OPENCLAW_AUTOMATION_PLUGIN_ID",
    default="nbhd-automation-tools",
)
OPENCLAW_AUTOMATION_PLUGIN_PATH = env(
    "OPENCLAW_AUTOMATION_PLUGIN_PATH",
    default="/opt/nbhd/plugins/nbhd-automation-tools",
)
# Cron enforcement plugin — fire-time typed-cron enforcement, rebuilt in
# #1117. Ships dark (empty defaults): production enables by setting both env
# vars on the container app; tests/smoke disable via ID="".
OPENCLAW_CRON_ENFORCEMENT_PLUGIN_ID = env(
    "OPENCLAW_CRON_ENFORCEMENT_PLUGIN_ID",
    default="",
)
OPENCLAW_CRON_ENFORCEMENT_PLUGIN_PATH = env(
    "OPENCLAW_CRON_ENFORCEMENT_PLUGIN_PATH",
    default="",
)
# "log_only" (default) logs what the egress gate would have blocked without
# blocking it; "enforce" hard-blocks. Fleet-wide flip = one env var change +
# apply-pending-configs, no per-tenant migration. See the rollout plan in
# docs/upload-security-threat-model.md.
DOC_TAINT_GATE_MODE = env("DOC_TAINT_GATE_MODE", default="log_only")
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
# Apple Maps Server API. The private `.p8` stays in Key Vault; Django holds
# only its secret name plus the non-secret key/team identifiers.
AZURE_KV_SECRET_APPLE_MAPS_AUTHKEY = env(
    "AZURE_KV_SECRET_APPLE_MAPS_AUTHKEY",
    default="apple-maps-server-authkey",
)
NBHD_APPLE_MAPS_KEY_ID = env("NBHD_APPLE_MAPS_KEY_ID", default="")
NBHD_APPLE_MAPS_TEAM_ID = env("NBHD_APPLE_MAPS_TEAM_ID", default="")
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", default="")
AZURE_KV_SECRET_OPENROUTER_API_KEY = env(
    "AZURE_KV_SECRET_OPENROUTER_API_KEY",
    default="openrouter-api-key",
)

# Galaxy co-pilot — the in-game line shown when a player lands on a star.
# Small/fast model (latency-sensitive, ~1 short sentence) called server-side
# via the shared OpenRouter key, attributed is_system. COPILOT_LLM_ENABLED is a
# kill-switch: off → the endpoint serves only the deterministic warm fallback,
# no LLM spend, no deploy. See apps/lessons/copilot.py.
COPILOT_MODEL = env("COPILOT_MODEL", default="anthropic/claude-haiku-4.5")
COPILOT_LLM_ENABLED = env.bool("COPILOT_LLM_ENABLED", default=True)

# Siri tiered responder (HER_SIRI_ARCHITECTURE.md). The Tier-2 fast responder
# reuses the fleet "fast" model (the slot mapped to scheduled/worker tasks) —
# NOT a per-tenant field. Override the ordered candidate list via env if needed.
SIRI_FAST_MODELS = env.list(
    "SIRI_FAST_MODELS",
    default=["openrouter/deepseek/deepseek-v4-flash", "openrouter/deepseek/deepseek-v4-pro"],
)

# APNs (Apple Push Notification service) — token-based (.p8) auth. The push path
# is fully gated: a logged no-op unless ALL of these are set (operator provisions
# the .p8 from the Apple Developer account → Key Vault → env), and HTTP/2
# requires httpx[http2] (the `h2` package). See apps/common/apns.py.
#   APNS_AUTH_KEY    — the .p8 EC private key contents (PEM string).
#   APNS_KEY_ID      — the 10-char key id of that .p8.
#   APNS_TEAM_ID     — the 10-char Apple Developer team id.
#   APNS_BUNDLE_ID   — the app bundle id (apns-topic), e.g. org.hoodunited.nbhd.
#   APNS_USE_SANDBOX — True for sandbox/dev builds (api.sandbox.push.apple.com).
APNS_AUTH_KEY = env("APNS_AUTH_KEY", default="").replace("\\n", "\n")
APNS_KEY_ID = env("APNS_KEY_ID", default="")
APNS_TEAM_ID = env("APNS_TEAM_ID", default="")
APNS_BUNDLE_ID = env("APNS_BUNDLE_ID", default="")
APNS_USE_SANDBOX = env.bool("APNS_USE_SANDBOX", default=False)

# Sign in with Apple (web Services ID popup flow).
APPLE_SIWA_SERVICES_ID = env("APPLE_SIWA_SERVICES_ID", default="")
APPLE_SIWA_TEAM_ID = env("APPLE_SIWA_TEAM_ID", default="")
APPLE_SIWA_KEY_ID = env("APPLE_SIWA_KEY_ID", default="")
APPLE_SIWA_PRIVATE_KEY = env("APPLE_SIWA_PRIVATE_KEY", default="").replace("\\n", "\n")
APPLE_SIWA_REDIRECT_URI = env("APPLE_SIWA_REDIRECT_URI", default="https://hoodunited.org")
APPLE_SIWA_TOKEN_ENC_KEYS = env.list("APPLE_SIWA_TOKEN_ENC_KEYS", default=[])
APPLE_SIWA_TRANSACTION_TTL_SECONDS = env.int(
    "APPLE_SIWA_TRANSACTION_TTL_SECONDS",
    default=600,
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
# Per-tenant OR sub-key ceiling for budget-exempt tenants (canary, internal
# accounts). They run without a spend cap, so their sub-key must sit well above
# any realistic monthly usage — otherwise OR 402s and the credit-limit breaker
# hibernates + suspends them (the 2026-06-10 canary outage).
OPENROUTER_EXEMPT_KEY_LIMIT = env.float("OPENROUTER_EXEMPT_KEY_LIMIT", default=1000.0)
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
# Encryption-at-rest (Phase 1, dark — no user data encrypted yet). Separate
# vault from AZURE_KEY_VAULT_NAME: KEKs never live alongside the platform's
# operational secrets. AZURE_DECRYPT_BROKER_CLIENT_ID is a DIFFERENT managed
# identity from AZURE_PROVISIONER_CLIENT_ID — the provisioner can wrap/create/
# delete KEKs but must NOT be able to unwrap; only the decrypt-broker identity
# can. See apps/orchestrator/azure_client.py create_tenant_kek/wrap_dek/
# unwrap_dek and CONTINUITY_encryption-phase1.md.
AZURE_KEK_VAULT_NAME = env("AZURE_KEK_VAULT_NAME", default="kv-nbhd-keks")
AZURE_DECRYPT_BROKER_CLIENT_ID = env("AZURE_DECRYPT_BROKER_CLIENT_ID", default="")

# Stripe pricing — single plan.
# NOTE: the Django setting is STRIPE_PRICE_ID, but the env var it reads is
# STRIPE_PRICE_STARTER. When configuring the Container App, set
# STRIPE_PRICE_STARTER=price_… (the live-account Starter price), NOT
# STRIPE_PRICE_ID. All code references settings.STRIPE_PRICE_ID. Legacy
# STRIPE_PRICE_BASIC/PLUS are unused (tiers collapsed to a single "starter").
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

# Legacy per-run eval failure/reaper emails. Steward's daily digest is now the
# routine eval-health surface; production may opt these alerts back in without a
# code change. Watcher-of-the-watcher email paths bypass this flag in alerting.py.
EVAL_EMAIL_ALERTS_ENABLED = False

# Eval system journey canaries (Wave B, docs/evals-wave-b-plan.md). The probes
# target SYNTHETIC tenants by id (never a hardcoded UUID) — provisioning them is
# a separate ops step. Both names are plumbed now; only eval-journey is
# provisioned in Wave B (eval-behavior is deferred to Wave D). Empty default so
# resolve_journey_tenant() can raise a loud config error when a probe runs
# unconfigured (INVARIANT #3 — no silent skip).
EVAL_JOURNEY_TENANT_ID = env("EVAL_JOURNEY_TENANT_ID", default="")
EVAL_BEHAVIOR_TENANT_ID = env("EVAL_BEHAVIOR_TENANT_ID", default="")
EVAL_JOURNEY_PAT = env("EVAL_JOURNEY_PAT", default="")
# Wave D behavior suite: the PAT that authenticates scenario turns as the synthetic
# behavior tenant's user. Wave D (#1168) read this via getattr() to stay a
# zero-settings-change PR; the ops provisioning step mints the PAT + sets the secret,
# so it is promoted to a real env() setting here (mirrored in production.py so the
# Azure Container App env var name matches). Empty default → resolve_behavior_pat()
# raises a loud config error when the suite runs unconfigured (INVARIANT #3).
EVAL_BEHAVIOR_PAT = env("EVAL_BEHAVIOR_PAT", default="")

# Eval Suite 4 — production SLO snapshot thresholds (docs/evals-directive.md §Suite
# 4). The nightly, metadata-only snapshot (apps/evals/suites/slo_snapshot.py) owns
# the sane defaults in code (DEFAULT_SLO_THRESHOLDS); this env JSON overrides any
# SUBSET without a deploy — e.g. EVAL_SLO_THRESHOLDS='{"reply_latency_p95_ms": 30000}'.
# Empty default = use the code defaults verbatim. Latencies are in milliseconds; a
# breached metric closes its snapshot run FAIL → owner alert + DLQ. Unknown keys are
# ignored by thresholds() so a typo can never introduce a phantom metric.
EVAL_SLO_THRESHOLDS = env.json("EVAL_SLO_THRESHOLDS", default={})

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

# Cap on the per-tenant slice of the shared database bill. An even $cost/N split
# overcharges a small fleet — at 3 tenants each would carry ~$8.33, which alone
# exceeds the subscription price and structurally zeroes the open-books surplus
# (so no donation ever shows). A database instance serves far more tenants than
# that, so the honest *marginal* per-tenant cost is small; the cap reflects the
# fair per-tenant DB cost at target scale so donations stay viable before scale.
INFRA_DB_SHARE_CAP = env.float("INFRA_DB_SHARE_CAP", default=0.50)

# Flat per-tenant estimate of the amortized shared *platform* overhead — the
# always-on Django control plane (nbhd-django-westus2), container registry, Key
# Vault and Log Analytics, i.e. the rg-nbhd-prod costs not attributed to any
# per-tenant oc-*/ws-* resource. Used ONLY when live Azure Cost Management data
# is unavailable (brand-new tenant before the first cron run, AZURE_MOCK, or a
# failed/empty query). The real figure is computed daily by the cron as
# (total resource-group cost − attributed container/storage cost) / active
# tenants; this flat placeholder keeps the "true monthly cost" figure stable and
# deliberately conservative before real data lands, mirroring how
# ESTIMATE_CONTAINER/ESTIMATE_STORAGE seed the container/storage lines. Has a
# sane default so no Azure Container App env change is required to ship.
INFRA_PLATFORM_SHARE_ESTIMATE = env.float("INFRA_PLATFORM_SHARE_ESTIMATE", default=2.0)

# The platform's pledged share of gross subscription revenue that goes to food
# initiatives. Owner-tunable via env. The donation ledger records
# `subscription_price * DONATION_REVENUE_PCT / 100` for every paying subscriber
# each month — a flat percentage of what we actually collect, NOT a surplus
# calculation (which donated less the more a subscriber used the product and
# leaned on fragile Azure cost attribution).
DONATION_REVENUE_PCT = env.float("DONATION_REVENUE_PCT", default=10.0)

# API base URL (for OAuth callback redirects)
API_BASE_URL = env("API_BASE_URL", default="http://localhost:8000")

# Invite code for gated signup (set to gate registration; leave empty for open signup)
PREVIEW_ACCESS_KEY = env("PREVIEW_ACCESS_KEY", default="")

# Web→app PKCE handoff (iOS "Create an account"). The one-time authorization
# code's TTL and the redirect_uri allowlist enforced at /authorize/ + /exchange/.
# Defaults match the shipped iOS contract (WebAuth.redirectURI = nbhd://auth/callback).
AUTH_EXCHANGE_CODE_TTL_SECONDS = env.int("AUTH_EXCHANGE_CODE_TTL_SECONDS", default=300)
AUTH_ALLOWED_REDIRECT_URIS = env.list("AUTH_ALLOWED_REDIRECT_URIS", default=["nbhd://auth/callback"])

# OAuth client credentials
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")
SAUTAI_OAUTH_CLIENT_ID = env("SAUTAI_OAUTH_CLIENT_ID", default="")
SAUTAI_OAUTH_CLIENT_SECRET = env("SAUTAI_OAUTH_CLIENT_SECRET", default="")

# sautai Phase 0 M2M bridge (SAUTAI_M2M_BASE_URL + SAUTAI_PLATFORM_SECRET) is
# defined in config/settings/production.py, NOT here — the env-var names must
# match the Azure Container App exactly (invariant §10) and there is
# deliberately NO default host/secret, so an unset value fails loud rather than
# silently calling the wrong sautai. Dev/test read them via override_settings.
# The OAuth stub above is separate and points at a sautai OAuth server that
# does not exist yet.

# Composio (managed OAuth integrations)
COMPOSIO_API_KEY = env("COMPOSIO_API_KEY", default="")
COMPOSIO_GMAIL_AUTH_CONFIG_ID = env("COMPOSIO_GMAIL_AUTH_CONFIG_ID", default="")
COMPOSIO_GCAL_AUTH_CONFIG_ID = env("COMPOSIO_GCAL_AUTH_CONFIG_ID", default="")
COMPOSIO_REDDIT_AUTH_CONFIG_ID = env("COMPOSIO_REDDIT_AUTH_CONFIG_ID", default="")
COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS = env.bool(
    "COMPOSIO_ALLOW_MULTIPLE_ACCOUNTS",
    default=True,
)

# Fuel edit-lock TTL — how long a single user-side acquire keeps the runtime
# from clobbering the workout. Heartbeats every ~half this value renew the
# lock; release endpoint clears it explicitly. Defaults to 60s and is
# tunable via env if telemetry shows misfires.
FUEL_EDIT_LOCK_TTL_SECONDS = env.int("FUEL_EDIT_LOCK_TTL_SECONDS", default=60)

# ---------------------------------------------------------------------------
# Sentry — error & log & performance monitoring.
#
# Inert unless SENTRY_DSN is set AND we're not in a test run: with no DSN,
# sentry_sdk.init is skipped, so local dev and CI never phone home. Set
# SENTRY_DSN on the Azure Container App (sourced from Key Vault) to turn it on
# in production — no code change needed. The Django integration ships in the
# core SDK and is auto-enabled, so request/view/ORM errors are captured.
#
# PRIVACY: send_default_pii defaults False — load-bearing. The whole platform
# exists to keep user PII out of third parties (DeBERTa redactor, BYO body
# scrubbing). False means Sentry does NOT attach request bodies, cookies, user
# emails, or client IPs to events. Override SENTRY_SEND_DEFAULT_PII=true only as
# a deliberate decision. `before_send` / `before_send_log` are a second line of
# defense mirroring apps.byo_models.logging_filters.RedactBYOPasteBody — they
# scrub BYO request bodies out of both error events AND the logs stream.
SENTRY_DSN = env("SENTRY_DSN", default="")
SENTRY_ENVIRONMENT = env("SENTRY_ENVIRONMENT", default="production")
SENTRY_SEND_DEFAULT_PII = env.bool("SENTRY_SEND_DEFAULT_PII", default=False)
# Forward Python `logging` records to Sentry's Logs stream — the "check logs as
# things happen" surface. On by default; set SENTRY_ENABLE_LOGS=false to mute.
SENTRY_ENABLE_LOGS = env.bool("SENTRY_ENABLE_LOGS", default=True)
# Minimum level forwarded to the Logs stream. Default WARNING so the app's
# per-request INFO chatter (httpx calls, request logs, PERF lines) does NOT
# flood Sentry or burn log quota — only warnings and errors go through. Set
# ERROR for errors-only, or INFO to capture everything. (Errors are captured as
# Issues regardless of this setting.)
SENTRY_LOGS_LEVEL = env("SENTRY_LOGS_LEVEL", default="WARNING")
# Tracing + profiling are sampled and billed separately from errors. Default 1.0
# (capture everything) is fine at current traffic; dial down via env (e.g. 0.1)
# as volume grows — no redeploy needed.
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", default=1.0)
SENTRY_PROFILE_SESSION_SAMPLE_RATE = env.float("SENTRY_PROFILE_SESSION_SAMPLE_RATE", default=1.0)
# "trace" auto-runs the profiler whenever a transaction is active.
SENTRY_PROFILE_LIFECYCLE = env("SENTRY_PROFILE_LIFECYCLE", default="trace")
# Optional: tie events to a deploy so regressions point at a build. CI can pass
# the Django image SHA here.
SENTRY_RELEASE = env("SENTRY_RELEASE", default="")

# Public base URL of this control plane (e.g. the Container App FQDN). Used by
# the daily reconcile_system_crons task to (re)register QStash schedules
# against itself — the deploy pipeline sets it alongside SENTRY_RELEASE.
# Empty = reconcile task logs and skips (safe in dev/test).
DJANGO_BASE_URL = env("DJANGO_BASE_URL", default="")

# Never initialize Sentry during a test run. `make test` and CI both invoke
# `manage.py test` under DEV settings, so a SENTRY_DSN present in that
# environment would otherwise make the suite phone home (stray events + latency).
import sys  # noqa: E402

_SENTRY_RUNNING_TESTS = "test" in sys.argv or "pytest" in sys.modules

if SENTRY_DSN and not _SENTRY_RUNNING_TESTS:
    import logging as _logging
    import re as _re

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration as _LoggingIntegration

    _SENTRY_BYO_PATH = "/api/v1/tenants/byo-credentials/"
    _SENTRY_JSON_BLOCK = _re.compile(r"\{.*\}", _re.DOTALL)
    # Map SENTRY_LOGS_LEVEL ("WARNING"/"ERROR"/"INFO") to a logging int; unknown
    # → WARNING. Controls what the Logs stream receives (see SENTRY_LOGS_LEVEL).
    _sentry_logs_level = getattr(_logging, SENTRY_LOGS_LEVEL.upper(), _logging.WARNING)

    def _sentry_before_send(event, hint):
        """Backstop scrub: strip JSON-shaped bodies from log-message error events
        that touch the BYO paste endpoint, mirroring the console handler's filter."""
        try:
            logentry = event.get("logentry") or {}
            message = logentry.get("message")
            params = logentry.get("params")
            haystacks = [message]
            if isinstance(params, (list, tuple)):
                haystacks.extend(params)
            if any(isinstance(h, str) and _SENTRY_BYO_PATH in h for h in haystacks):
                if isinstance(message, str):
                    logentry["message"] = _SENTRY_JSON_BLOCK.sub("[REDACTED]", message)
                if isinstance(params, (list, tuple)):
                    scrubbed = [_SENTRY_JSON_BLOCK.sub("[REDACTED]", p) if isinstance(p, str) else p for p in params]
                    logentry["params"] = scrubbed if isinstance(params, list) else tuple(scrubbed)
                event["logentry"] = logentry
        except Exception:
            # Never let scrubbing raise inside Sentry's send path.
            pass
        return event

    def _sentry_before_send_log(log, hint):
        """Same BYO scrub for the Logs stream — enable_logs forwards `logging`
        records as structured logs, a path separate from error events."""
        try:
            body = log.get("body")
            attributes = log.get("attributes")
            attr_values = list(attributes.values()) if isinstance(attributes, dict) else []
            if any(isinstance(h, str) and _SENTRY_BYO_PATH in h for h in [body, *attr_values]):
                if isinstance(body, str):
                    log["body"] = _SENTRY_JSON_BLOCK.sub("[REDACTED]", body)
                if isinstance(attributes, dict):
                    for key, value in list(attributes.items()):
                        if isinstance(value, str):
                            attributes[key] = _SENTRY_JSON_BLOCK.sub("[REDACTED]", value)
        except Exception:
            pass
        return log

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        release=SENTRY_RELEASE or None,
        send_default_pii=SENTRY_SEND_DEFAULT_PII,
        # PRIVACY constraint — load-bearing: user content in stack-frame locals
        # (a chat/journal/message string in scope when an exception fires) must
        # NEVER reach Sentry. send_default_pii=False does NOT cover this — the
        # SDK captures frame local variables on error events by default. Disable
        # it, trading some debugging context for the guarantee that no user text
        # is exfiltrated via a traceback. Param name is for sentry-sdk 2.x
        # (`include_local_variables`); on <1.5 SDKs it was `with_locals`.
        include_local_variables=False,
        enable_logs=SENTRY_ENABLE_LOGS,
        # Configured LoggingIntegration: only WARNING+ reaches the Logs stream
        # (Django + other default integrations stay auto-enabled).
        integrations=[_LoggingIntegration(sentry_logs_level=_sentry_logs_level)],
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        profile_session_sample_rate=SENTRY_PROFILE_SESSION_SAMPLE_RATE,
        profile_lifecycle=SENTRY_PROFILE_LIFECYCLE,
        before_send=_sentry_before_send,
        before_send_log=_sentry_before_send_log,
    )

# Custom test runner — disconnects the CronJob → reconciler signal during
# test runs so the publish_task sync fallback (no QSTASH_TOKEN) doesn't
# accumulate DB connections + outbound HTTP attempts on every CronJob save.
# See ``config/test_runner.py`` for the full rationale.
TEST_RUNNER = "config.test_runner.QuietCronSignalRunner"
