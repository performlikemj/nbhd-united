"""Tests for Phase 3 proxy features: rich responses, inline buttons, image delivery."""

import re
from unittest.mock import MagicMock

from django.test import TestCase

from apps.router.poller import TelegramPoller


class MessageSplitAndButtonTest(TestCase):
    """Tests for button parsing in AI responses."""

    def test_button_pattern_extraction(self):
        text = "Choose one:\n[[button:Option A|choice_a]] [[button:Option B|choice_b]]"
        pattern = re.compile(r"\[\[button:([^|]+)\|([^\]]+)\]\]")
        buttons = pattern.findall(text)
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0], ("Option A", "choice_a"))
        self.assertEqual(buttons[1], ("Option B", "choice_b"))

    def test_button_stripped_from_text(self):
        text = "Choose one:\n[[button:Option A|choice_a]]"
        pattern = re.compile(r"\[\[button:([^|]+)\|([^\]]+)\]\]")
        clean = pattern.sub("", text).strip()
        self.assertEqual(clean, "Choose one:")

    def test_no_buttons(self):
        text = "Just a regular response with no buttons."
        pattern = re.compile(r"\[\[button:([^|]+)\|([^\]]+)\]\]")
        buttons = pattern.findall(text)
        self.assertEqual(len(buttons), 0)


class ImageDetectionTest(TestCase):
    """Tests for image path detection in AI responses."""

    def test_media_prefix_detected(self):
        text = "Here's the image:\nMEDIA:./media/generated/cat.jpg\nPretty cool right?"
        pattern = re.compile(r"MEDIA:(\S+\.(?:jpg|jpeg|png|gif|webp))", re.IGNORECASE)
        matches = pattern.findall(text)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0], "./media/generated/cat.jpg")

    def test_workspace_path_detected(self):
        text = "Generated: /home/node/.openclaw/workspace/media/out/image.png"
        pattern = re.compile(r"(/home/node/\.openclaw/workspace/\S+\.(?:jpg|jpeg|png|gif|webp))", re.IGNORECASE)
        matches = pattern.findall(text)
        self.assertEqual(len(matches), 1)

    def test_no_images(self):
        text = "No images here, just text."
        media_pattern = re.compile(r"MEDIA:(\S+\.(?:jpg|jpeg|png|gif|webp))", re.IGNORECASE)
        workspace_pattern = re.compile(
            r"(/home/node/\.openclaw/workspace/\S+\.(?:jpg|jpeg|png|gif|webp))", re.IGNORECASE
        )
        self.assertEqual(len(media_pattern.findall(text)), 0)
        self.assertEqual(len(workspace_pattern.findall(text)), 0)


class RichResponseIntegrationTest(TestCase):
    """Integration tests for _send_rich_response."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()
        # Mock _send_markdown and _send_message
        self.poller._send_markdown = MagicMock()
        self.poller._send_message = MagicMock()
        self.poller._send_photo = MagicMock(return_value=True)

    def test_plain_text_response(self):
        from apps.tenants.models import Tenant

        tenant = MagicMock(spec=Tenant)
        self.poller._send_rich_response(123, tenant, "Hello world")
        self.poller._send_markdown.assert_called_once_with(123, "Hello world")

    def test_response_with_buttons(self):
        from apps.tenants.models import Tenant

        tenant = MagicMock(spec=Tenant)
        text = "Pick one:\n[[button:Yes|yes]] [[button:No|no]]"
        self.poller._send_rich_response(123, tenant, text)
        # Should call _send_message with reply_markup (not _send_markdown)
        self.poller._send_message.assert_called_once()
        call_kwargs = self.poller._send_message.call_args
        self.assertIn("reply_markup", call_kwargs[1])
        keyboard = call_kwargs[1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(keyboard), 2)
        self.assertEqual(keyboard[0][0]["text"], "Yes")
        self.assertEqual(keyboard[0][0]["callback_data"], "agent:yes")
