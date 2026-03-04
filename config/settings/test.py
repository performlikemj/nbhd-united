"""Test settings — SQLite-compatible by stubbing out pgvector migrations."""
from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
CORS_ALLOW_ALL_ORIGINS = True

# Stub out pgvector-dependent app migrations so SQLite can run tests.
# The app models are still loaded (to keep FK references working), but their
# tables use Django's in-memory schema instead of the pgvector migrations.
MIGRATION_MODULES = {
    "lessons": "apps.lessons.test_migrations",
    "journal": "apps.journal.test_migrations",
}
