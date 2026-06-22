"""Audit regression tests for C50 / FA-0814.

Verifies that _send_apology_for_dropped_message sends a Telegram message
when the buffered message channel is TELEGRAM, matching the behaviour
already present for the LINE channel.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase


class SendApologyTelegramTest(TestCase):
    """FA-0814: Telegram users must receive a dropped-message apology."""

    def _make_tenant(self, telegram_chat_id=12345678):
        user = MagicMock()
        user.telegram_chat_id = telegram_chat_id
        user.language = "en"
        tenant = MagicMock()
        tenant.user = user
        tenant.id = "00000000-0000-0000-0000-000000000001"
        return tenant

    def _make_msg(self, channel, user_text="hello"):

        msg = MagicMock()
        msg.channel = channel
        msg.user_text = user_text
        return msg

    def test_telegram_channel_calls_send_telegram_message(self):
        """Dropped Telegram message triggers send_telegram_message, not a no-op."""
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message
        from apps.router.models import BufferedMessage

        tenant = self._make_tenant()
        msg = self._make_msg(BufferedMessage.Channel.TELEGRAM, user_text="my question")

        with patch("apps.router.services.send_telegram_message", return_value=True) as mock_send:
            _send_apology_for_dropped_message(tenant, msg)

        mock_send.assert_called_once()
        call_args = mock_send.call_args
        # First positional arg must be the chat id
        assert call_args[0][0] == 12345678
        # Second positional arg must be a non-empty string (the apology text)
        assert isinstance(call_args[0][1], str) and len(call_args[0][1]) > 0

    def test_telegram_missing_chat_id_returns_early(self):
        """No send attempt when the user has no telegram_chat_id (mirrors LINE guard)."""
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message
        from apps.router.models import BufferedMessage

        tenant = self._make_tenant(telegram_chat_id=None)
        msg = self._make_msg(BufferedMessage.Channel.TELEGRAM)

        with patch("apps.router.services.send_telegram_message") as mock_send:
            _send_apology_for_dropped_message(tenant, msg)

        mock_send.assert_not_called()

    def test_telegram_send_failure_does_not_raise(self):
        """A send exception is logged but must not propagate (delivery accounting must continue)."""
        from apps.orchestrator.hibernation import _send_apology_for_dropped_message
        from apps.router.models import BufferedMessage

        tenant = self._make_tenant()
        msg = self._make_msg(BufferedMessage.Channel.TELEGRAM)

        with patch(
            "apps.router.services.send_telegram_message", side_effect=RuntimeError("network error")
        ):
            # Must not raise
            _send_apology_for_dropped_message(tenant, msg)
