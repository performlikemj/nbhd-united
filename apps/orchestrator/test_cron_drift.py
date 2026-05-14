"""Unit tests for the pure drift-detection helpers in ``cron_drift``.

These pin the precise contract used by ``regenerate_tenant_crons`` (and
the seed refresher) to decide whether two cron job dicts are converged.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.orchestrator.cron_drift import (
    job_drift,
    message_body_drift,
    payload_non_message_drift,
    schedule_drift,
    strip_date_line,
)


class StripDateLineTests(SimpleTestCase):
    def test_strips_preamble_followed_by_blank_line(self):
        msg = "Current date and time: 2026-05-14 09:00\n\nBriefing body."
        self.assertEqual(strip_date_line(msg), "Briefing body.")

    def test_leaves_other_prefixes_alone(self):
        msg = "Good morning!\n\nBody."
        self.assertEqual(strip_date_line(msg), msg)

    def test_handles_non_string(self):
        self.assertEqual(strip_date_line(None), "")
        self.assertEqual(strip_date_line(123), "")

    def test_no_blank_line_after_preamble_returns_input(self):
        msg = "Current date and time: 2026-05-14 09:00"
        self.assertEqual(strip_date_line(msg), msg)


class PayloadNonMessageDriftTests(SimpleTestCase):
    def test_model_in_payload_matches_no_drift(self):
        existing = {"payload": {"kind": "agentTurn", "model": "x", "message": "m"}}
        desired = {"payload": {"kind": "agentTurn", "model": "x", "message": "m"}}
        self.assertEqual(payload_non_message_drift(existing, desired), [])

    def test_stale_model_in_payload_detected(self):
        existing = {"payload": {"kind": "agentTurn", "model": "stale/cli", "message": "m"}}
        desired = {"payload": {"kind": "agentTurn", "message": "m"}}
        self.assertEqual(payload_non_message_drift(existing, desired), ["model"])

    def test_heartbeat_top_level_model_folds_correctly(self):
        """OC normalizes top-level ``model`` into ``payload.model`` on store.
        Desired side has ``model`` at top level; existing has it in payload.
        Both should resolve to the same value via ``_resolved_model``."""
        existing = {
            "payload": {"kind": "agentTurn", "model": "openrouter/x", "message": "m"},
        }
        desired = {
            "model": "openrouter/x",
            "payload": {"kind": "agentTurn", "message": "m"},
        }
        self.assertEqual(payload_non_message_drift(existing, desired), [])

    def test_kind_drift_detected(self):
        existing = {"payload": {"kind": "systemEvent", "message": "m"}}
        desired = {"payload": {"kind": "agentTurn", "message": "m"}}
        self.assertEqual(payload_non_message_drift(existing, desired), ["kind"])


class MessageBodyDriftTests(SimpleTestCase):
    def test_same_body_different_date_preamble_is_no_drift(self):
        existing = {"payload": {"message": "Current date and time: 2026-05-13\n\nBody"}}
        desired = {"payload": {"message": "Current date and time: 2026-05-14\n\nBody"}}
        self.assertFalse(message_body_drift(existing, desired))

    def test_different_body_after_preamble_is_drift(self):
        existing = {"payload": {"message": "Current date and time: 2026-05-13\n\nOld body"}}
        desired = {"payload": {"message": "Current date and time: 2026-05-14\n\nNew body"}}
        self.assertTrue(message_body_drift(existing, desired))


class ScheduleDriftTests(SimpleTestCase):
    def test_same_schedule_no_drift(self):
        existing = {"schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}}
        desired = {"schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}}
        self.assertEqual(schedule_drift(existing, desired), [])

    def test_tz_drift_detected(self):
        existing = {"schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}}
        desired = {"schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "Asia/Tokyo"}}
        self.assertEqual(schedule_drift(existing, desired), ["tz"])

    def test_expr_drift_detected(self):
        existing = {"schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"}}
        desired = {"schedule": {"kind": "cron", "expr": "0 8 * * *", "tz": "UTC"}}
        self.assertEqual(schedule_drift(existing, desired), ["expr"])


class JobDriftAggregateTests(SimpleTestCase):
    def test_no_drift_returns_empty(self):
        job = {
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
            "payload": {"kind": "agentTurn", "message": "body"},
            "enabled": True,
        }
        self.assertEqual(job_drift(job, dict(job)), [])

    def test_combined_drift(self):
        existing = {
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "UTC"},
            "payload": {"kind": "agentTurn", "model": "stale", "message": "old"},
            "enabled": True,
        }
        desired = {
            "schedule": {"kind": "cron", "expr": "0 7 * * *", "tz": "Asia/Tokyo"},
            "payload": {"kind": "agentTurn", "message": "new"},
            "enabled": False,
        }
        drift = set(job_drift(existing, desired))
        self.assertEqual(drift, {"model", "message", "schedule.tz", "enabled"})
