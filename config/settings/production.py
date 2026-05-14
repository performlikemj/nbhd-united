"""Production settings (Azure)."""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

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
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["redact_byo_paste_body"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
