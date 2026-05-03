"""Tests for build_datetime_context — per-turn time header injection.

Also covers `build_chat_context_marker` — the conversational-turn marker
that tells the agent to skip the heavy AGENTS.md session-start context-load.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from django.test import TestCase

from apps.router.services import build_chat_context_marker, build_datetime_context


class BuildDatetimeContextTest(TestCase):
    """build_datetime_context returns a well-formed [Now: ...] header."""

    def test_basic_format(self):
        result = build_datetime_context("UTC")
        self.assertTrue(result.startswith("[Now: "))
        self.assertTrue(result.endswith("]\n"))

    def test_includes_day_of_week(self):
        result = build_datetime_context("UTC")
        days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        self.assertTrue(any(day in result for day in days))

    def test_respects_timezone(self):
        # Pin time to verify timezone offset is applied
        fixed = datetime(2026, 4, 21, 6, 47, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Tokyo"))
        with patch("apps.router.services.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = build_datetime_context("Asia/Tokyo")
        self.assertIn("2026-04-21 06:47", result)
        self.assertIn("JST", result)

    def test_fallback_on_invalid_timezone(self):
        result = build_datetime_context("Invalid/Zone")
        self.assertIn("UTC", result)
        self.assertTrue(result.startswith("[Now: "))

    def test_fallback_on_empty_string(self):
        result = build_datetime_context("")
        self.assertIn("UTC", result)

    def test_matches_expected_pattern(self):
        result = build_datetime_context("US/Pacific")
        # [Now: YYYY-MM-DD HH:MM TZ (Weekday)]
        pattern = r"^\[Now: \d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+ \(\w+\)\]\n$"
        self.assertRegex(result, pattern)


class BuildChatContextMarkerTest(TestCase):
    """build_chat_context_marker returns the conversational-turn signal."""

    def test_starts_with_chat_marker(self):
        result = build_chat_context_marker()
        self.assertTrue(result.startswith("[chat:"))

    def test_ends_with_newline(self):
        result = build_chat_context_marker()
        self.assertTrue(result.endswith("\n"))

    def test_mentions_skip_workspace_docs(self):
        # The marker must explicitly tell the agent to skip the auto-context-load,
        # otherwise AGENTS.md still triggers daily-note + journal-context fetches.
        result = build_chat_context_marker()
        self.assertIn("workspace docs", result)

    def test_marker_is_single_line(self):
        # The marker must be a single line so it doesn't split a [Now: ...]
        # header from the user's actual message text.
        result = build_chat_context_marker()
        # Count newlines — should be exactly 1, the trailing newline.
        self.assertEqual(result.count("\n"), 1)

    def test_marker_is_compact(self):
        # Should be short enough that we're not paying real prompt-token cost
        # on every conversational turn — a few hundred chars at most.
        result = build_chat_context_marker()
        self.assertLess(len(result), 300)
