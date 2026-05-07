"""Tests for cron gateway client helpers — freshness-aware cron_exists.

The freshness check is the durable invariant added in Phase 1.3 to
prevent the canary class of bug: a date-pattern one-shot cron whose
fire date already passed (because the agent failed to self-remove)
should not block re-scheduling.
"""

from __future__ import annotations

from datetime import UTC
from unittest import mock

from django.test import TestCase

from apps.cron.gateway_client import _next_fire_at, cron_exists


class NextFireAtTests(TestCase):
    def test_returns_future_for_recurring_expression(self):
        nxt = _next_fire_at({"kind": "cron", "expr": "* * * * *", "tz": "UTC"})
        self.assertIsNotNone(nxt)

    def test_handles_unparseable_expr(self):
        self.assertIsNone(_next_fire_at({"expr": "not-a-cron", "tz": "UTC"}))
        self.assertIsNone(_next_fire_at({}))
        self.assertIsNone(_next_fire_at({"expr": ""}))

    def test_falls_back_to_utc_for_invalid_tz(self):
        nxt = _next_fire_at({"expr": "0 * * * *", "tz": "Not/A/Real/Tz"})
        self.assertIsNotNone(nxt)


class CronExistsFreshnessTests(TestCase):
    """``require_future_fire=True`` hides crons whose next fire is in the past."""

    def _mocked_invoke(self, jobs):
        return mock.patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={"jobs": jobs},
        )

    def test_present_no_freshness_check(self):
        with self._mocked_invoke([{"name": "_fuel:welcome", "schedule": {}}]):
            self.assertTrue(cron_exists(_FakeTenant(), "_fuel:welcome"))

    def test_absent(self):
        with self._mocked_invoke([{"name": "_other:job"}]):
            self.assertFalse(cron_exists(_FakeTenant(), "_fuel:welcome"))

    def test_stale_recurring_blocks_without_freshness(self):
        """Without require_future_fire, the canary's stale Apr 25 cron looks present."""
        stale = {"name": "_fuel:welcome", "schedule": {"kind": "cron", "expr": "25 23 25 4 *", "tz": "Asia/Tokyo"}}
        with self._mocked_invoke([stale]):
            self.assertTrue(cron_exists(_FakeTenant(), "_fuel:welcome"))

    def test_stale_recurring_treated_absent_with_freshness(self):
        """With require_future_fire and a date already in the past for THIS year,
        the cron is treated as not-existing so the caller will replace it.

        The expression "25 23 25 4 *" fires April 25 23:25 — choose a day-of-year
        that's safely in the past during normal CI runs (early in the year).
        """
        # Use January 1st 00:00 — virtually always in the past relative to a
        # CI run that happens any time after that date in the same year.
        # We can't make this test deterministic across all dates, so we use
        # a freshly-computed past timestamp via a tiny shim.
        from datetime import datetime, timedelta

        past = datetime.now(UTC) - timedelta(days=2)
        # Build a date-pattern that points to that exact moment, this year.
        expr = f"{past.minute} {past.hour} {past.day} {past.month} *"
        stale = {"name": "_fuel:welcome", "schedule": {"kind": "cron", "expr": expr, "tz": "UTC"}}
        with self._mocked_invoke([stale]):
            self.assertFalse(cron_exists(_FakeTenant(), "_fuel:welcome", require_future_fire=True))

    def test_future_recurring_passes_freshness(self):
        """Standard hourly cron next fire is always in the future."""
        live = {"name": "_keep:alive", "schedule": {"kind": "cron", "expr": "0 * * * *", "tz": "UTC"}}
        with self._mocked_invoke([live]):
            self.assertTrue(cron_exists(_FakeTenant(), "_keep:alive", require_future_fire=True))


class _FakeTenant:
    """Minimal tenant stub — invoke_gateway_tool is mocked so no fields
    are actually read."""

    container_fqdn = "oc-fake.example.com"
    id = "00000000-0000-0000-0000-000000000000"
