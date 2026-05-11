"""Tests for log-leakage redaction.

Two layers under test:

  1. ``_build_logging_config()`` — patterns injected into ``openclaw.json``
     so OpenClaw's upstream ``redactToolDetail`` masks ``"message":`` etc.
     in the ``[tools] X failed: ... raw_params={...}`` lines (which go
     to stderr via ``logError → runtime.error → console.error``).

  2. Django subprocess output logs — ``apps/cron/views.py`` and
     ``apps/lessons/tasks.py`` previously dumped Django management
     command stdout (which contains lesson text from user daily notes)
     into ``logger.info`` calls and JSON response bodies.

Layer-3 (the ``runtime/openclaw/redact-stdout.js`` sidecar) has its
own standalone Node test alongside the source file — it can't be
exercised from Django's test runner since it monkey-patches the
node process streams.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.tenants.services import create_tenant

from .config_generator import _build_logging_config, generate_openclaw_config


class BuildLoggingConfigTest(TestCase):
    """``_build_logging_config()`` shape and pattern correctness."""

    def test_shape(self):
        cfg = _build_logging_config()
        self.assertEqual(cfg["redactSensitive"], "tools")
        self.assertIsInstance(cfg["redactPatterns"], list)
        self.assertGreater(len(cfg["redactPatterns"]), 0)

    def test_patterns_are_valid_regex(self):
        """OpenClaw compiles each pattern via compileSafeRegex — ensure
        they're at least valid Python regex too (catches typos before
        deploy)."""
        cfg = _build_logging_config()
        for pattern in cfg["redactPatterns"]:
            try:
                re.compile(pattern)
            except re.error as exc:
                self.fail(f"redactPattern invalid regex: {pattern!r} — {exc}")

    def test_content_field_pattern_matches_canary_leak_shape(self):
        """The actual leaked shape captured on the canary 2026-05-11."""
        cfg = _build_logging_config()
        leak = (
            "[tools] cron failed: invalid cron.add params: delivery.channel "
            "is required when multiple channels are configured: line, "
            'telegram raw_params={"action":"add","job":{"enabled":true,'
            '"name":"Record SCORM Cloud Demo","schedule":{"kind":"at",'
            '"at":"2026-05-11T07:38:03.023Z"},"payload":{"kind":"agentTurn",'
            '"message":"Reminder: record your SCORM Cloud demo"}}}'
        )
        # The first pattern is the content-field pattern.
        content_pattern = re.compile(cfg["redactPatterns"][0])
        match = content_pattern.search(leak)
        self.assertIsNotNone(
            match,
            "First redactPattern did not match the canary leak shape — "
            'JSON `"message":"..."` inside raw_params should be caught.',
        )

    def test_bearer_token_pattern_matches(self):
        """One of the upstream auth defaults we mirror."""
        cfg = _build_logging_config()
        candidate = "Authorization: Bearer abc123def456ghi789jkl"
        any_match = any(re.search(p, candidate) for p in cfg["redactPatterns"])
        self.assertTrue(any_match, "Auth Bearer pattern missing from config")


class GenerateOpenclawConfigIncludesLoggingTest(TestCase):
    """Layer-1 redaction must reach the generated ``openclaw.json``."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Redaction Test",
            telegram_chat_id=999000111,
        )

    def test_logging_block_present(self):
        config = generate_openclaw_config(self.tenant)
        self.assertIn("logging", config)
        self.assertIn("redactPatterns", config["logging"])
        self.assertIn("redactSensitive", config["logging"])

    def test_redact_sensitive_is_tools(self):
        config = generate_openclaw_config(self.tenant)
        self.assertEqual(config["logging"]["redactSensitive"], "tools")

    def test_logging_block_is_stable_across_tenants(self):
        """Two unrelated tenants get the same logging block — no tenant-
        identifying data sneaking into the redaction config."""
        t2 = create_tenant(display_name="Other", telegram_chat_id=999000222)
        cfg1 = generate_openclaw_config(self.tenant)
        cfg2 = generate_openclaw_config(t2)
        self.assertEqual(cfg1["logging"], cfg2["logging"])


class DjangoSubprocessLogRedactionTest(TestCase):
    """``apps/cron/views.py`` and ``apps/lessons/tasks.py`` must not dump
    subprocess stdout (which contains lesson text from user daily notes)
    to the logger or response body."""

    @patch("django.core.management.call_command")
    @patch("apps.tenants.middleware.set_rls_context")
    @override_settings(DEPLOY_SECRET="test-secret")
    def test_rewrite_lessons_actionable_does_not_log_or_return_content(self, _mock_rls, mock_call):
        """The endpoint must summarize, not echo, the management command
        stdout. Real `rewrite_lessons_actionable` writes
        `[lesson_id] BEFORE: {lesson.text}` lines — those would leak
        daily-note-derived content into Log Analytics."""
        from django.test import RequestFactory

        from apps.cron.views import run_rewrite_lessons_actionable

        def fake_call(_name, stdout):
            stdout.write("Found 3 approved lessons to rewrite\n")
            stdout.write("  [abc-123] BEFORE: I learned to drink more water after that marathon training month\n")
            stdout.write("  [abc-123] AFTER:  Drink 3L water per training day\n")
            stdout.write("  Rewrote lesson abc-123\n")

        mock_call.side_effect = fake_call

        rf = RequestFactory()
        req = rf.post(
            "/api/cron/run-rewrite-lessons-actionable/",
            HTTP_X_DEPLOY_SECRET="test-secret",
        )

        with self.assertLogs("apps.cron.views", level="INFO") as captured:
            resp = run_rewrite_lessons_actionable(req)

        self.assertEqual(resp.status_code, 200)

        # Response body must not echo the user content.
        body = resp.content.decode()
        self.assertNotIn("marathon training", body)
        self.assertNotIn("drink more water", body)
        self.assertNotIn("BEFORE:", body)
        # And must report the safe summary.
        self.assertIn('"output_bytes"', body)

        # Logger output must not echo the user content either.
        joined_logs = "\n".join(captured.output)
        self.assertNotIn("marathon training", joined_logs)
        self.assertNotIn("BEFORE:", joined_logs)
        self.assertIn("rewrite_lessons_actionable: completed", joined_logs)

    @patch("django.core.management.call_command")
    @patch("apps.tenants.middleware.set_rls_context")
    @override_settings(DEPLOY_SECRET="test-secret")
    def test_reseed_lessons_does_not_log_or_return_content(self, _mock_rls, mock_call):
        from django.test import RequestFactory

        from apps.cron.views import run_reseed_lessons

        def fake_call(_name, stdout):
            stdout.write("Processing 1 tenant(s)\n")
            stdout.write("Tenant 12345678\n")
            # Real reseed_lessons can echo daily-note fragments via the
            # extraction path; simulate with a recognisable string.
            stdout.write("  Extracted lesson: bought sourdough starter from MJ\n")

        mock_call.side_effect = fake_call

        rf = RequestFactory()
        req = rf.post(
            "/api/cron/run-reseed-lessons/",
            HTTP_X_DEPLOY_SECRET="test-secret",
        )

        with self.assertLogs("apps.cron.views", level="INFO") as captured:
            resp = run_reseed_lessons(req)

        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertNotIn("sourdough", body)
        self.assertNotIn("Extracted lesson", body)
        self.assertIn('"output_bytes"', body)

        joined_logs = "\n".join(captured.output)
        self.assertNotIn("sourdough", joined_logs)
        self.assertIn("reseed_lessons: completed", joined_logs)

    @patch("django.core.management.call_command")
    def test_dedup_lessons_task_does_not_log_or_return_content(self, mock_call):
        from apps.lessons.tasks import dedup_lessons_task

        def fake_call(_name, stdout):
            stdout.write("  [tenant1] 12 lessons\n")
            stdout.write("\n  [tenant1] Duplicate group (keeping: I should swim before legs because)\n")
            stdout.write("    REMOVE: I learned swimming first helps my squats (sim=0.892)\n")

        mock_call.side_effect = fake_call

        with self.assertLogs("apps.lessons.tasks", level="INFO") as captured:
            result = dedup_lessons_task()

        # Return value must not echo the user content.
        self.assertEqual(result["ok"], True)
        self.assertNotIn("output_tail", result)
        self.assertIn("output_bytes", result)
        # Safety: stringify the whole result and grep for the leak.
        serialized = repr(result)
        self.assertNotIn("swimming", serialized)
        self.assertNotIn("squats", serialized)
        self.assertNotIn("Duplicate group", serialized)

        # Logger output must not echo the user content either.
        joined_logs = "\n".join(captured.output)
        self.assertNotIn("swimming", joined_logs)
        self.assertNotIn("squats", joined_logs)
        self.assertIn("dedup_lessons_task: completed", joined_logs)
