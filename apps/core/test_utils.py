"""Shared isolation for compose tests under Django and pytest runners."""

from unittest.mock import patch

from apps.core import compose


class ComposeSchemaCacheMixin:
    """Keep schema fallback state within each test, including failed tests."""

    def setUp(self):
        super().setUp()
        cache = patch.object(compose, "_SCHEMA_REJECTED_MODELS", set())
        cache.start()
        self.addCleanup(cache.stop)
