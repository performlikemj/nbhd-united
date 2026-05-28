"""Regression guards for templates/openclaw/docs/cron-management.md.

The doc told the agent for months to use `sessionTarget: "main" + payload.kind:
"agentTurn"` — a combination OC's runtime rejects at submit-time
(`jobs-DIMVdW2S.js:assertSupportedJobSpec`). Empirical canary testing on
2026-05-29 verified the correct shape is `isolated + agentTurn + delivery:
{mode: "none"}`. These tests pin the doc against drift back to the broken
pattern.
"""

from __future__ import annotations

import re

from django.test import SimpleTestCase

from apps.orchestrator.personas import _load_doc_template


class CronManagementDocShapeTests(SimpleTestCase):
    """The doc must prescribe the runtime-validated shape, not the broken one."""

    @classmethod
    def setUpTestData(cls):
        cls.doc = _load_doc_template("cron-management.md") or ""

    def test_doc_is_loaded(self):
        self.assertTrue(self.doc, "cron-management.md must load via _load_doc_template")

    def test_doc_does_not_prescribe_main_plus_agentturn(self):
        """The combo `sessionTarget: "main" + payload.kind: "agentTurn"` is
        runtime-rejected. The doc must not tell the agent to use it.
        """
        # Look for the broken pattern in any code-fenced example.
        broken_combos = re.findall(
            r'"sessionTarget"\s*:\s*"main".*?"kind"\s*:\s*"agentTurn"',
            self.doc,
            flags=re.DOTALL,
        )
        self.assertEqual(
            broken_combos,
            [],
            "cron-management.md prescribes `main + agentTurn` somewhere — "
            "this combo is rejected at submit-time by OC's runtime. Use "
            "`isolated + agentTurn` instead.",
        )

    def test_doc_prescribes_isolated_plus_agentturn(self):
        """The canonical shape must appear at least once."""
        self.assertIn('"sessionTarget": "isolated"', self.doc)
        self.assertIn('"kind": "agentTurn"', self.doc)

    def test_doc_calls_out_delivery_none_explicit(self):
        """Implicit announce default fails on this fleet — the doc must say
        `delivery: {"mode": "none"}` explicitly.
        """
        self.assertIn('"mode": "none"', self.doc)

    def test_doc_warns_against_announce(self):
        """The doc must warn that `mode: "announce"` is broken on this fleet."""
        self.assertRegex(
            self.doc,
            r"(?i)announce.*(?:broken|fail|reject|no telegram|bot token missing)",
            'cron-management.md must explain why `delivery.mode: "announce"` '
            "is unsafe on this fleet (OC has no built-in Telegram token; "
            "nbhd-telegram plugin handles outbound via nbhd_send_to_user).",
        )

    def test_doc_calls_out_main_session_quiet_hours_gate(self):
        """`sessionTarget: "main" + wakeMode: "now"` is heartbeat-gated and
        skips outside active hours. The doc must call this out so the agent
        does not reach for `main` when a user asks for an off-hours reminder.
        """
        self.assertRegex(
            self.doc,
            r"(?i)(quiet[- ]?hours|active[- ]?hours|heartbeat)",
            "cron-management.md must explain the heartbeat active-hours gate on `main + systemEvent` jobs.",
        )

    def test_doc_documents_main_requires_systemevent(self):
        """The runtime invariant `main REQUIRES systemEvent` must be in the doc
        so the agent doesn't try `main + agentTurn` from first principles.
        """
        self.assertRegex(
            self.doc,
            r"(?i)main.*(?:require|enforce).*systemEvent",
            "cron-management.md must document the `main REQUIRES payload.kind=systemEvent` invariant.",
        )

    def test_doc_documents_isolated_requires_agentturn(self):
        """Symmetric invariant: isolated/current/session REQUIRES agentTurn."""
        self.assertRegex(
            self.doc,
            r"(?i)(isolated|current|session).*(?:require|enforce).*agentTurn",
            "cron-management.md must document the `isolated/current/session "
            "REQUIRES payload.kind=agentTurn` invariant.",
        )

    def test_doc_requires_explicit_timezone_offset(self):
        """`schedule.at` without timezone is treated as UTC. The doc must say so."""
        self.assertRegex(
            self.doc,
            r"(?i)(timezone offset|with.*offset|treated as utc)",
            "cron-management.md must call out the UTC-default trap on naked ISO 8601 timestamps in `schedule.at`.",
        )

    def test_doc_notes_at_jobs_auto_delete_others_do_not(self):
        """One-off `kind:"at"` jobs auto-delete; recurring do not."""
        self.assertRegex(
            self.doc,
            r"(?i)kind.*at.*auto[- ]?delete",
            'cron-management.md must explain that `kind:"at"` jobs auto-delete but recurring jobs do not.',
        )
