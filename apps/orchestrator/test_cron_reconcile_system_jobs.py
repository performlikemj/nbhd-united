"""OpenClaw 2026.9.4 gateway auto-provisions system-owned monitor crons
(heartbeat-<agent>, heartbeat-monitor-*, heartbeat-task:*,
skill-collection-review-<agent>) that cron clients cannot remove
("system-owned monitor jobs cannot be removed by cron clients"). The Django
reconciler must treat them as UNMANAGED so it doesn't error on every pass
trying to delete them (and burn its per-pass removal budget).
"""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.orchestrator.cron_reconcile import _is_unmanaged_cron


class SystemOwnedCronUnmanagedTest(SimpleTestCase):
    def test_9_4_system_crons_are_unmanaged(self):
        for name in (
            "heartbeat-main",
            "heartbeat-agent-abc123",
            "heartbeat-monitor-created",
            "heartbeat-task:xyz",
            "skill-collection-review-main",
        ):
            with self.subTest(name=name):
                self.assertTrue(_is_unmanaged_cron(name))
                self.assertTrue(_is_unmanaged_cron({"name": name}))
                # Even with a recurring schedule (not kind:at) they stay unmanaged.
                self.assertTrue(_is_unmanaged_cron({"name": name, "schedule": {"kind": "cron"}}))

    def test_our_managed_crons_stay_managed(self):
        # Our heartbeat cron is "Heartbeat Check-in" (capitalized, spaced) — must
        # NOT be shadowed by the lowercase-hyphen system heartbeat prefix.
        for name in ("Heartbeat Check-in", "Morning Briefing", "Evening Check-in"):
            with self.subTest(name=name):
                self.assertFalse(_is_unmanaged_cron(name))
                self.assertFalse(_is_unmanaged_cron({"name": name, "schedule": {"kind": "cron"}}))

    def test_existing_unmanaged_conventions_still_hold(self):
        self.assertTrue(_is_unmanaged_cron("_fuel:workout"))
        self.assertTrue(_is_unmanaged_cron("_sync:foo"))
        self.assertTrue(_is_unmanaged_cron({"name": "one-shot", "schedule": {"kind": "at"}}))
