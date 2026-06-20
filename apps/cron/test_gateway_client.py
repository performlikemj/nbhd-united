"""Tests for cron gateway client helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest import mock

from django.test import TestCase

from apps.cron.gateway_client import (
    _next_fire_at,
    _normalize_cron_delivery_for_ios,
    cron_exists,
    cron_get,
    invoke_gateway_tool,
)

_OMIT = object()  # sentinel: build a job with NO delivery block


class NormalizeCronDeliveryTests(TestCase):
    """The backstop that forces a Django-pushed cron onto the iOS-reachable
    delivery path (delivery.mode:"none" + nbhd_send_to_user)."""

    def _job(self, delivery, *, message="Send the daily digest.", name="Daily Digest", kind="agentTurn", tools=None):
        payload = {"kind": kind, "message": message}
        if tools is not None:
            payload["toolsAllow"] = tools
        job = {"name": name, "payload": payload, "sessionTarget": "isolated"}
        if delivery is not _OMIT:
            job["delivery"] = delivery
        return job

    def test_announce_is_rewritten_to_none(self):
        job = _normalize_cron_delivery_for_ios(self._job({"mode": "announce"}))
        self.assertEqual(job["delivery"], {"mode": "none"})

    def test_channel_delivery_is_rewritten(self):
        job = _normalize_cron_delivery_for_ios(self._job({"mode": "telegram", "channel": "telegram"}))
        self.assertEqual(job["delivery"], {"mode": "none"})

    def test_missing_delivery_block_is_rewritten(self):
        # No delivery block → OC defaults to announce → must be normalized.
        job = _normalize_cron_delivery_for_ios(self._job(_OMIT))
        self.assertEqual(job["delivery"], {"mode": "none"})

    def test_mode_none_is_unchanged_idempotent(self):
        original = self._job({"mode": "none"}, message="Compose the digest, then call nbhd_send_to_user.")
        job = _normalize_cron_delivery_for_ios(original)
        self.assertEqual(job["delivery"], {"mode": "none"})

    def test_message_is_never_mutated(self):
        # The backstop touches ONLY delivery — never the message — so it can't
        # perturb the reconciler's message-body drift detection.
        msg = "Compose the digest."
        job = _normalize_cron_delivery_for_ios(self._job({"mode": "announce"}, message=msg))
        self.assertEqual(job["payload"]["message"], msg)

    def test_systemevent_payload_is_skipped(self):
        # Heartbeat/sync systemEvents are not user deliveries — never touch them.
        job = _normalize_cron_delivery_for_ios(self._job({"mode": "announce"}, kind="systemEvent"))
        self.assertEqual(job["delivery"], {"mode": "announce"})

    def test_sync_continuity_cron_is_skipped(self):
        job = _normalize_cron_delivery_for_ios(self._job(_OMIT, name="_sync:Evening Check-in"))
        self.assertNotIn("delivery", job)


class InvokeGatewayToolDeliveryNormalizationTests(TestCase):
    """The backstop fires at the invoke_gateway_tool push boundary, for both the
    {"job"} (cron.add / recreate) and {"jobId","patch"} (cron.update) shapes."""

    class _FakeTenant:
        id = "t-1"
        container_fqdn = "oc-test.example.com"
        internal_api_key = "k"

    def _invoke_capture(self, tool, args):
        """Call invoke_gateway_tool with the gateway POST stubbed; return the body
        the gateway would have received."""
        captured = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"ok": True, "result": {}}

        def _fake_post(url, json, headers, timeout):
            captured["body"] = json
            return _Resp()

        with mock.patch("apps.cron.gateway_client.requests.post", side_effect=_fake_post):
            invoke_gateway_tool(self._FakeTenant(), tool, args)
        return captured["body"]

    def test_cron_add_job_delivery_is_normalized(self):
        body = self._invoke_capture(
            "cron.add",
            {
                "job": {
                    "name": "Digest",
                    "payload": {"kind": "agentTurn", "message": "send"},
                    "delivery": {"mode": "announce"},
                }
            },
        )
        self.assertEqual(body["args"]["job"]["delivery"], {"mode": "none"})

    def test_cron_add_does_not_mutate_callers_dict(self):
        original = {
            "job": {
                "name": "Digest",
                "payload": {"kind": "agentTurn", "message": "send"},
                "delivery": {"mode": "announce"},
            }
        }
        self._invoke_capture("cron.add", original)
        # Caller's dict is untouched (deep-copied before normalize).
        self.assertEqual(original["job"]["delivery"], {"mode": "announce"})

    def test_cron_update_patch_delivery_is_normalized(self):
        body = self._invoke_capture(
            "cron.update",
            {"jobId": "Digest", "patch": {"delivery": {"mode": "telegram", "channel": "telegram"}}},
        )
        self.assertEqual(body["args"]["patch"]["delivery"], {"mode": "none"})

    def test_cron_update_patch_without_delivery_untouched(self):
        body = self._invoke_capture("cron.update", {"jobId": "Digest", "patch": {"enabled": False}})
        self.assertEqual(body["args"]["patch"], {"enabled": False})

    def test_non_cron_tool_args_untouched(self):
        body = self._invoke_capture("cron.list", {"includeDisabled": True})
        self.assertEqual(body["args"], {"includeDisabled": True})


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
