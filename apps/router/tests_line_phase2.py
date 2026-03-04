"""Tests for LINE Phase 2: loading animation, Reply API, Flex Messages, quick replies."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.router.line_flex import (
    attach_quick_reply,
    build_flex_bubble,
    build_flex_carousel,
    extract_quick_reply_buttons,
    should_use_flex,
    _parse_sections,
    _strip_md_inline,
    _parse_list_items,
)


# ────────────────────────────────────────────────────────────────────────────
# Flex Detection Tests
# ────────────────────────────────────────────────────────────────────────────


class ShouldUseFlexTest(TestCase):
    """Test the should_use_flex detection logic."""

    def test_short_simple_text_no_flex(self):
        self.assertFalse(should_use_flex("Hello! How can I help?"))

    def test_short_text_under_200_no_newlines(self):
        self.assertFalse(should_use_flex("a" * 199))

    def test_text_with_headers(self):
        text = "## Weather\nSunny and warm.\n\n## Tasks\n- Buy groceries"
        self.assertTrue(should_use_flex(text))

    def test_text_with_h3_headers(self):
        text = "### Morning\nBriefing content.\n\n### Afternoon\nMore content."
        self.assertTrue(should_use_flex(text))

    def test_text_with_bullet_list(self):
        text = "Here are your tasks:\n- Task one\n- Task two\n- Task three\n- Task four"
        self.assertTrue(should_use_flex(text))

    def test_text_with_numbered_list(self):
        text = "Steps to follow:\n1. First step\n2. Second step\n3. Third step"
        self.assertTrue(should_use_flex(text))

    def test_two_bullets_not_enough(self):
        text = "Some items:\n- Item one\n- Item two"
        self.assertFalse(should_use_flex(text))

    def test_many_sections(self):
        text = "\n\n".join([f"Section {i}: " + "content here " * 10 for i in range(5)])
        self.assertTrue(should_use_flex(text))

    def test_empty_string(self):
        self.assertFalse(should_use_flex(""))

    def test_single_line_long(self):
        self.assertFalse(should_use_flex("a" * 500))


# ────────────────────────────────────────────────────────────────────────────
# Section Parsing Tests
# ────────────────────────────────────────────────────────────────────────────


class ParseSectionsTest(TestCase):

    def test_single_header_with_content(self):
        text = "## Weather\nSunny and 25°C"
        sections = _parse_sections(text)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["title"], "Weather")
        self.assertIn("Sunny", sections[0]["content"])

    def test_multiple_headers(self):
        text = "## Weather\nSunny\n## Tasks\n- Buy milk\n## Notes\nNone"
        sections = _parse_sections(text)
        self.assertEqual(len(sections), 3)
        self.assertEqual(sections[0]["title"], "Weather")
        self.assertEqual(sections[1]["title"], "Tasks")
        self.assertEqual(sections[2]["title"], "Notes")

    def test_content_before_first_header(self):
        text = "Hello MJ!\n\n## Weather\nSunny"
        sections = _parse_sections(text)
        self.assertEqual(len(sections), 2)
        self.assertIsNone(sections[0]["title"])
        self.assertIn("Hello", sections[0]["content"])
        self.assertEqual(sections[1]["title"], "Weather")

    def test_no_headers(self):
        text = "Just a plain message with\nmultiple lines."
        sections = _parse_sections(text)
        self.assertEqual(len(sections), 1)
        self.assertIsNone(sections[0]["title"])

    def test_empty_content_after_header(self):
        text = "## Title\n## Another"
        sections = _parse_sections(text)
        self.assertEqual(len(sections), 2)


class StripMdInlineTest(TestCase):

    def test_bold(self):
        self.assertEqual(_strip_md_inline("**bold**"), "bold")

    def test_italic(self):
        self.assertEqual(_strip_md_inline("*italic*"), "italic")

    def test_code(self):
        self.assertEqual(_strip_md_inline("`code`"), "code")

    def test_link(self):
        self.assertEqual(
            _strip_md_inline("[click](https://example.com)"),
            "click: https://example.com",
        )

    def test_mixed(self):
        result = _strip_md_inline("**bold** and *italic* with `code`")
        self.assertEqual(result, "bold and italic with code")


class ParseListItemsTest(TestCase):

    def test_bullet_list(self):
        content = "- First item\n- Second item\n- Third item"
        items = _parse_list_items(content)
        self.assertEqual(items, ["First item", "Second item", "Third item"])

    def test_numbered_list(self):
        content = "1. Step one\n2. Step two\n3. Step three"
        items = _parse_list_items(content)
        self.assertEqual(items, ["Step one", "Step two", "Step three"])

    def test_mixed_content(self):
        content = "Some intro text\n- Item A\n- Item B\nMore text"
        items = _parse_list_items(content)
        self.assertEqual(items, ["Item A", "Item B"])

    def test_no_list(self):
        items = _parse_list_items("Just plain text here.")
        self.assertEqual(items, [])

    def test_strips_markdown_in_items(self):
        content = "- **Bold item**\n- *Italic item*"
        items = _parse_list_items(content)
        self.assertEqual(items, ["Bold item", "Italic item"])


# ────────────────────────────────────────────────────────────────────────────
# Flex Builder Tests
# ────────────────────────────────────────────────────────────────────────────


class BuildFlexBubbleTest(TestCase):

    def test_simple_structured_text(self):
        text = "## Weather\nSunny and warm.\n\n## Tasks\n- Buy groceries\n- Clean house"
        result = build_flex_bubble(text)
        self.assertEqual(result["type"], "flex")
        self.assertIn("altText", result)
        self.assertEqual(result["contents"]["type"], "bubble")
        body = result["contents"]["body"]
        self.assertEqual(body["type"], "box")
        self.assertTrue(len(body["contents"]) > 0)

    def test_alttext_truncated(self):
        result = build_flex_bubble("## Title\nContent", alt_text="x" * 500)
        self.assertTrue(len(result["altText"]) <= 400)

    def test_plain_text_fallback(self):
        result = build_flex_bubble("Just plain text no sections")
        self.assertEqual(result["type"], "flex")
        body = result["contents"]["body"]["contents"]
        # Should have at least one text component
        texts = [c for c in body if c.get("type") == "text"]
        self.assertTrue(len(texts) > 0)

    def test_sections_with_bullets(self):
        text = "## Shopping List\n- Apples\n- Bananas\n- Milk"
        result = build_flex_bubble(text)
        body = result["contents"]["body"]["contents"]
        # Should have horizontal boxes for bullet items
        horiz = [c for c in body if c.get("layout") == "horizontal"]
        self.assertEqual(len(horiz), 3)

    def test_body_contents_capped(self):
        """Very long content doesn't exceed 30 components."""
        sections = "\n".join([f"## Section {i}\n- Item {i}" for i in range(50)])
        result = build_flex_bubble(sections)
        body = result["contents"]["body"]["contents"]
        self.assertTrue(len(body) <= 30)

    def test_valid_json_serializable(self):
        text = "## Title\nContent with **bold** and [link](https://x.com)"
        result = build_flex_bubble(text)
        # Must be JSON-serializable
        json_str = json.dumps(result)
        self.assertIsInstance(json_str, str)


class BuildFlexCarouselTest(TestCase):

    def test_basic_carousel(self):
        items = [
            {"title": "Item 1", "content": "Description 1"},
            {"title": "Item 2", "content": "Description 2"},
        ]
        result = build_flex_carousel(items)
        self.assertEqual(result["type"], "flex")
        self.assertEqual(result["contents"]["type"], "carousel")
        self.assertEqual(len(result["contents"]["contents"]), 2)

    def test_carousel_max_12(self):
        items = [{"title": f"Item {i}"} for i in range(20)]
        result = build_flex_carousel(items)
        self.assertEqual(len(result["contents"]["contents"]), 12)

    def test_carousel_with_action_buttons(self):
        items = [{
            "title": "Approve",
            "content": "Lesson content",
            "action_label": "Approve",
            "action_data": "approve:lesson:123",
        }]
        result = build_flex_carousel(items)
        bubble = result["contents"]["contents"][0]
        self.assertIn("footer", bubble)
        button = bubble["footer"]["contents"][0]
        self.assertEqual(button["action"]["type"], "postback")
        self.assertEqual(button["action"]["data"], "approve:lesson:123")

    def test_label_truncated_to_20(self):
        items = [{
            "title": "Test",
            "action_label": "A very long label that exceeds twenty characters",
            "action_data": "test",
        }]
        result = build_flex_carousel(items)
        button = result["contents"]["contents"][0]["footer"]["contents"][0]
        self.assertTrue(len(button["action"]["label"]) <= 20)


# ────────────────────────────────────────────────────────────────────────────
# Quick Reply Tests
# ────────────────────────────────────────────────────────────────────────────


class ExtractQuickReplyButtonsTest(TestCase):

    def test_no_buttons(self):
        text, items = extract_quick_reply_buttons("Just regular text")
        self.assertEqual(text, "Just regular text")
        self.assertIsNone(items)

    def test_single_button(self):
        text, items = extract_quick_reply_buttons(
            "Choose one: [[button:Yes|confirm_yes]]"
        )
        self.assertNotIn("[[button:", text)
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"]["label"], "Yes")
        self.assertEqual(items[0]["action"]["data"], "confirm_yes")

    def test_multiple_buttons(self):
        text, items = extract_quick_reply_buttons(
            "How was your day?\n"
            "[[button:Great 😊|mood:great]]"
            "[[button:OK 😐|mood:ok]]"
            "[[button:Rough 😞|mood:rough]]"
        )
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["action"]["data"], "mood:great")
        self.assertEqual(items[1]["action"]["data"], "mood:ok")

    def test_max_13_buttons(self):
        buttons = "".join(
            f"[[button:Btn{i}|data{i}]]" for i in range(20)
        )
        text, items = extract_quick_reply_buttons(f"Pick one: {buttons}")
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 13)

    def test_display_text_set(self):
        _, items = extract_quick_reply_buttons("[[button:Approve|approve:123]]")
        self.assertEqual(items[0]["action"]["displayText"], "Approve")

    def test_label_truncated(self):
        _, items = extract_quick_reply_buttons(
            "[[button:This is a very long label exceeding twenty chars|data]]"
        )
        self.assertTrue(len(items[0]["action"]["label"]) <= 20)

    def test_cleaned_text_no_extra_newlines(self):
        text, _ = extract_quick_reply_buttons(
            "Question?\n\n\n\n[[button:Yes|yes]]\n\n\n"
        )
        self.assertNotIn("\n\n\n", text)


class AttachQuickReplyTest(TestCase):

    def test_attaches_to_text_message(self):
        msg = {"type": "text", "text": "Choose one:"}
        items = [{"type": "action", "action": {"type": "message", "label": "Yes", "text": "Yes"}}]
        result = attach_quick_reply(msg, items)
        self.assertIn("quickReply", result)
        self.assertEqual(len(result["quickReply"]["items"]), 1)

    def test_attaches_to_flex_message(self):
        msg = {"type": "flex", "altText": "test", "contents": {"type": "bubble"}}
        items = [{"type": "action", "action": {"type": "postback", "label": "OK", "data": "ok"}}]
        result = attach_quick_reply(msg, items)
        self.assertIn("quickReply", result)


# ────────────────────────────────────────────────────────────────────────────
# Loading Animation Tests
# ────────────────────────────────────────────────────────────────────────────


@override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token", LINE_CHANNEL_SECRET="test-secret")
class LoadingAnimationTest(TestCase):

    @patch("apps.router.line_webhook.httpx.post")
    def test_show_loading_calls_api(self, mock_post):
        from apps.router.line_webhook import _show_loading
        mock_post.return_value = MagicMock(status_code=200)
        _show_loading("U1234")
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn("chat/loading/start", call_args[0][0])
        self.assertEqual(call_args[1]["json"]["chatId"], "U1234")

    @patch("apps.router.line_webhook.httpx.post")
    def test_show_loading_silent_on_failure(self, mock_post):
        from apps.router.line_webhook import _show_loading
        mock_post.side_effect = Exception("network error")
        # Should not raise
        _show_loading("U1234")

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="")
    def test_show_loading_noop_without_token(self):
        from apps.router.line_webhook import _show_loading
        # Should not raise
        _show_loading("U1234")


# ────────────────────────────────────────────────────────────────────────────
# Reply API Tests
# ────────────────────────────────────────────────────────────────────────────


@override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token", LINE_CHANNEL_SECRET="test-secret")
class ReplyAPITest(TestCase):

    @patch("apps.router.line_webhook.httpx.post")
    def test_reply_api_success(self, mock_post):
        from apps.router.line_webhook import _send_line_reply
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_post.return_value = mock_resp
        result = _send_line_reply("valid_token", [{"type": "text", "text": "hi"}])
        self.assertTrue(result)
        call_url = mock_post.call_args[0][0]
        self.assertIn("message/reply", call_url)

    @patch("apps.router.line_webhook.httpx.post")
    def test_reply_api_expired_token(self, mock_post):
        from apps.router.line_webhook import _send_line_reply
        mock_resp = MagicMock()
        mock_resp.is_success = False
        mock_resp.status_code = 400
        mock_resp.text = "Invalid reply token"
        mock_post.return_value = mock_resp
        result = _send_line_reply("expired_token", [{"type": "text", "text": "hi"}])
        self.assertFalse(result)

    @patch("apps.router.line_webhook.httpx.post")
    def test_send_messages_prefers_reply(self, mock_post):
        """_send_line_messages tries Reply API first."""
        from apps.router.line_webhook import _send_line_messages
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_post.return_value = mock_resp
        result = _send_line_messages("U123", [{"type": "text", "text": "hi"}], reply_token="tok")
        self.assertTrue(result)
        # Should have called reply endpoint, not push
        call_url = mock_post.call_args[0][0]
        self.assertIn("message/reply", call_url)

    @patch("apps.router.line_webhook._send_line_push")
    @patch("apps.router.line_webhook._send_line_reply")
    def test_send_messages_falls_back_to_push(self, mock_reply, mock_push):
        """Falls back to Push when Reply fails."""
        from apps.router.line_webhook import _send_line_messages
        mock_reply.return_value = False
        mock_push.return_value = True
        result = _send_line_messages("U123", [{"type": "text", "text": "hi"}], reply_token="tok")
        self.assertTrue(result)
        mock_push.assert_called_once()

    @patch("apps.router.line_webhook._send_line_push")
    def test_send_messages_push_only_when_no_token(self, mock_push):
        """Uses Push directly when no reply_token."""
        from apps.router.line_webhook import _send_line_messages
        mock_push.return_value = True
        result = _send_line_messages("U123", [{"type": "text", "text": "hi"}], reply_token=None)
        self.assertTrue(result)
        mock_push.assert_called_once()

    def test_reply_returns_false_without_token(self):
        from apps.router.line_webhook import _send_line_reply
        self.assertFalse(_send_line_reply("", [{"type": "text", "text": "hi"}]))
        self.assertFalse(_send_line_reply(None, [{"type": "text", "text": "hi"}]))


# ────────────────────────────────────────────────────────────────────────────
# Integration: Flex in Webhook Response
# ────────────────────────────────────────────────────────────────────────────


@override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token", LINE_CHANNEL_SECRET="test-secret")
class WebhookFlexIntegrationTest(TestCase):
    """Test that structured AI responses get converted to Flex messages."""

    @patch("apps.router.line_webhook._send_line_messages")
    @patch("apps.router.line_webhook.httpx.post")
    def test_structured_response_sends_flex(self, mock_httpx, mock_send):
        """AI response with headers → Flex message."""
        from apps.router.line_webhook import LineWebhookView
        from apps.tenants.models import Tenant, User

        mock_send.return_value = True

        user = User.objects.create_user(
            username="flex_test",
            password="test123",
            line_user_id="U_flex_test",
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="test.example.com",
        )

        # Mock container response with structured content
        ai_response = {
            "choices": [{"message": {"content": (
                "## Weather\nSunny and 25°C in Osaka.\n\n"
                "## Tasks\n- Review PR\n- Deploy LINE integration\n- Test Flex messages"
            )}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "model": "test",
        }
        mock_container_resp = MagicMock()
        mock_container_resp.is_success = True
        mock_container_resp.status_code = 200
        mock_container_resp.json.return_value = ai_response
        mock_container_resp.raise_for_status = MagicMock()
        mock_httpx.return_value = mock_container_resp

        view = LineWebhookView()
        view._forward_to_container("U_flex_test", tenant, "What's up?", reply_token="tok123")

        mock_send.assert_called_once()
        messages = mock_send.call_args[0][1]
        self.assertEqual(messages[0]["type"], "flex")
        self.assertEqual(messages[0]["contents"]["type"], "bubble")

    @patch("apps.router.line_webhook._send_line_messages")
    @patch("apps.router.line_webhook.httpx.post")
    def test_short_response_sends_plain_text(self, mock_httpx, mock_send):
        """Short AI response → plain text, not Flex."""
        from apps.router.line_webhook import LineWebhookView
        from apps.tenants.models import Tenant, User

        mock_send.return_value = True

        user = User.objects.create_user(
            username="plain_test",
            password="test123",
            line_user_id="U_plain_test",
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="test.example.com",
        )

        ai_response = {
            "choices": [{"message": {"content": "Sure, I can help with that!"}}],
            "usage": {},
            "model": "test",
        }
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = ai_response
        mock_resp.raise_for_status = MagicMock()
        mock_httpx.return_value = mock_resp

        view = LineWebhookView()
        view._forward_to_container("U_plain_test", tenant, "Help me")

        mock_send.assert_called_once()
        messages = mock_send.call_args[0][1]
        self.assertEqual(messages[0]["type"], "text")

    @patch("apps.router.line_webhook._send_line_messages")
    @patch("apps.router.line_webhook.httpx.post")
    def test_response_with_buttons_gets_quick_reply(self, mock_httpx, mock_send):
        """AI response with button markers → quick reply attached."""
        from apps.router.line_webhook import LineWebhookView
        from apps.tenants.models import Tenant, User

        mock_send.return_value = True

        user = User.objects.create_user(
            username="qr_test",
            password="test123",
            line_user_id="U_qr_test",
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="test.example.com",
        )

        ai_response = {
            "choices": [{"message": {"content": (
                "How was your day?\n"
                "[[button:Great 😊|mood:great]]"
                "[[button:OK 😐|mood:ok]]"
                "[[button:Rough 😞|mood:rough]]"
            )}}],
            "usage": {},
            "model": "test",
        }
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = ai_response
        mock_resp.raise_for_status = MagicMock()
        mock_httpx.return_value = mock_resp

        view = LineWebhookView()
        view._forward_to_container("U_qr_test", tenant, "Check in")

        mock_send.assert_called_once()
        messages = mock_send.call_args[0][1]
        last_msg = messages[-1]
        self.assertIn("quickReply", last_msg)
        self.assertEqual(len(last_msg["quickReply"]["items"]), 3)


# ────────────────────────────────────────────────────────────────────────────
# Edge Cases
# ────────────────────────────────────────────────────────────────────────────


class FlexEdgeCaseTest(TestCase):

    def test_empty_text(self):
        result = build_flex_bubble("")
        self.assertEqual(result["type"], "flex")

    def test_only_headers_no_content(self):
        result = build_flex_bubble("## Title One\n## Title Two")
        self.assertEqual(result["type"], "flex")
        body = result["contents"]["body"]["contents"]
        self.assertTrue(len(body) > 0)

    def test_deeply_nested_markdown(self):
        text = "## **Bold Header**\n- ***bold italic item***\n- [Link](https://x.com)"
        result = build_flex_bubble(text)
        self.assertEqual(result["type"], "flex")
        # Should not crash, markdown stripped in Flex components

    def test_very_long_content(self):
        """Content over 2000 chars per component gets truncated."""
        long_text = "## Section\n" + ("x" * 5000)
        result = build_flex_bubble(long_text)
        # Check text component content is capped
        body = result["contents"]["body"]["contents"]
        for comp in body:
            if comp.get("type") == "text" and comp.get("text"):
                self.assertTrue(len(comp["text"]) <= 2000)

    def test_unicode_japanese_text(self):
        text = "## 天気\n大阪は晴れ、気温25度です。\n\n## タスク\n- PRをレビュー\n- LINEの統合をデプロイ\n- Flexメッセージをテスト"
        result = build_flex_bubble(text)
        self.assertEqual(result["type"], "flex")
        # Verify Japanese text preserved
        json_str = json.dumps(result, ensure_ascii=False)
        self.assertIn("天気", json_str)
        self.assertIn("大阪", json_str)

    def test_quick_reply_with_flex(self):
        """Quick replies work on Flex messages too."""
        text = "## Options\nPick one:\n- A\n- B\n- C\n[[button:Option A|pick:a]][[button:Option B|pick:b]]"
        cleaned, items = extract_quick_reply_buttons(text)
        self.assertIsNotNone(items)
        flex_msg = build_flex_bubble(cleaned)
        result = attach_quick_reply(flex_msg, items)
        self.assertIn("quickReply", result)
        self.assertEqual(result["type"], "flex")

    def test_special_chars_in_button_data(self):
        text = "[[button:Approve ✅|extract:approve_lesson:abc-123_xyz]]"
        _, items = extract_quick_reply_buttons(text)
        self.assertIsNotNone(items)
        self.assertEqual(items[0]["action"]["data"], "extract:approve_lesson:abc-123_xyz")

    def test_bullet_with_star_prefix(self):
        content = "* Star item one\n* Star item two\n* Star item three"
        items = _parse_list_items(content)
        self.assertEqual(len(items), 3)

    def test_bullet_with_unicode_bullet(self):
        content = "• Bullet one\n• Bullet two\n• Bullet three"
        items = _parse_list_items(content)
        self.assertEqual(len(items), 3)
