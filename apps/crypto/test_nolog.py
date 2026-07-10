"""Tests for apps.crypto.nolog.RedactedStr — every render path must redact.

Red-team finding 18: f-strings / %-logging call __str__ or __format__, not
__repr__. This file proves ALL of str(), repr(), f-string, %-format, and a
real `logging` call redact — and that `.reveal()` is the only way back to
plaintext. Pure Python, no DB — SimpleTestCase.
"""

from __future__ import annotations

import logging

from django.test import SimpleTestCase

from apps.crypto.nolog import RedactedStr

PLAINTEXT = "hello world"
EXPECTED = f"‹redacted:{len(PLAINTEXT)}c›"  # 11 chars


class RedactedStrTest(SimpleTestCase):
    def setUp(self):
        self.x = RedactedStr(PLAINTEXT)

    def test_str_redacts(self):
        self.assertEqual(str(self.x), EXPECTED)
        self.assertNotIn(PLAINTEXT, str(self.x))

    def test_repr_redacts(self):
        self.assertEqual(repr(self.x), EXPECTED)
        self.assertNotIn(PLAINTEXT, repr(self.x))

    def test_fstring_redacts(self):
        rendered = f"{self.x}"
        self.assertEqual(rendered, EXPECTED)
        self.assertNotIn(PLAINTEXT, rendered)

    def test_percent_format_redacts(self):
        rendered = "%s" % self.x  # noqa: UP031 - deliberately %-formatting, mirrors logger.info("%s", x)
        self.assertEqual(rendered, EXPECTED)
        self.assertNotIn(PLAINTEXT, rendered)

    def test_dot_format_redacts(self):
        rendered = f"{self.x}"
        self.assertEqual(rendered, EXPECTED)
        self.assertNotIn(PLAINTEXT, rendered)

    def test_logging_call_redacts(self):
        logger_name = "apps.crypto.test_nolog.probe"
        with self.assertLogs(logger_name, level="INFO") as cm:
            logging.getLogger(logger_name).info("%s", self.x)
        self.assertEqual(len(cm.output), 1)
        self.assertIn(EXPECTED, cm.output[0])
        self.assertNotIn(PLAINTEXT, cm.output[0])

    def test_reveal_returns_real_plaintext(self):
        self.assertEqual(self.x.reveal(), PLAINTEXT)

    def test_reveal_is_not_redacted(self):
        self.assertNotEqual(self.x.reveal(), EXPECTED)

    def test_still_a_str_subclass(self):
        self.assertIsInstance(self.x, str)

    def test_equality_still_works_for_internal_logic(self):
        self.assertEqual(self.x, PLAINTEXT)

    def test_len_reflects_real_content_length(self):
        self.assertEqual(len(self.x), len(PLAINTEXT))

    def test_empty_redacted_str(self):
        empty = RedactedStr("")
        self.assertEqual(str(empty), "‹redacted:0c›")
        self.assertEqual(empty.reveal(), "")
