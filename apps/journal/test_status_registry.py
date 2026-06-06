"""Tests for the Journal status-provider registry.

The registry is what guarantees a new feature is never left out of the
current-status snapshot the proactive/cron layer grounds on: features register
a provider, and ``build_journal_status`` returns the union of every enabled
provider. These tests pin that the built-in domains stay registered, that a
freshly-registered provider is auto-included with no change to the assembler,
that disabled providers contribute nothing, and that a failing provider is
isolated rather than breaking the whole snapshot.
"""

from __future__ import annotations

from datetime import date

from django.test import TestCase

# Importing build_journal_status registers the built-in providers as a side effect.
from apps.journal.status_projection import build_journal_status
from apps.journal.status_registry import (
    register_status_provider,
    registered_keys,
    status_providers,
    unregister_status_provider,
)
from apps.tenants.models import Tenant, User


class StatusRegistryTests(TestCase):
    def _tenant(self, username: str) -> Tenant:
        user = User.objects.create_user(username=username, password="x")
        return Tenant.objects.create(user=user, status="active")

    def test_builtin_domains_registered(self):
        self.assertLessEqual({"tasks", "goals", "finance"}, registered_keys())

    def test_providers_satisfy_contract(self):
        for provider in status_providers():
            self.assertIsInstance(provider.key, str)
            self.assertTrue(callable(provider.enabled))
            self.assertTrue(callable(provider.provide))

    def test_new_provider_is_auto_included(self):
        """A feature that registers a provider appears in the snapshot with no
        change to build_journal_status — the guarantee that features are not
        left behind."""
        tenant = self._tenant("reg_new")
        register_status_provider(
            "widgets",
            enabled=lambda t: True,
            provide=lambda t, today: {"widgets": [{"title": "shiny"}]},
        )
        self.addCleanup(unregister_status_provider, "widgets")

        result = build_journal_status(tenant, date(2026, 6, 6))

        self.assertEqual(result["widgets"], [{"title": "shiny"}])

    def test_disabled_provider_is_skipped(self):
        tenant = self._tenant("reg_disabled")
        register_status_provider(
            "hidden",
            enabled=lambda t: False,
            provide=lambda t, today: {"hidden": ["nope"]},
        )
        self.addCleanup(unregister_status_provider, "hidden")

        result = build_journal_status(tenant, date(2026, 6, 6))

        self.assertNotIn("hidden", result)

    def test_failing_provider_is_isolated(self):
        tenant = self._tenant("reg_flaky")

        def boom(t, today):
            raise RuntimeError("kaboom")

        register_status_provider("flaky", enabled=lambda t: True, provide=boom)
        self.addCleanup(unregister_status_provider, "flaky")

        result = build_journal_status(tenant, date(2026, 6, 6))

        self.assertIn("flaky", result.get("unavailable", []))
        self.assertIn("open_tasks", result)  # rest of the snapshot still returns
