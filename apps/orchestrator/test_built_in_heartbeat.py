"""Tests for the experimental built-in heartbeat path.

When ``tenant.experimental_built_in_heartbeat`` is True the generated
openclaw.json should:

  - emit ``agents.defaults.heartbeat`` with the every-1h built-in shape
    (target, model, activeHours)
  - emit ``commitments.enabled = true``
  - suppress ``_build_heartbeat_cron()`` so the cron-based heartbeat
    doesn't fire alongside (overlap → duplicate user-facing messages)

When the flag is False everything stays at today's defaults: heartbeat
is ``{"every": "0m"}``, commitments are disabled, and the cron-based
heartbeat keeps owning the user-facing check-in flow.
"""

from __future__ import annotations

from django.test import TestCase

from apps.orchestrator.config_generator import (
    _build_commitments_config,
    _build_heartbeat_cron,
    _build_heartbeat_defaults,
    generate_openclaw_config,
)
from apps.tenants.services import create_tenant


class BuiltInHeartbeatOffTest(TestCase):
    """Default path — flag off, today's behavior preserved."""

    def setUp(self):
        self.tenant = create_tenant(display_name="HeartbeatOff", telegram_chat_id=720001)
        # default for the field is False; assert it explicitly
        self.assertFalse(self.tenant.experimental_built_in_heartbeat)

    def test_heartbeat_defaults_disabled(self):
        self.assertEqual(_build_heartbeat_defaults(self.tenant), {"every": "0m"})

    def test_commitments_disabled(self):
        self.assertEqual(_build_commitments_config(self.tenant), {"enabled": False})

    def test_cron_heartbeat_still_fires_when_heartbeat_enabled(self):
        # heartbeat_enabled defaults True on Tenant
        self.assertTrue(self.tenant.heartbeat_enabled)
        cron = _build_heartbeat_cron(self.tenant)
        self.assertIsNotNone(cron)
        self.assertEqual(cron["name"], "Heartbeat Check-in")

    def test_full_config_shape(self):
        config = generate_openclaw_config(self.tenant)
        self.assertEqual(config["agents"]["defaults"]["heartbeat"], {"every": "0m"})
        self.assertEqual(config["commitments"], {"enabled": False})


class BuiltInHeartbeatOnTest(TestCase):
    """Canary path — flag on, commitments + every-1h heartbeat enabled."""

    def setUp(self):
        self.tenant = create_tenant(display_name="HeartbeatOn", telegram_chat_id=720002)
        self.tenant.experimental_built_in_heartbeat = True
        self.tenant.heartbeat_start_hour = 8
        self.tenant.heartbeat_window_hours = 6
        self.tenant.save()

    def test_heartbeat_defaults_built_in_shape(self):
        hb = _build_heartbeat_defaults(self.tenant)
        self.assertEqual(hb["every"], "1h")
        self.assertEqual(hb["target"], "last")
        self.assertEqual(hb["directPolicy"], "allow")
        self.assertTrue(hb["lightContext"])
        self.assertTrue(hb["isolatedSession"])
        self.assertTrue(hb["skipWhenBusy"])
        # model pinned to HEARTBEAT_MODEL so BYO Anthropic subs aren't billed
        from apps.orchestrator.config_generator import HEARTBEAT_MODEL

        self.assertEqual(hb["model"], HEARTBEAT_MODEL)
        # activeHours derived from tenant's existing fields
        self.assertEqual(hb["activeHours"]["start"], "08:00")
        self.assertEqual(hb["activeHours"]["end"], "14:00")
        self.assertIn("timezone", hb["activeHours"])

    def test_active_hours_wrap_around_midnight(self):
        self.tenant.heartbeat_start_hour = 22
        self.tenant.heartbeat_window_hours = 6
        self.tenant.save()
        hb = _build_heartbeat_defaults(self.tenant)
        # 22 + 6 = 28 → wraps to 04
        self.assertEqual(hb["activeHours"]["start"], "22:00")
        self.assertEqual(hb["activeHours"]["end"], "04:00")

    def test_commitments_enabled(self):
        self.assertEqual(
            _build_commitments_config(self.tenant),
            {"enabled": True, "maxPerDay": 3},
        )

    def test_cron_heartbeat_suppressed(self):
        """Built-in heartbeat owns delivery; cron heartbeat must not fire too."""
        self.assertTrue(self.tenant.heartbeat_enabled)  # the OLD flag stays on
        cron = _build_heartbeat_cron(self.tenant)
        self.assertIsNone(
            cron,
            "Cron heartbeat must return None when experimental_built_in_heartbeat is on — "
            "two heartbeats firing in the same activeHours window deliver overlapping "
            "messages to the user.",
        )

    def test_full_config_shape(self):
        config = generate_openclaw_config(self.tenant)
        hb = config["agents"]["defaults"]["heartbeat"]
        self.assertEqual(hb["every"], "1h")
        self.assertEqual(config["commitments"]["enabled"], True)
        self.assertEqual(config["commitments"]["maxPerDay"], 3)
