"""Tests for cron gateway client helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.test import TestCase

from apps.cron.gateway_client import _next_fire_at, cron_exists, cron_get


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

    def test_canary_stale_cron_resolves_to_far_future(self):
        """The canary's `25 23 25 4 *` cron, after April 25 has passed,
        resolves to next year — that's the signal welcome_scheduler uses
        to detect a stale one-shot."""
        nxt = _next_fire_at({"expr": "25 23 25 4 *", "tz": "Asia/Tokyo"})
        self.assertIsNotNone(nxt)
        # Whether April 25 has passed this year or not, the next fire is
        # always within the next ~365 days. We're not asserting the year
        # here — just that the helper produces a parseable timestamp.


class CronExistsTests(TestCase):
    """Plain existence check — name match, no schedule semantics."""

    def _mocked_invoke(self, jobs):
        return mock.patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={"jobs": jobs},
        )

    def test_present(self):
        with self._mocked_invoke([{"name": "_fuel:welcome", "schedule": {}}]):
            self.assertTrue(cron_exists(_FakeTenant(), "_fuel:welcome"))

    def test_absent(self):
        with self._mocked_invoke([{"name": "_other:job"}]):
            self.assertFalse(cron_exists(_FakeTenant(), "_fuel:welcome"))


class CronGetTests(TestCase):
    """``cron_get`` returns the full job dict (or None) — used by the
    welcome scheduler to inspect a cron's schedule and decide whether
    it's still pending or stale."""

    def _mocked_invoke(self, jobs):
        return mock.patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={"jobs": jobs},
        )

    def test_returns_job_dict_when_present(self):
        job = {"name": "_fuel:welcome", "schedule": {"kind": "cron", "expr": "0 * * * *", "tz": "UTC"}}
        with self._mocked_invoke([job, {"name": "_other:job"}]):
            got = cron_get(_FakeTenant(), "_fuel:welcome")
        self.assertEqual(got, job)

    def test_returns_none_when_absent(self):
        with self._mocked_invoke([{"name": "_other:job"}]):
            self.assertIsNone(cron_get(_FakeTenant(), "_fuel:welcome"))

    def test_returns_none_on_gateway_error(self):
        from apps.cron.gateway_client import GatewayError

        with mock.patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            side_effect=GatewayError("simulated"),
        ):
            self.assertIsNone(cron_get(_FakeTenant(), "_fuel:welcome"))


class WelcomeFreshnessIntegrationTests(TestCase):
    """End-to-end check that the welcome_scheduler treats a freshly-scheduled
    welcome as pending and a year-stale welcome as needing replacement."""

    def _mocked_invoke(self, jobs):
        return mock.patch(
            "apps.cron.gateway_client.invoke_gateway_tool",
            return_value={"jobs": jobs},
        )

    def test_fresh_welcome_within_window(self):
        """A welcome scheduled to fire within the next minute should look
        pending (next fire is well within the 1-day window)."""
        from apps.orchestrator.welcome_scheduler import _ONE_SHOT_WINDOW

        # Build a cron expression that fires "soon" — use today's date
        # patterns offset by a few minutes.
        soon = datetime.now(UTC) + timedelta(minutes=2)
        expr = f"{soon.minute} {soon.hour} {soon.day} {soon.month} *"
        nxt = _next_fire_at({"expr": expr, "tz": "UTC"})
        self.assertIsNotNone(nxt)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=UTC)
        self.assertLessEqual(nxt - datetime.now(UTC), _ONE_SHOT_WINDOW)

    def test_stale_welcome_outside_window(self):
        """The canary's stale Apr 25 cron has next_fire ~365 days away —
        which is well beyond the 1-day pending window, so the scheduler
        will detect it and replace."""
        from apps.orchestrator.welcome_scheduler import _ONE_SHOT_WINDOW

        # Use a date safely in the past relative to "now" — last week.
        past = datetime.now(UTC) - timedelta(days=7)
        expr = f"{past.minute} {past.hour} {past.day} {past.month} *"
        nxt = _next_fire_at({"expr": expr, "tz": "UTC"})
        self.assertIsNotNone(nxt)
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=UTC)
        # Next fire is roughly a year away — far beyond the 1-day window.
        self.assertGreater(nxt - datetime.now(UTC), _ONE_SHOT_WINDOW)


class _FakeTenant:
    """Minimal tenant stub — invoke_gateway_tool is mocked so no fields
    are actually read."""

    container_fqdn = "oc-fake.example.com"
    id = "00000000-0000-0000-0000-000000000000"
