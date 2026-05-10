"""Regression coverage for the hibernate-idle vs wake-for-cron race.

The bug (canary 2026-05-11 07:00 JST):
``hibernate_idle_tenants_task`` runs at minute 0 of every hour. The
canary's morning briefing is also scheduled at minute 0 (07:00 user
local). ``wake_for_cron_task`` had brought the container up at :56,
set ``cron_wake_at = now()``, and scheduled the morning briefing to
fire at :00. At :00, ``hibernate_idle_tenants_task`` saw
``last_message_at`` > 2h ago and (without checking ``cron_wake_at``)
hibernated the container — Azure terminated the container at :00:47
with reason ``ManuallyStopped``, killing the about-to-fire briefing.

The cron-wake re-hibernation lifecycle is owned by
``check_cron_wake_idle_task`` (which knows about upcoming crons via
``_next_cron_within_window`` and decides correctly). The hourly idle
sweep should defer to it while ``cron_wake_at`` is fresh.

These tests pin: hibernate_idle_tenants_task skips tenants whose
cron_wake_at is recent, but still hibernates when cron_wake_at is
NULL (normal idle case) or stale (>2h, defensive fallback if a
check_cron_wake_idle ever gets dropped).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.orchestrator.tasks import hibernate_idle_tenants_task
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class HibernateIdleCronWakeRaceTests(TestCase):
    """Pin: hibernate_idle_tenants must not race with wake_for_cron."""

    def _make_idle_tenant(self, *, suffix: int) -> Tenant:
        tenant = create_tenant(
            display_name=f"Idle Cron Race {suffix}",
            telegram_chat_id=900_000_000 + suffix,
        )
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = f"oc-race-{suffix}"
        tenant.container_fqdn = f"oc-race-{suffix}.internal"
        tenant.last_message_at = timezone.now() - timedelta(hours=3)
        tenant.save()
        return tenant

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    def test_skips_tenant_with_recent_cron_wake(self, mock_hibernate):
        """The exact 2026-05-11 canary scenario in test form.

        Tenant was woken for cron 5 min ago. Idle for 3h otherwise. Hourly
        sweep must NOT hibernate — check_cron_wake_idle owns the lifecycle.
        """
        tenant = self._make_idle_tenant(suffix=1)
        tenant.cron_wake_at = timezone.now() - timedelta(minutes=5)
        tenant.save(update_fields=["cron_wake_at"])

        result = hibernate_idle_tenants_task()

        mock_hibernate.assert_not_called()
        self.assertEqual(result["hibernated"], 0)

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    def test_hibernates_when_cron_wake_at_is_null(self, mock_hibernate):
        """Sanity: a normally-idle tenant (no cron wake in flight) still hibernates."""
        tenant = self._make_idle_tenant(suffix=2)
        # cron_wake_at left as NULL (default)
        self.assertIsNone(tenant.cron_wake_at)

        result = hibernate_idle_tenants_task()

        mock_hibernate.assert_called_once_with(tenant)
        self.assertEqual(result["hibernated"], 1)

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    def test_hibernates_when_cron_wake_at_is_stale(self, mock_hibernate):
        """Defensive: if check_cron_wake_idle got dropped (cron_wake_at stuck
        at >2h old), the hourly sweep still reclaims the container.
        """
        tenant = self._make_idle_tenant(suffix=3)
        tenant.cron_wake_at = timezone.now() - timedelta(hours=4)
        tenant.save(update_fields=["cron_wake_at"])

        result = hibernate_idle_tenants_task()

        mock_hibernate.assert_called_once_with(tenant)
        self.assertEqual(result["hibernated"], 1)

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    def test_toctou_race_when_cron_wake_arrives_mid_task(self, mock_hibernate):
        """If wake_for_cron lands AFTER the queryset eval but BEFORE the
        per-tenant refresh, the in-loop check still catches it.

        Simulates the race by mocking the per-tenant refresh to update
        cron_wake_at, mimicking a wake_for_cron_task that fires during
        this task's execution.
        """
        tenant = self._make_idle_tenant(suffix=4)
        # Tenant qualifies at queryset time (cron_wake_at NULL).

        original_refresh = Tenant.refresh_from_db
        race_state = {"applied": False}

        def racing_refresh(self_, fields=None):
            # First refresh call: simulate wake_for_cron landing now.
            if not race_state["applied"]:
                race_state["applied"] = True
                Tenant.objects.filter(id=self_.id).update(
                    cron_wake_at=timezone.now() - timedelta(seconds=10),
                )
            return original_refresh(self_, fields=fields)

        with patch.object(Tenant, "refresh_from_db", racing_refresh):
            result = hibernate_idle_tenants_task()

        mock_hibernate.assert_not_called()
        self.assertEqual(result["hibernated"], 0)
        self.assertEqual(result["skipped_cron_wake"], 1)
