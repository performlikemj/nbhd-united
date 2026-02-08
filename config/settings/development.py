"""Development settings."""
from .base import *  # noqa: F401,F403

DEBUG = True

# Allow all hosts in dev
ALLOWED_HOSTS = ["*"]

# Allow all CORS origins in dev (frontend on :3000 → backend on :8000)
CORS_ALLOW_ALL_ORIGINS = True

# Use console email backend
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Debug toolbar (install separately if needed)
# INSTALLED_APPS += ["debug_toolbar"]
# MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")
# INTERNAL_IPS = ["127.0.0.1"]
