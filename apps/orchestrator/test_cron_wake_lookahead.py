"""Tests for cron-wake look-ahead re-hibernation.

When a cron-wake idle check fires and another cron is due soon,
``check_cron_wake_idle_task`` should defer re-hibernation rather than
force the next cron through a cold-start. Cron output never updates
``last_message_at`` (only inbound user messages do — see
``apps/router/wake_on_message.py:58``), so the original 30-min idle
gate would always re-hibernate even when a 7am+8:30am pattern was
about to fire its second cron.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.cron.gateway_client import GatewayError
from apps.orchestrator import hibernation
from apps.orchestrator.hibernation import (
    _CRON_WAKE_IDLE_SECONDS,
    _next_cron_within_window,
    check_cron_wake_idle_task,
)
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant


class _Base(TestCase):
    """Shared fixture: a cron-woken tenant past the idle gate, no user activity."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Lookahead", telegram_chat_id=950200)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.container_id = "oc-lookahead"
        self.tenant.container_fqdn = "oc-lookahead.internal"
        self.tenant.hibernated_at = None  # awake from cron wake
        # cron_wake_at is in the past, last_message_at hasn't moved past it →
        # the existing idle gate would re-hibernate.
        self.tenant.cron_wake_at = timezone.now() - timedelta(minutes=30)
        self.tenant.last_message_at = timezone.now() - timedelta(hours=4)
        self.tenant.save()

    @staticmethod
    def _job(*, name: str, next_run_ms: int, enabled: bool = True) -> dict:
        """Job in the gateway's real shape — runtime state nested under ``state``."""
        return {
            "id": f"id-{name}",
            "name": name,
            "enabled": enabled,
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
            "state": {"nextRunAtMs": next_run_ms},
        }


class NextCronWithinWindowTests(_Base):
    """Direct tests of the ``_next_cron_within_window`` helper."""

    def test_live_gateway_used_when_reachable(self):
        in_30min_ms = int(timezone.now().timestamp() * 1000) + 30 * 60 * 1000
        live_jobs = [self._job(name="Project Check-in", next_run_ms=in_30min_ms)]

        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={"jobs": live_jobs},
        ) as mock_invoke:
            result = _next_cron_within_window(self.tenant, window_seconds=5400)

        self.assertEqual(result, in_30min_ms)
        mock_invoke.assert_called_once_with(
            self.tenant, "cron.list", {"includeDisabled": False}
        )

    def test_snapshot_fallback_when_gateway_fails(self):
        in_45min_ms = int(timezone.now().timestamp() * 1000) + 45 * 60 * 1000
        snapshot_jobs = [self._job(name="Heartbeat", next_run_ms=in_45min_ms)]
        self.tenant.cron_jobs_snapshot = {
            "jobs": snapshot_jobs,
            "snapshot_at": timezone.now().isoformat(),
        }
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            side_effect=GatewayError("502: bad_gateway", status_code=502),
        ):
            result = _next_cron_within_window(self.tenant, window_seconds=5400)

        self.assertEqual(result, in_45min_ms)

    def test_no_data_anywhere_returns_none(self):
        self.tenant.cron_jobs_snapshot = {}
        self.tenant.save(update_fields=["cron_jobs_snapshot"])

        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            side_effect=GatewayError("502: bad_gateway", status_code=502),
        ):
            result = _next_cron_within_window(self.tenant, window_seconds=5400)

        self.assertIsNone(result)

    def test_cron_outside_window_returns_none(self):
        # Fires 3 hours from now — outside a 90-min window.
        far_future_ms = int(timezone.now().timestamp() * 1000) + 3 * 3600 * 1000
        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={"jobs": [self._job(name="Far", next_run_ms=far_future_ms)]},
        ):
            result = _next_cron_within_window(self.tenant, window_seconds=5400)
        self.assertIsNone(result)

    def test_disabled_cron_in_window_ignored(self):
        in_30min_ms = int(timezone.now().timestamp() * 1000) + 30 * 60 * 1000
        with patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={
                "jobs": [self._job(name="Disabled", next_run_ms=in_30min_ms, enabled=False)]
            },
        ):
            result = _next_cron_within_window(self.tenant, window_seconds=5400)
        self.assertIsNone(result)


class CheckCronWakeIdleLookAheadTests(_Base):
    """Integration tests of ``check_cron_wake_idle_task`` with the look-ahead step."""

    def test_no_cron_in_window_rehibernates(self):
        with (
            patch.object(hibernation, "_next_cron_within_window", return_value=None),
            patch.object(hibernation, "hibernate_idle_tenant", return_value=True) as mock_hibernate,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            result = check_cron_wake_idle_task(str(self.tenant.id))

        self.assertEqual(result["status"], "re_hibernated")
        mock_hibernate.assert_called_once()
        # No follow-up check scheduled — we hibernated.
        for call in mock_publish.call_args_list:
            self.assertNotEqual(call.args[0], "check_cron_wake_idle")

    def test_cron_within_window_defers_with_correct_delay(self):
        # Next cron in 40 min — well within 90-min window.
        now_ms = int(timezone.now().timestamp() * 1000)
        in_40min_ms = now_ms + 40 * 60 * 1000

        with (
            patch.object(hibernation, "_next_cron_within_window", return_value=in_40min_ms),
            patch.object(hibernation, "hibernate_idle_tenant") as mock_hibernate,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            result = check_cron_wake_idle_task(str(self.tenant.id))

        self.assertEqual(result["status"], "deferred_for_upcoming_cron")
        # Hibernation NOT called.
        mock_hibernate.assert_not_called()
        # New idle check scheduled ~ (40 min + 30 min idle window) from now.
        mock_publish.assert_called_once()
        args, kwargs = mock_publish.call_args
        self.assertEqual(args[0], "check_cron_wake_idle")
        self.assertEqual(args[1], str(self.tenant.id))
        expected_delay = 40 * 60 + _CRON_WAKE_IDLE_SECONDS
        self.assertAlmostEqual(kwargs["delay_seconds"], expected_delay, delta=5)

    def test_user_active_path_short_circuits_before_lookahead(self):
        # User messaged after the cron wake → existing user_active path.
        # Look-ahead must NOT be consulted (regression guard for ordering).
        self.tenant.last_message_at = timezone.now() - timedelta(minutes=5)
        self.tenant.save(update_fields=["last_message_at"])

        with (
            patch.object(hibernation, "_next_cron_within_window") as mock_lookahead,
            patch.object(hibernation, "hibernate_idle_tenant") as mock_hibernate,
        ):
            result = check_cron_wake_idle_task(str(self.tenant.id))

        self.assertEqual(result["status"], "user_active")
        mock_lookahead.assert_not_called()
        mock_hibernate.assert_not_called()
