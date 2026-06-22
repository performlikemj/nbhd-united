"""Adversarial-audit cluster A28 regression tests.

FA-1036 — buttons-only Telegram reply silently dropped.

When the agent emits only [[button:...]] markers with no prose (e.g.
``[[button:Yes|confirm]][[button:No|deny]]``), both delivery paths stripped
the markers from text, leaving text empty, then short-circuited before
sending the keyboard:

  pending_queue.relay_ai_response_to_telegram:
      if not text: return True   (pre-fix)

  poller._send_rich_response:
      if clean_text: ...         (pre-fix, outer guard skips the elif)

The fix sends a middle-dot placeholder ("·") carrying the inline_keyboard
when text is empty after button-stripping, so the user still receives the
buttons.

These tests unit-test the critical branching logic in isolation — the
inner helpers and the poller method — without requiring the full Django
request cycle or a live Telegram token.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

# ---------------------------------------------------------------------------
# _send_telegram_markdown — the delivery helper used by the pending-queue path
# ---------------------------------------------------------------------------

class SendTelegramMarkdownButtonsOnlyTest(SimpleTestCase):
    """_send_telegram_markdown must deliver reply_markup even with minimal text."""

    @patch("apps.router.pending_queue._telegram_api_base", return_value=None)
    def test_no_token_returns_false(self, _mock_base):
        """Without a bot token the helper returns False (unchanged)."""
        from apps.router.pending_queue import _send_telegram_markdown

        result = _send_telegram_markdown(12345, "·", reply_markup={"inline_keyboard": [[{"text": "Y", "callback_data": "agent:yes"}]]})
        assert result is False

    @patch("httpx.post")
    @patch("apps.router.pending_queue._telegram_api_base", return_value="https://api.telegram.org/botTOKEN")
    def test_middle_dot_placeholder_delivers_keyboard(self, _mock_base, mock_post):
        """A middle-dot text + reply_markup should result in a sendMessage POST
        that includes the inline_keyboard — the core fix for FA-1036."""
        from apps.router.pending_queue import _send_telegram_markdown

        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_post.return_value = mock_resp

        keyboard = {"inline_keyboard": [[{"text": "Yes", "callback_data": "agent:confirm"}]]}
        result = _send_telegram_markdown(12345, "·", reply_markup=keyboard)

        assert result is True
        assert mock_post.called
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args.args[1]
        assert payload is not None
        assert payload.get("reply_markup") == keyboard
        assert payload.get("chat_id") == 12345


# ---------------------------------------------------------------------------
# TelegramPoller._send_rich_response — the live-poller delivery path
# ---------------------------------------------------------------------------

class PollerSendRichResponseButtonsOnlyTest(SimpleTestCase):
    """TelegramPoller._send_rich_response delivers keyboard even with no prose."""

    def _make_poller(self):
        """Build a minimal TelegramPoller stub sufficient to call _send_rich_response."""
        from apps.router.poller import TelegramPoller

        poller = object.__new__(TelegramPoller)
        # _api_base is a read-only property derived from bot_token.
        poller.bot_token = "TOKEN"
        poller._http = MagicMock()
        poller._send_message = MagicMock()
        poller._send_photo = MagicMock()
        poller._send_markdown = MagicMock()
        return poller

    def _make_tenant(self):
        """Minimal tenant stub — pii_entity_map=None avoids rehydrate_text import."""
        tenant = MagicMock()
        tenant.pii_entity_map = None
        tenant.id = "test-tenant-a28"
        return tenant

    def test_buttons_only_sends_keyboard(self):
        """Buttons-only response must call _send_message with reply_markup."""
        from apps.router.poller import TelegramPoller

        poller = self._make_poller()
        buttons_only = "[[button:Yes|confirm]][[button:No|deny]]"
        with patch("apps.router.output_guards.log_ascii_chart_leak"):
            with patch("apps.insights.markers.extract_and_record_insights", return_value=buttons_only):
                TelegramPoller._send_rich_response(poller, chat_id=12345, tenant=self._make_tenant(), text=buttons_only)

        assert poller._send_message.called, "_send_message must be called for buttons-only reply"
        call_kwargs = poller._send_message.call_args.kwargs
        rm = call_kwargs.get("reply_markup")
        assert rm is not None, "reply_markup must be passed to _send_message"
        assert "inline_keyboard" in rm, "reply_markup must contain inline_keyboard"
        keyboard_rows = rm["inline_keyboard"]
        # Two buttons → two rows
        assert len(keyboard_rows) == 2
        assert keyboard_rows[0][0]["callback_data"] == "agent:confirm"
        assert keyboard_rows[1][0]["callback_data"] == "agent:deny"
        # _send_markdown must NOT have been called (no prose to send)
        assert poller._send_markdown.call_count == 0

    def test_prose_and_buttons_sends_both(self):
        """Normal prose+buttons path must be unaffected by the fix."""
        from apps.router.poller import TelegramPoller

        poller = self._make_poller()
        # render_telegram_html is imported locally inside _send_rich_response so
        # we patch the canonical location (telegram_format module) instead.
        prose_and_buttons = "Pick one: [[button:A|opt_a]][[button:B|opt_b]]"
        with patch("apps.router.output_guards.log_ascii_chart_leak"):
            with patch("apps.insights.markers.extract_and_record_insights", return_value=prose_and_buttons):
                with patch("apps.router.telegram_format.render_telegram_html", return_value=["Pick one:"]):
                    TelegramPoller._send_rich_response(
                        poller, chat_id=12345, tenant=self._make_tenant(), text=prose_and_buttons
                    )

        assert poller._send_message.called
        # The final (and only) call must carry reply_markup
        last_call = poller._send_message.call_args
        assert last_call.kwargs.get("reply_markup") is not None

    def test_empty_text_no_buttons_no_send(self):
        """Truly empty text with no buttons sends nothing (unchanged pre-fix behavior)."""
        from apps.router.poller import TelegramPoller

        poller = self._make_poller()
        with patch("apps.router.output_guards.log_ascii_chart_leak"):
            with patch("apps.insights.markers.extract_and_record_insights", return_value=""):
                TelegramPoller._send_rich_response(poller, chat_id=12345, tenant=self._make_tenant(), text="")

        assert not poller._send_message.called
        assert not poller._send_markdown.called

    def test_buttons_only_placeholder_text_is_not_raw_marker(self):
        """The placeholder sent for buttons-only must not be a raw [[button:...]] string."""
        from apps.router.poller import TelegramPoller

        poller = self._make_poller()
        with patch("apps.router.output_guards.log_ascii_chart_leak"):
            with patch("apps.insights.markers.extract_and_record_insights", return_value="[[button:OK|ok]]"):
                TelegramPoller._send_rich_response(
                    poller, chat_id=12345, tenant=self._make_tenant(), text="[[button:OK|ok]]"
                )

        assert poller._send_message.called
        # The text argument (first positional after chat_id) must not contain [[button:
        sent_text = poller._send_message.call_args.args[1] if len(poller._send_message.call_args.args) > 1 else ""
        assert "[[button:" not in sent_text, "Raw [[button:...]] marker must not be sent to Telegram"
