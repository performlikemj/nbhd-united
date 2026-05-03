"""Tests for LINE Phase 2: loading animation, Reply API, Flex Messages, quick replies."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.router.line_flex import (
    COLORS,
    _extract_links,
    _link_component,
    _parse_list_items,
    _parse_sections,
    _strip_md_inline,
    attach_quick_reply,
    build_flex_bubble,
    build_flex_carousel,
    build_short_bubble,
    build_status_bubble,
    classify_content,
    extract_quick_reply_buttons,
    should_use_flex,
)

# ────────────────────────────────────────────────────────────────────────────
# Content Classification Tests
# ────────────────────────────────────────────────────────────────────────────


class ClassifyContentTest(TestCase):
    def test_empty_string(self):
        self.assertEqual(classify_content(""), "short")

    def test_short_simple_text(self):
        self.assertEqual(classify_content("Hello! How can I help?"), "short")

    def test_medium_text_without_structure(self):
        self.assertEqual(classify_content("a" * 500), "short")

    def test_text_with_headers(self):
        text = "## Weather\nSunny and warm.\n\n## Tasks\n- Buy groceries"
        self.assertEqual(classify_content(text), "structured")

    def test_text_with_bullet_list(self):
        text = "Here are your tasks:\n- Task one\n- Task two\n- Task three\n- Task four"
        self.assertEqual(classify_content(text), "structured")

    def test_text_with_numbered_list(self):
        text = "Steps to follow:\n1. First step\n2. Second step\n3. Third step"
        self.assertEqual(classify_content(text), "structured")

    def test_many_sections(self):
        text = "\n\n".join([f"Section {i}: " + "content here " * 10 for i in range(5)])
        self.assertEqual(classify_content(text), "structured")

    def test_very_long_plain_text(self):
        self.assertEqual(classify_content("a " * 1500), "plain_text")


class ShouldUseFlexTest(TestCase):
    """Test the should_use_flex detection logic."""

    def test_short_simple_text_uses_flex(self):
        # Short messages now use Flex (short bubble)
        self.assertTrue(should_use_flex("Hello! How can I help?"))

    def test_short_text_under_200_no_newlines(self):
        self.assertTrue(should_use_flex("a" * 199))

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

    def test_two_bullets_not_enough_for_structured(self):
        text = "Some items:\n- Item one\n- Item two"
        # Still uses flex (short bubble), just not structured
        self.assertTrue(should_use_flex(text))

    def test_many_sections(self):
        text = "\n\n".join([f"Section {i}: " + "content here " * 10 for i in range(5)])
        self.assertTrue(should_use_flex(text))

    def test_empty_string(self):
        self.assertTrue(should_use_flex(""))

    def test_very_long_plain_text_no_flex(self):
        # Only very long unstructured text falls back to plain text
        self.assertFalse(should_use_flex("a " * 1500))


# ────────────────────────────────────────────────────────────────────────────
# Section Parsing Tests
# ────────────────────────────────────────────────────────────────────────────


class ParseSectionsTest(TestCase):
    def test_single_header_with_content(self):
        text = "## Weather\nSunny and 25\u00b0C"
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
            "click",
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
# Link Extraction Tests
# ────────────────────────────────────────────────────────────────────────────


class ExtractLinksTest(TestCase):
    def test_markdown_link(self):
        links = _extract_links("Check [Google](https://google.com) for info")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0], ("Google", "https://google.com"))

    def test_bare_url(self):
        links = _extract_links("Visit https://example.com for more")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][1], "https://example.com")
        self.assertEqual(links[0][0], "example.com")

    def test_multiple_links(self):
        text = "[A](https://a.com) and [B](https://b.com)"
        links = _extract_links(text)
        self.assertEqual(len(links), 2)

    def test_no_duplicates(self):
        text = "[Link](https://x.com) and https://x.com again"
        links = _extract_links(text)
        self.assertEqual(len(links), 1)

    def test_no_links(self):
        links = _extract_links("Just plain text")
        self.assertEqual(links, [])

    def test_bare_url_strips_trailing_punctuation(self):
        links = _extract_links("See https://example.com/path.")
        self.assertEqual(links[0][1], "https://example.com/path")


class LinkComponentTest(TestCase):
    def test_has_uri_action(self):
        comp = _link_component("Google", "https://google.com")
        self.assertEqual(comp["action"]["type"], "uri")
        self.assertEqual(comp["action"]["uri"], "https://google.com")

    def test_uses_signal_text_color(self):
        comp = _link_component("Link", "https://example.com")
        text_comp = comp["contents"][1]
        self.assertEqual(text_comp["color"], COLORS["signal_text"])


class FlexBubbleLinksTest(TestCase):
    def test_structured_bubble_includes_link_rows(self):
        text = "## Info\nCheck [docs](https://docs.example.com) for details."
        result = build_flex_bubble(text)
        body = result["contents"]["body"]["contents"]
        # Find link components (boxes with URI action)
        link_boxes = [c for c in body if c.get("action", {}).get("type") == "uri"]
        self.assertEqual(len(link_boxes), 1)
        self.assertEqual(link_boxes[0]["action"]["uri"], "https://docs.example.com")

    def test_short_bubble_includes_link_rows(self):
        text = "Visit https://example.com for more info"
        result = build_short_bubble(text)
        body = result["contents"]["body"]["contents"]
        # Second item should be a link row
        self.assertTrue(len(body) >= 2)
        link_box = body[1]
        self.assertEqual(link_box["action"]["type"], "uri")

    def test_no_links_no_extra_components(self):
        text = "## Title\nJust text, no links here."
        result = build_flex_bubble(text)
        body = result["contents"]["body"]["contents"]
        link_boxes = [c for c in body if c.get("action", {}).get("type") == "uri"]
        self.assertEqual(len(link_boxes), 0)


# ────────────────────────────────────────────────────────────────────────────
# Flex Builder Tests
# ────────────────────────────────────────────────────────────────────────────


class BuildFlexBubbleTest(TestCase):
    def test_simple_structured_text(self):
        text = "## Weather\nSunny and warm.\n\n## Tasks\n- Buy groceries\n- Clean house"
        result = build_flex_bubble(text)
        self.assertEqual(result["type"], "flex")
        self.assertIn("altText", result)
        bubble = result["contents"]
        self.assertEqual(bubble["type"], "bubble")
        # Should have branded styles
        self.assertIn("styles", bubble)
        self.assertEqual(bubble["styles"]["body"]["backgroundColor"], COLORS["mist"])

    def test_header_promoted_to_bubble_header(self):
        text = "## Weather\nSunny and warm."
        result = build_flex_bubble(text)
        bubble = result["contents"]
        self.assertIn("header", bubble)
        self.assertEqual(
            bubble["styles"]["header"]["backgroundColor"],
            COLORS["signal_text"],
        )
        # Header text should be white
        header_text = bubble["header"]["contents"][0]
        self.assertEqual(header_text["color"], COLORS["white"])

    def test_alttext_truncated(self):
        result = build_flex_bubble("## Title\nContent", alt_text="x" * 500)
        self.assertTrue(len(result["altText"]) <= 400)

    def test_short_text_produces_short_bubble(self):
        result = build_flex_bubble("Just a quick reply!")
        bubble = result["contents"]
        self.assertEqual(bubble["size"], "mega")
        self.assertEqual(bubble["styles"]["body"]["backgroundColor"], COLORS["mist"])
        # Body is vertical, first child is horizontal accent bar row
        body = bubble["body"]
        self.assertEqual(body["layout"], "vertical")
        accent_row = body["contents"][0]
        self.assertEqual(accent_row["layout"], "horizontal")

    def test_sections_with_bullets(self):
        text = "## Shopping List\n- Apples\n- Bananas\n- Milk"
        result = build_flex_bubble(text)
        body = result["contents"]["body"]["contents"]
        # Should have baseline boxes for bullet items
        baseline = [c for c in body if c.get("layout") == "baseline"]
        self.assertEqual(len(baseline), 3)

    def test_bullet_uses_signal_color(self):
        text = "## List\n- Item one\n- Item two\n- Item three"
        result = build_flex_bubble(text)
        body = result["contents"]["body"]["contents"]
        baseline_boxes = [c for c in body if c.get("layout") == "baseline"]
        self.assertTrue(len(baseline_boxes) > 0)
        bullet_text = baseline_boxes[0]["contents"][0]
        self.assertEqual(bullet_text["color"], COLORS["signal"])

    def test_body_contents_capped(self):
        """Very long content doesn't exceed 30 components."""
        sections = "\n".join([f"## Section {i}\n- Item {i}" for i in range(50)])
        result = build_flex_bubble(sections)
        body = result["contents"]["body"]["contents"]
        self.assertTrue(len(body) <= 30)

    def test_valid_json_serializable(self):
        text = "## Title\nContent with **bold** and [link](https://x.com)"
        result = build_flex_bubble(text)
        json_str = json.dumps(result)
        self.assertIsInstance(json_str, str)

    def test_branded_colors_in_bubble(self):
        text = "## Header\nBody text.\n\n## Section\n- Item"
        result = build_flex_bubble(text)
        bubble = result["contents"]
        self.assertEqual(bubble["styles"]["header"]["backgroundColor"], COLORS["signal_text"])
        self.assertEqual(bubble["styles"]["body"]["backgroundColor"], COLORS["mist"])


class BuildShortBubbleTest(TestCase):
    def test_produces_flex_message(self):
        result = build_short_bubble("Hello!")
        self.assertEqual(result["type"], "flex")

    def test_has_accent_bar(self):
        result = build_short_bubble("Hello!")
        body = result["contents"]["body"]
        self.assertEqual(body["layout"], "vertical")
        accent_row = body["contents"][0]
        self.assertEqual(accent_row["layout"], "horizontal")
        accent_bar = accent_row["contents"][0]
        self.assertEqual(accent_bar["backgroundColor"], COLORS["signal"])

    def test_warm_background(self):
        result = build_short_bubble("Hello!")
        self.assertEqual(
            result["contents"]["styles"]["body"]["backgroundColor"],
            COLORS["mist"],
        )


class BuildStatusBubbleTest(TestCase):
    def test_success_tone(self):
        result = build_status_bubble("Done!", tone="success")
        self.assertEqual(result["type"], "flex")
        bg = result["contents"]["styles"]["body"]["backgroundColor"]
        self.assertEqual(bg, COLORS["emerald_bg"])

    def test_error_tone(self):
        result = build_status_bubble("Failed.", tone="error")
        bg = result["contents"]["styles"]["body"]["backgroundColor"]
        self.assertEqual(bg, COLORS["rose_bg"])

    def test_warning_tone(self):
        result = build_status_bubble("Caution.", tone="warning")
        bg = result["contents"]["styles"]["body"]["backgroundColor"]
        self.assertEqual(bg, COLORS["amber_bg"])

    def test_icon_and_text_present(self):
        result = build_status_bubble("Message", tone="success")
        body_contents = result["contents"]["body"]["contents"]
        self.assertEqual(len(body_contents), 2)
        # Icon has flex 0, text has flex 1
        self.assertEqual(body_contents[0]["flex"], 0)
        self.assertEqual(body_contents[1]["flex"], 1)


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
        items = [
            {
                "title": "Approve",
                "content": "Lesson content",
                "action_label": "Approve",
                "action_data": "approve:lesson:123",
            }
        ]
        result = build_flex_carousel(items)
        bubble = result["contents"]["contents"][0]
        self.assertIn("footer", bubble)
        button = bubble["footer"]["contents"][0]
        self.assertEqual(button["action"]["type"], "postback")
        self.assertEqual(button["action"]["data"], "approve:lesson:123")

    def test_carousel_uses_branded_button_color(self):
        items = [
            {
                "title": "Test",
                "action_label": "Go",
                "action_data": "test",
            }
        ]
        result = build_flex_carousel(items)
        button = result["contents"]["contents"][0]["footer"]["contents"][0]
        self.assertEqual(button["color"], COLORS["signal_text"])

    def test_carousel_has_branded_background(self):
        items = [{"title": "Item 1"}]
        result = build_flex_carousel(items)
        bubble = result["contents"]["contents"][0]
        self.assertEqual(bubble["styles"]["body"]["backgroundColor"], COLORS["mist"])

    def test_label_truncated_to_20(self):
        items = [
            {
                "title": "Test",
                "action_label": "A very long label that exceeds twenty characters",
                "action_data": "test",
            }
        ]
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
        text, items = extract_quick_reply_buttons("Choose one: [[button:Yes|confirm_yes]]")
        self.assertNotIn("[[button:", text)
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"]["label"], "Yes")
        self.assertEqual(items[0]["action"]["data"], "confirm_yes")

    def test_multiple_buttons(self):
        text, items = extract_quick_reply_buttons(
            "How was your day?\n"
            "[[button:Great \U0001f60a|mood:great]]"
            "[[button:OK \U0001f610|mood:ok]]"
            "[[button:Rough \U0001f61e|mood:rough]]"
        )
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["action"]["data"], "mood:great")
        self.assertEqual(items[1]["action"]["data"], "mood:ok")

    def test_max_13_buttons(self):
        buttons = "".join(f"[[button:Btn{i}|data{i}]]" for i in range(20))
        text, items = extract_quick_reply_buttons(f"Pick one: {buttons}")
        self.assertIsNotNone(items)
        self.assertEqual(len(items), 13)

    def test_display_text_set(self):
        _, items = extract_quick_reply_buttons("[[button:Approve|approve:123]]")
        self.assertEqual(items[0]["action"]["displayText"], "Approve")

    def test_label_truncated(self):
        _, items = extract_quick_reply_buttons("[[button:This is a very long label exceeding twenty chars|data]]")
        self.assertTrue(len(items[0]["action"]["label"]) <= 20)

    def test_cleaned_text_no_extra_newlines(self):
        text, _ = extract_quick_reply_buttons("Question?\n\n\n\n[[button:Yes|yes]]\n\n\n")
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
    @patch("apps.router.pending_queue.httpx.post")
    def test_structured_response_sends_flex(self, mock_httpx, mock_send):
        """AI response with headers -> Flex message.

        After PR #431 (per-tenant queue), ``_forward_to_container``
        enqueues a row and the QStash drain (sync fallback in tests)
        does the actual POST + relay. Mock target moved from
        ``line_webhook.httpx.post`` to ``pending_queue.httpx.post``.
        """
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
            "choices": [
                {
                    "message": {
                        "content": (
                            "## Weather\nSunny and 25\u00b0C in Osaka.\n\n"
                            "## Tasks\n- Review PR\n- Deploy LINE integration\n- Test Flex messages"
                        )
                    }
                }
            ],
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
    @patch("apps.router.pending_queue.httpx.post")
    def test_short_response_sends_flex(self, mock_httpx, mock_send):
        """Short AI response -> branded Flex bubble (not plain text)."""
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
        # Now returns Flex, not plain text
        self.assertEqual(messages[0]["type"], "flex")

    @patch("apps.router.line_webhook._send_line_messages")
    @patch("apps.router.pending_queue.httpx.post")
    def test_response_with_buttons_gets_quick_reply(self, mock_httpx, mock_send):
        """AI response with button markers -> quick reply attached."""
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
            "choices": [
                {
                    "message": {
                        "content": (
                            "How was your day?\n"
                            "[[button:Great \U0001f60a|mood:great]]"
                            "[[button:OK \U0001f610|mood:ok]]"
                            "[[button:Rough \U0001f61e|mood:rough]]"
                        )
                    }
                }
            ],
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
        # Should have header block for first title
        self.assertIn("header", result["contents"])

    def test_deeply_nested_markdown(self):
        text = "## **Bold Header**\n- ***bold italic item***\n- [Link](https://x.com)"
        result = build_flex_bubble(text)
        self.assertEqual(result["type"], "flex")

    def test_very_long_content(self):
        """Content over 2000 chars per component gets truncated."""
        long_text = "## Section\n" + ("x" * 5000)
        result = build_flex_bubble(long_text)
        body = result["contents"]["body"]["contents"]
        for comp in body:
            if comp.get("type") == "text" and comp.get("text"):
                self.assertTrue(len(comp["text"]) <= 2000)

    def test_unicode_japanese_text(self):
        text = "## \u5929\u6c17\n\u5927\u962a\u306f\u6674\u308c\u3001\u6c17\u6e2925\u5ea6\u3067\u3059\u3002\n\n## \u30bf\u30b9\u30af\n- PR\u3092\u30ec\u30d3\u30e5\u30fc\n- LINE\u306e\u7d71\u5408\u3092\u30c7\u30d7\u30ed\u30a4\n- Flex\u30e1\u30c3\u30bb\u30fc\u30b8\u3092\u30c6\u30b9\u30c8"
        result = build_flex_bubble(text)
        self.assertEqual(result["type"], "flex")
        json_str = json.dumps(result, ensure_ascii=False)
        self.assertIn("\u5929\u6c17", json_str)
        self.assertIn("\u5927\u962a", json_str)

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
        text = "[[button:Approve \u2705|extract:approve_lesson:abc-123_xyz]]"
        _, items = extract_quick_reply_buttons(text)
        self.assertIsNotNone(items)
        self.assertEqual(items[0]["action"]["data"], "extract:approve_lesson:abc-123_xyz")

    def test_bullet_with_star_prefix(self):
        content = "* Star item one\n* Star item two\n* Star item three"
        items = _parse_list_items(content)
        self.assertEqual(len(items), 3)

    def test_bullet_with_unicode_bullet(self):
        content = "\u2022 Bullet one\n\u2022 Bullet two\n\u2022 Bullet three"
        items = _parse_list_items(content)
        self.assertEqual(len(items), 3)


# ────────────────────────────────────────────────────────────────────────────
# Table Conversion Tests
# ────────────────────────────────────────────────────────────────────────────


class MarkdownTableConversionTest(TestCase):
    """Test markdown table -> readable text conversion."""

    def test_basic_table(self):
        from apps.router.line_webhook import _strip_markdown

        text = (
            "| Exercise | Sets \u00d7 Reps | Rest |\n"
            "|----------|-------------|------|\n"
            "| Pull-Ups | 4 \u00d7 6-10 | 90 sec |\n"
            "| Incline Press | 3 \u00d7 8-10 | 90 sec |"
        )
        result = _strip_markdown(text)
        self.assertNotIn("|", result)
        self.assertIn("Exercise: Pull-Ups", result)
        self.assertIn("Sets \u00d7 Reps: 4 \u00d7 6-10", result)
        self.assertIn("Rest: 90 sec", result)

    def test_table_with_surrounding_text(self):
        from apps.router.line_webhook import _strip_markdown

        text = (
            "Here's your workout:\n\n"
            "| Exercise | Sets |\n"
            "|----------|------|\n"
            "| Squats | 4\u00d78 |\n\n"
            "Have a great session!"
        )
        result = _strip_markdown(text)
        self.assertIn("Here's your workout", result)
        self.assertIn("Exercise: Squats", result)
        self.assertIn("great session", result)

    def test_no_table_unchanged(self):
        from apps.router.line_webhook import _strip_markdown

        text = "Just regular text with a | pipe character"
        result = _strip_markdown(text)
        self.assertIn("pipe character", result)

    def test_multiple_data_rows(self):
        from apps.router.line_webhook import _strip_markdown

        text = "| Name | Score |\n|------|-------|\n| Alice | 95 |\n| Bob | 87 |\n| Charlie | 92 |"
        result = _strip_markdown(text)
        self.assertIn("Name: Alice", result)
        self.assertIn("Name: Bob", result)
        self.assertIn("Score: 92", result)

    def test_table_separator_stripped(self):
        from apps.router.line_webhook import _strip_markdown

        text = "| A | B |\n|---|---|\n| 1 | 2 |"
        result = _strip_markdown(text)
        self.assertNotIn("---", result)

    def test_japanese_table(self):
        from apps.router.line_webhook import _strip_markdown

        text = (
            "| \u7a2e\u76ee | \u30bb\u30c3\u30c8 | \u4f11\u61a9 |\n"
            "|------|--------|------|\n"
            "| \u30b9\u30af\u30ef\u30c3\u30c8 | 4\u00d78 | 90\u79d2 |"
        )
        result = _strip_markdown(text)
        self.assertIn("\u7a2e\u76ee: \u30b9\u30af\u30ef\u30c3\u30c8", result)
        self.assertIn("\u30bb\u30c3\u30c8: 4\u00d78", result)


# ────────────────────────────────────────────────────────────────────────────
# Localization of system status messages (PR #393)
# ────────────────────────────────────────────────────────────────────────────


@override_settings(LINE_CHANNEL_ACCESS_TOKEN="test-token", LINE_CHANNEL_SECRET="test-secret")
class PendingMessageApologyLocalizationTest(TestCase):
    """After PR #431, ``_forward_to_container`` enqueues onto a per-tenant
    queue rather than POSTing synchronously. Transient container failures
    (502, timeout) become per-row retries; persistent failures past the
    attempts cap surface to the user as a localized apology via
    ``_send_apology_for_dropped_pending_message``.

    The PRE-#431 ``WebhookForwardErrorLocalizationTest`` covered the
    synchronous error-message localization. With the queue, that
    localization is still required — but it's the apology copy that needs
    to respect ``tenant.user.language``.
    """

    def _make_user_tenant(self, lang: str, line_user_id: str):
        from apps.tenants.models import Tenant, User

        user = User.objects.create_user(
            username=f"loc_{lang}_{line_user_id[-4:]}",
            password="test123",
            line_user_id=line_user_id,
            language=lang,
        )
        tenant = Tenant.objects.create(
            user=user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="test.example.com",
        )
        return tenant

    @patch("apps.router.line_webhook._send_line_text", return_value=True)
    def test_apology_localized_to_japanese(self, mock_send_text):
        from apps.router.models import PendingMessage
        from apps.router.pending_queue import _send_apology_for_dropped_pending_message

        tenant = self._make_user_tenant("ja", "U_loc_apology_ja")
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_loc_apology_ja",
            payload={"message_text": "こんにちは"},
            user_text="こんにちは",
        )

        _send_apology_for_dropped_pending_message(tenant, msg)

        mock_send_text.assert_called_once()
        body = mock_send_text.call_args[0][1]
        # Japanese apology marker — must use translated copy, not English.
        self.assertIn("\u3054\u3081\u3093\u306a\u3055\u3044", body)
        self.assertNotIn("Sorry", body)

    @patch("apps.router.line_webhook._send_line_text", return_value=True)
    def test_apology_falls_back_to_english_for_untranslated_language(self, mock_send_text):
        from apps.router.models import PendingMessage
        from apps.router.pending_queue import _send_apology_for_dropped_pending_message

        tenant = self._make_user_tenant("vi", "U_loc_apology_vi")  # Vietnamese — falls back
        msg = PendingMessage.objects.create(
            tenant=tenant,
            channel=PendingMessage.Channel.LINE,
            channel_user_id="U_loc_apology_vi",
            payload={"message_text": "hi"},
            user_text="hi",
        )

        _send_apology_for_dropped_pending_message(tenant, msg)
        body = mock_send_text.call_args[0][1]
        self.assertIn("Sorry", body)
