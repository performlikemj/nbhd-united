"""Loaded only by deploy/local-test/run.py with a clean generated environment."""

import os

from .development import *  # noqa: F403,F401

if os.environ.get("AZURE_MOCK") != "true":
    raise RuntimeError("The local test stack cannot use Azure")
LOCAL_TEST_ROOT = os.environ["LOCAL_TEST_ROOT"]
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
SAUTAI_M2M_BASE_URL = "http://127.0.0.1:8000"
SAUTAI_PLATFORM_SECRET = os.environ.get("SAUTAI_PLATFORM_SECRET", "")
ROOT_URLCONF = "deploy.local-test.urls"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "root": {"handlers": ["null"], "level": "CRITICAL"},
}
