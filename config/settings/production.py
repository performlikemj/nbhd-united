"""Production settings (Azure)."""
from .base import *  # noqa: F401,F403

DEBUG = False

# Security
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# CORS — production uses the explicit allowlist from CORS_ALLOWED_ORIGINS in base.py.
# Do NOT set CORS_ALLOW_ALL_ORIGINS here (that is dev-only).
