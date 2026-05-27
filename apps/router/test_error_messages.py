"""Tests for the ``strip_internal_framing`` scrubber.

The chat pipeline prepends bracket-framed agent-context markers like
``[Now: …]``, ``[chat: …]``, ``[System: just updated…]``,
``[User tapped button: …]`` to outbound payloads. When a delivery fails
three times and we apologize, the quoted excerpt must NOT echo this
internal framing back to the user. Every call site should pass
``raw_user_text`` so ``PendingMessage.user_text`` is already clean — but
the scrubber is wired into both apology builders (router + orchestrator)
as defense in depth.
"""

from __future__ import annotations

from django.test import TestCase

from apps.router.error_messages import strip_internal_framing


class StripInternalFramingTest(TestCase):
    def test_returns_empty_unchanged(self):
        self.assertEqual(strip_internal_framing(""), "")

    def test_returns_plain_text_unchanged(self):
        self.assertEqual(strip_internal_framing("hello there"), "hello there")

    def test_strips_system_just_updated(self):
        framed = "[System: just updated. User's message from before the update:]\nthanks. i'll read through these"
        self.assertEqual(strip_internal_framing(framed), "thanks. i'll read through these")

    def test_strips_system_restarting(self):
        framed = "[System: assistant was restarting when this arrived. User's message:]\nactual user words"
        self.assertEqual(strip_internal_framing(framed), "actual user words")

    def test_strips_now_marker(self):
        framed = "[Now: 2026-05-27 22:46 JST (Tuesday)]\nhi"
        self.assertEqual(strip_internal_framing(framed), "hi")

    def test_strips_chat_marker(self):
        framed = "[chat: user is mid-conversation, reply concisely]\nquick q for you"
        self.assertEqual(strip_internal_framing(framed), "quick q for you")

    def test_strips_button_tap_marker(self):
        framed = '[User tapped button: "Yes, schedule it"]'
        self.assertEqual(strip_internal_framing(framed), "")

    def test_strips_photo_attached_marker(self):
        framed = "[Photo attached: /workspaces/abc/photo.jpg]\nwhat is this?"
        self.assertEqual(strip_internal_framing(framed), "what is this?")

    def test_strips_stacked_markers(self):
        framed = "[Now: 2026-05-27 22:46 JST]\n[chat: user is mid-conversation]\nactual question"
        self.assertEqual(strip_internal_framing(framed), "actual question")

    def test_strips_stacked_now_then_system(self):
        framed = "[Now: 2026-05-27 22:46 JST]\n[System: just updated. User's message from before the update:]\nthe real message"
        self.assertEqual(strip_internal_framing(framed), "the real message")

    def test_leaves_user_bracket_text_alone(self):
        # If the user legitimately starts their message with a non-allowlisted
        # bracket, leave it intact — only known internal tags get stripped.
        self.assertEqual(strip_internal_framing("[TODO] write tests"), "[TODO] write tests")
        self.assertEqual(strip_internal_framing("[draft] hello"), "[draft] hello")

    def test_case_insensitive_tag(self):
        framed = "[system: just updated. user's message:]\nhi"
        self.assertEqual(strip_internal_framing(framed), "hi")

    def test_does_not_strip_marker_in_middle(self):
        # Scrubber is anchored to the start. Mid-text marker stays.
        self.assertEqual(
            strip_internal_framing("hello [Now: 22:46]"),
            "hello [Now: 22:46]",
        )

    def test_preserves_internal_newlines_in_user_text(self):
        framed = "[System: just updated. User's message from before the update:]\nline one\n\nline two"
        self.assertEqual(strip_internal_framing(framed), "line one\n\nline two")
