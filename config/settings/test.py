"""Test settings — SQLite-compatible, excludes pgvector apps."""
from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

# Remove pgvector-dependent apps so SQLite tests work
INSTALLED_APPS = [app for app in INSTALLED_APPS if app not in ("apps.lessons", "apps.journal")]  # noqa: F405

# Silence noisy migrations
MIGRATION_MODULES = {
    # Keep all standard migrations except the pgvector ones
}
