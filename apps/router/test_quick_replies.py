"""Tests for the generic quick-reply marker parser.

``extract_quick_replies`` is the shared helper every outbound-reply
chokepoint (iOS app, Telegram queue drain, Telegram poller, LINE) calls to
strip the agent's ``[[quick-replies: A | B | C]]`` marker before a user ever
sees it. Only the iOS path keeps the parsed labels (see test_ios_chat.py);
this module covers the parsing/validation contract in isolation.
"""

from __future__ import annotations

import logging

from django.test import SimpleTestCase

from apps.router.quick_replies import extract_quick_replies


class ExtractQuickRepliesTest(SimpleTestCase):
    def test_no_marker_returns_text_unchanged(self):
        text, labels = extract_quick_replies("Just a normal reply.")
        self.assertEqual(text, "Just a normal reply.")
        self.assertIsNone(labels)

    def test_empty_text(self):
        text, labels = extract_quick_replies("")
        self.assertEqual(text, "")
        self.assertIsNone(labels)

    def test_none_text(self):
        text, labels = extract_quick_replies(None)
        self.assertIsNone(text)
        self.assertIsNone(labels)

    def test_single_label(self):
        text, labels = extract_quick_replies("Want a summary?\n[[quick-replies: Yes]]")
        self.assertEqual(text, "Want a summary?")
        self.assertEqual(labels, ["Yes"])

    def test_two_labels(self):
        text, labels = extract_quick_replies("Keep going?\n[[quick-replies: Yes | No]]")
        self.assertEqual(text, "Keep going?")
        self.assertEqual(labels, ["Yes", "No"])

    def test_three_labels(self):
        text, labels = extract_quick_replies(
            "Save both changes?\n[[quick-replies: Save both | Change something | No thanks]]"
        )
        self.assertEqual(text, "Save both changes?")
        self.assertEqual(labels, ["Save both", "Change something", "No thanks"])

    def test_labels_trimmed(self):
        text, labels = extract_quick_replies("Pick:\n[[quick-replies:  Yes  |  No  ]]")
        self.assertEqual(labels, ["Yes", "No"])

    def test_trailing_newlines_after_marker_ignored(self):
        text, labels = extract_quick_replies("Pick:\n[[quick-replies: Yes | No]]\n\n")
        self.assertEqual(text, "Pick:")
        self.assertEqual(labels, ["Yes", "No"])

    def test_case_insensitive_marker(self):
        text, labels = extract_quick_replies("Pick:\n[[QUICK-REPLIES: Yes | No]]")
        self.assertEqual(labels, ["Yes", "No"])

    def test_multiline_prose_before_marker_preserved(self):
        text, labels = extract_quick_replies("Line one.\nLine two.\n[[quick-replies: Yes | No]]")
        self.assertEqual(text, "Line one.\nLine two.")
        self.assertEqual(labels, ["Yes", "No"])

    # ── marker must be the LAST line ────────────────────────────────────────

    def test_marker_mid_text_is_not_parsed(self):
        original = "Before [[quick-replies: Yes | No]] and after."
        text, labels = extract_quick_replies(original)
        self.assertEqual(text, original)
        self.assertIsNone(labels)

    def test_marker_followed_by_more_prose_is_not_parsed(self):
        original = "[[quick-replies: Yes | No]]\nOne more thing."
        text, labels = extract_quick_replies(original)
        self.assertEqual(text, original)
        self.assertIsNone(labels)

    def test_marker_with_trailing_prose_on_same_line_not_parsed(self):
        original = "Pick:\n[[quick-replies: Yes | No]] please"
        text, labels = extract_quick_replies(original)
        self.assertEqual(text, original)
        self.assertIsNone(labels)

    # ── malformed marker: stripped anyway, no buttons, telemetry logged ─────

    def test_too_many_labels_stripped_with_no_buttons(self):
        with self.assertLogs("apps.router.quick_replies", level=logging.WARNING) as cm:
            text, labels = extract_quick_replies(
                "Pick one:\n[[quick-replies: A | B | C | D]]",
                tenant_id="t-1",
                channel="ios",
            )
        self.assertEqual(text, "Pick one:")
        self.assertIsNone(labels)
        self.assertIn("quick_reply_marker_malformed", cm.output[0])

    def test_label_too_long_stripped_with_no_buttons(self):
        with self.assertLogs("apps.router.quick_replies", level=logging.WARNING):
            text, labels = extract_quick_replies("Pick:\n[[quick-replies: This label is definitely over 24 chars]]")
        self.assertEqual(text, "Pick:")
        self.assertIsNone(labels)

    def test_label_at_exactly_24_chars_is_valid(self):
        label = "x" * 24
        text, labels = extract_quick_replies(f"Pick:\n[[quick-replies: {label}]]")
        self.assertEqual(labels, [label])

    def test_label_at_25_chars_is_invalid(self):
        label = "x" * 25
        with self.assertLogs("apps.router.quick_replies", level=logging.WARNING):
            text, labels = extract_quick_replies(f"Pick:\n[[quick-replies: {label}]]")
        self.assertIsNone(labels)

    def test_empty_label_stripped_with_no_buttons(self):
        with self.assertLogs("apps.router.quick_replies", level=logging.WARNING):
            text, labels = extract_quick_replies("Pick:\n[[quick-replies: Yes | ]]")
        self.assertEqual(text, "Pick:")
        self.assertIsNone(labels)

    def test_zero_labels_stripped_with_no_buttons(self):
        with self.assertLogs("apps.router.quick_replies", level=logging.WARNING):
            text, labels = extract_quick_replies("Pick:\n[[quick-replies: ]]")
        self.assertEqual(text, "Pick:")
        self.assertIsNone(labels)

    def test_telemetry_includes_tenant_and_channel(self):
        with self.assertLogs("apps.router.quick_replies", level=logging.WARNING) as cm:
            extract_quick_replies(
                "Pick:\n[[quick-replies: A | B | C | D]]",
                tenant_id="tenant-abc",
                channel="telegram_drain",
            )
        record = cm.records[0]
        self.assertEqual(record.tenant_id, "tenant-abc")
        self.assertEqual(record.channel, "telegram_drain")

    def test_only_marker_yields_empty_remainder(self):
        text, labels = extract_quick_replies("[[quick-replies: Yes | No]]")
        self.assertEqual(text, "")
        self.assertEqual(labels, ["Yes", "No"])
