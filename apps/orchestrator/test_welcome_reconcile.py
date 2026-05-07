"""Tests for the daily reconcile_welcomes watchdog task.

The watchdog is the third leg of welcome delivery (after the live
toggle path and the deploy backfill). Its job is to retry tenants
whose welcome was orphaned — e.g., the agent crashed mid-turn during
the original fire and never self-removed the cron, leaving the system
"convinced" a welcome is still pending when its fire date is in the
past. Phase 1.3.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from apps.orchestrator.tasks import reconcile_welcomes_task
from apps.orchestrator.welcome_scheduler import WelcomeStatus
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


def _make_active_tenant(*, suffix: int, fuel: bool = False, finance: bool = False):
    tenant = create_tenant(display_name=f"WelcomeReconcile-{suffix}", telegram_chat_id=901000 + suffix)
    tenant.status = Tenant.Status.ACTIVE
    tenant.container_id = "/some/container/id"
    tenant.container_fqdn = "oc-fake.example.com"
    tenant.fuel_enabled = fuel
    tenant.finance_enabled = finance
    tenant.save()
    return tenant


class ReconcileWelcomesTaskTests(TestCase):
    def test_walks_only_feature_enabled_tenants(self):
        _make_active_tenant(suffix=1, fuel=True, finance=False)
        _make_active_tenant(suffix=2, fuel=False, finance=True)
        _make_active_tenant(suffix=3, fuel=False, finance=False)  # not walked

        with (
            mock.patch(
                "apps.fuel.views._schedule_fuel_welcome",
                return_value=WelcomeStatus.SCHEDULED,
            ) as mock_fuel,
            mock.patch(
                "apps.finance.views._schedule_finance_welcome",
                return_value=WelcomeStatus.SCHEDULED,
            ) as mock_fin,
        ):
            totals = reconcile_welcomes_task()

        self.assertEqual(mock_fuel.call_count, 1)
        self.assertEqual(mock_fin.call_count, 1)
        self.assertEqual(totals["fuel"], {"scheduled": 1})
        self.assertEqual(totals["finance"], {"scheduled": 1})

    def test_counts_failures_per_feature(self):
        """A scheduler exception is tallied, not propagated — one bad
        tenant must not abort the fleet sweep."""
        _make_active_tenant(suffix=4, fuel=True)
        _make_active_tenant(suffix=5, fuel=True)

        with mock.patch(
            "apps.fuel.views._schedule_fuel_welcome",
            side_effect=[WelcomeStatus.SCHEDULED, RuntimeError("simulated gateway down")],
        ):
            totals = reconcile_welcomes_task()

        # Both tenants visited; one scheduled, one failed.
        self.assertEqual(totals["fuel"].get("scheduled", 0), 1)
        self.assertEqual(totals["fuel"].get("failed", 0), 1)

    def test_distinguishes_status_categories(self):
        _make_active_tenant(suffix=6, fuel=True, finance=True)

        with (
            mock.patch(
                "apps.fuel.views._schedule_fuel_welcome",
                return_value=WelcomeStatus.REPLACED_STALE,
            ),
            mock.patch(
                "apps.finance.views._schedule_finance_welcome",
                return_value=WelcomeStatus.SKIPPED_ALREADY_DELIVERED,
            ),
        ):
            totals = reconcile_welcomes_task()

        self.assertEqual(totals["fuel"], {"replaced_stale": 1})
        self.assertEqual(totals["finance"], {"skipped_already_delivered": 1})
