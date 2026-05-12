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


class HibernateIdleImminentCronTests(TestCase):
    """The 2026-05-12 12:00 canary case: long-running awake tenant gets
    hibernated mid-evening-check-in because ``cron_wake_at`` was NULL
    (no recent wake-for-cron) and the in-flight cron was invisible to
    the backwards-looking guard. Forward-looking check via
    ``_cron_active_or_imminent`` must defer.
    """

    def _make_idle_tenant(self, *, suffix: int) -> Tenant:
        tenant = create_tenant(
            display_name=f"Imminent Cron {suffix}",
            telegram_chat_id=920_000_000 + suffix,
        )
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = f"oc-imm-{suffix}"
        tenant.container_fqdn = f"oc-imm-{suffix}.internal"
        tenant.last_message_at = timezone.now() - timedelta(hours=3)
        tenant.save()
        return tenant

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    @patch("apps.orchestrator.hibernation._cron_active_or_imminent", return_value="cron_in_flight")
    def test_defers_when_cron_in_flight(self, mock_defer, mock_hibernate):
        """A cron currently mid-execution (state.runningAtMs set) must
        not be killed by the hourly sweep.
        """
        self._make_idle_tenant(suffix=1)

        result = hibernate_idle_tenants_task()

        mock_hibernate.assert_not_called()
        self.assertEqual(result["hibernated"], 0)
        self.assertEqual(result["skipped_imminent_cron"], 1)

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    @patch("apps.orchestrator.hibernation._cron_active_or_imminent", return_value="cron_imminent")
    def test_defers_when_cron_imminent(self, mock_defer, mock_hibernate):
        """A cron scheduled to fire within the defer window must not be
        raced by hibernation — the cron timer would fire just as the
        revision deactivation lands.
        """
        self._make_idle_tenant(suffix=2)

        result = hibernate_idle_tenants_task()

        mock_hibernate.assert_not_called()
        self.assertEqual(result["hibernated"], 0)
        self.assertEqual(result["skipped_imminent_cron"], 1)

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    @patch("apps.orchestrator.hibernation._cron_active_or_imminent", return_value=None)
    def test_hibernates_when_no_cron_active_or_imminent(self, mock_defer, mock_hibernate):
        """Sanity: a fully idle tenant with no upcoming or in-flight cron
        still hibernates after passing the new check.
        """
        tenant = self._make_idle_tenant(suffix=3)

        result = hibernate_idle_tenants_task()

        mock_hibernate.assert_called_once_with(tenant)
        mock_defer.assert_called_once()
        self.assertEqual(result["hibernated"], 1)
        self.assertEqual(result["skipped_imminent_cron"], 0)

    @patch("apps.orchestrator.hibernation.hibernate_idle_tenant", return_value=True)
    @patch(
        "apps.orchestrator.hibernation._cron_active_or_imminent",
        side_effect=["cron_in_flight", None],
    )
    def test_one_defer_one_hibernate_independent(self, mock_defer, mock_hibernate):
        """Two idle tenants: one has an in-flight cron (defer), one
        doesn't (hibernate). Counters track each independently.
        """
        tenant_busy = self._make_idle_tenant(suffix=4)
        tenant_quiet = self._make_idle_tenant(suffix=5)

        result = hibernate_idle_tenants_task()

        self.assertEqual(result["hibernated"], 1)
        self.assertEqual(result["skipped_imminent_cron"], 1)
        # Hibernate was called exactly once — with whichever tenant the
        # mock returned None for. The order isn't guaranteed.
        self.assertEqual(mock_hibernate.call_count, 1)
        called_with = mock_hibernate.call_args[0][0]
        self.assertIn(called_with, {tenant_busy, tenant_quiet})


class CronActiveOrImminentTests(TestCase):
    """Unit tests for ``_cron_active_or_imminent`` itself."""

    def _make_tenant(self) -> Tenant:
        tenant = create_tenant(display_name="Cron Defer", telegram_chat_id=930_000_001)
        tenant.status = Tenant.Status.ACTIVE
        tenant.container_id = "oc-cron-defer"
        tenant.container_fqdn = "oc-cron-defer.internal"
        tenant.save()
        return tenant

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_returns_cron_in_flight_when_runningAtMs_set(self, mock_invoke):
        from apps.orchestrator.hibernation import _cron_active_or_imminent

        tenant = self._make_tenant()
        future_ms = int(timezone.now().timestamp() * 1000) + 7_200_000  # 2h out
        mock_invoke.return_value = {
            "details": {
                "jobs": [
                    {
                        "id": "j1",
                        "enabled": True,
                        "state": {
                            "runningAtMs": int(timezone.now().timestamp() * 1000) - 5_000,
                            "nextRunAtMs": future_ms,
                        },
                    }
                ]
            }
        }

        self.assertEqual(_cron_active_or_imminent(tenant), "cron_in_flight")

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_returns_cron_imminent_when_nextRunAtMs_within_window(self, mock_invoke):
        from apps.orchestrator.hibernation import _cron_active_or_imminent

        tenant = self._make_tenant()
        # Fire in 60s — well within the 300s default window.
        soon_ms = int(timezone.now().timestamp() * 1000) + 60_000
        mock_invoke.return_value = {
            "details": {"jobs": [{"id": "j1", "enabled": True, "state": {"nextRunAtMs": soon_ms}}]}
        }

        self.assertEqual(_cron_active_or_imminent(tenant), "cron_imminent")

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_returns_none_when_no_active_or_imminent(self, mock_invoke):
        from apps.orchestrator.hibernation import _cron_active_or_imminent

        tenant = self._make_tenant()
        # Next fire in 30 min — outside the 5-min default window.
        future_ms = int(timezone.now().timestamp() * 1000) + 1_800_000
        mock_invoke.return_value = {
            "details": {"jobs": [{"id": "j1", "enabled": True, "state": {"nextRunAtMs": future_ms}}]}
        }

        self.assertIsNone(_cron_active_or_imminent(tenant))

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_returns_none_when_gateway_unreachable(self, mock_invoke):
        """Conservative on failure: don't block the sweep if cron.list
        fails — the 2h idle cutoff and cron_wake_at are still backstops.
        """
        from apps.cron.gateway_client import GatewayError
        from apps.orchestrator.hibernation import _cron_active_or_imminent

        tenant = self._make_tenant()
        mock_invoke.side_effect = GatewayError("boom")

        self.assertIsNone(_cron_active_or_imminent(tenant))

    @patch("apps.cron.gateway_client.invoke_gateway_tool")
    def test_skips_disabled_jobs(self, mock_invoke):
        from apps.orchestrator.hibernation import _cron_active_or_imminent

        tenant = self._make_tenant()
        soon_ms = int(timezone.now().timestamp() * 1000) + 60_000
        mock_invoke.return_value = {
            "details": {
                "jobs": [
                    {
                        "id": "j1",
                        "enabled": False,  # disabled — should be ignored
                        "state": {
                            "runningAtMs": int(timezone.now().timestamp() * 1000) - 1_000,
                            "nextRunAtMs": soon_ms,
                        },
                    }
                ]
            }
        }

        self.assertIsNone(_cron_active_or_imminent(tenant))
