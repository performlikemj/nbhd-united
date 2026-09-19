"""Offline regression tests on the dedicated Compose PostgreSQL only."""

from .local_test import *  # noqa: F401,F403

LOCAL_TEST_ROOT = ""
ROOT_URLCONF = "config.urls"
OPENCLAW_USAGE_PLUGIN_ID = "nbhd-usage-reporter"
