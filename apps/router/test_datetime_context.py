"""Tests for build_datetime_context — per-turn time header injection."""

from __future__ import annotations

import re
from unittest.mock import patch
from datetime import datetime

from django.test import TestCase

from apps.router.services import build_datetime_context


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
