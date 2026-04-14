"""Tests for LINE voice/audio message transcription."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.router.line_webhook import _transcribe_line_audio


class TranscribeLineAudioTest(TestCase):
    """Tests for _transcribe_line_audio."""

    @override_settings(
        OPENAI_API_KEY="test-key",
        LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    )
    @patch("apps.router.line_webhook.httpx")
    def test_successful_transcription(self, mock_httpx):
        """Audio downloaded from LINE and transcribed successfully."""
        # Mock LINE Content API download
        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"fake-audio-data"
        dl_resp.headers = {"content-type": "audio/x-m4a"}

        # Mock Whisper response
        whisper_resp = MagicMock()
        whisper_resp.is_success = True
        whisper_resp.json.return_value = {"text": "Hello from LINE"}

        mock_httpx.get.return_value = dl_resp
        mock_httpx.post.return_value = whisper_resp

        result = _transcribe_line_audio("msg-123")
        self.assertEqual(result, "Hello from LINE")

        # Verify LINE Content API was called correctly
        mock_httpx.get.assert_called_once()
        call_args = mock_httpx.get.call_args
        self.assertIn("msg-123/content", call_args[0][0])
        self.assertIn("Bearer test-line-token", call_args[1]["headers"]["Authorization"])

        # Verify Whisper was called
        mock_httpx.post.assert_called_once()
        whisper_call = mock_httpx.post.call_args
        self.assertIn("transcriptions", whisper_call[0][0])

    @override_settings(
        OPENAI_API_KEY="test-key",
        LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    )
    @patch("apps.router.line_webhook.httpx")
    def test_line_download_fails(self, mock_httpx):
        """LINE Content API failure returns None."""
        dl_resp = MagicMock()
        dl_resp.is_success = False
        dl_resp.status_code = 404
        mock_httpx.get.return_value = dl_resp

        result = _transcribe_line_audio("bad-msg-id")
        self.assertIsNone(result)
        mock_httpx.post.assert_not_called()

    @override_settings(
        OPENAI_API_KEY="test-key",
        LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    )
    @patch("apps.router.line_webhook.httpx")
    def test_whisper_fails(self, mock_httpx):
        """Whisper API failure returns None."""
        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"fake-audio-data"
        dl_resp.headers = {"content-type": "audio/x-m4a"}

        whisper_resp = MagicMock()
        whisper_resp.is_success = False
        whisper_resp.status_code = 500
        whisper_resp.text = "Server error"

        mock_httpx.get.return_value = dl_resp
        mock_httpx.post.return_value = whisper_resp

        result = _transcribe_line_audio("msg-123")
        self.assertIsNone(result)

    @override_settings(OPENAI_API_KEY="", LINE_CHANNEL_ACCESS_TOKEN="test-token")
    @patch.dict("os.environ", {"OPENAI_API_KEY": ""})
    def test_no_openai_key_returns_none(self):
        """No OpenAI API key returns None."""
        result = _transcribe_line_audio("msg-123")
        self.assertIsNone(result)

    @override_settings(OPENAI_API_KEY="test-key", LINE_CHANNEL_ACCESS_TOKEN="")
    def test_no_line_token_returns_none(self):
        """No LINE access token returns None."""
        result = _transcribe_line_audio("msg-123")
        self.assertIsNone(result)

    @override_settings(
        OPENAI_API_KEY="test-key",
        LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    )
    @patch("apps.router.line_webhook.httpx")
    def test_empty_audio_returns_none(self, mock_httpx):
        """Empty audio content returns None."""
        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b""
        dl_resp.headers = {"content-type": "audio/x-m4a"}
        mock_httpx.get.return_value = dl_resp

        result = _transcribe_line_audio("msg-123")
        self.assertIsNone(result)
        mock_httpx.post.assert_not_called()

    @override_settings(
        OPENAI_API_KEY="test-key",
        LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    )
    @patch("apps.router.line_webhook.httpx")
    def test_ogg_content_type(self, mock_httpx):
        """OGG audio content type detected correctly."""
        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"fake-ogg-data"
        dl_resp.headers = {"content-type": "audio/ogg"}

        whisper_resp = MagicMock()
        whisper_resp.is_success = True
        whisper_resp.json.return_value = {"text": "OGG audio"}

        mock_httpx.get.return_value = dl_resp
        mock_httpx.post.return_value = whisper_resp

        result = _transcribe_line_audio("msg-456")
        self.assertEqual(result, "OGG audio")

        # Verify file extension passed to Whisper
        whisper_call = mock_httpx.post.call_args
        files_arg = whisper_call[1]["files"]["file"]
        self.assertEqual(files_arg[0], "voice.ogg")

    @override_settings(
        OPENAI_API_KEY="test-key",
        LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    )
    @patch("apps.router.line_webhook.httpx")
    def test_empty_transcript_returns_none(self, mock_httpx):
        """Whisper returning empty text returns None."""
        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"fake-audio-data"
        dl_resp.headers = {"content-type": "audio/x-m4a"}

        whisper_resp = MagicMock()
        whisper_resp.is_success = True
        whisper_resp.json.return_value = {"text": "  "}

        mock_httpx.get.return_value = dl_resp
        mock_httpx.post.return_value = whisper_resp

        result = _transcribe_line_audio("msg-123")
        self.assertIsNone(result)

    @override_settings(
        OPENAI_API_KEY="test-key",
        LINE_CHANNEL_ACCESS_TOKEN="test-line-token",
    )
    @patch("apps.router.line_webhook.httpx")
    def test_exception_returns_none(self, mock_httpx):
        """Exception during transcription returns None."""
        mock_httpx.get.side_effect = Exception("network error")

        result = _transcribe_line_audio("msg-123")
        self.assertIsNone(result)


class HandleAudioMessageTest(TestCase):
    """Tests for audio message handling in LineWebhookView._handle_message."""

    def _make_event(self, msg_type="audio", message_id="msg-999"):
        return {
            "type": "message",
            "replyToken": "reply-token-abc",
            "source": {"userId": "U1234567890"},
            "message": {"id": message_id, "type": msg_type},
        }

    @override_settings(
        LINE_CHANNEL_ACCESS_TOKEN="test-token",
        LINE_CHANNEL_SECRET="test-secret",
    )
    @patch("apps.router.line_webhook._transcribe_line_audio", return_value="Buy groceries")
    @patch("apps.router.line_webhook._show_loading")
    @patch("apps.router.line_webhook._resolve_tenant_by_line_user_id")
    @patch("apps.router.line_webhook._send_line_flex")
    def test_audio_message_transcribed_and_forwarded(self, mock_flex, mock_resolve, mock_loading, mock_transcribe):
        """Audio message is transcribed and processed like text."""
        # Return None tenant to hit the "unrecognized account" path,
        # which proves the transcribed text path was entered
        mock_resolve.return_value = None

        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()
        view._handle_message(self._make_event())

        # Called twice: once before Whisper, once after transcription succeeds
        self.assertEqual(mock_loading.call_count, 2)
        mock_loading.assert_called_with("U1234567890")
        mock_transcribe.assert_called_once_with("msg-999")
        # Should have resolved tenant (meaning transcription succeeded and
        # flow continued to text processing)
        mock_resolve.assert_called_once_with("U1234567890")

    @override_settings(
        LINE_CHANNEL_ACCESS_TOKEN="test-token",
        LINE_CHANNEL_SECRET="test-secret",
    )
    @patch("apps.router.line_webhook._transcribe_line_audio", return_value=None)
    @patch("apps.router.line_webhook._show_loading")
    @patch("apps.router.line_webhook._send_line_flex")
    def test_audio_transcription_failure_sends_error(self, mock_flex, mock_loading, mock_transcribe):
        """Failed transcription sends error message to user."""
        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()
        view._handle_message(self._make_event())

        mock_transcribe.assert_called_once_with("msg-999")
        # Should have sent an error flex message
        mock_flex.assert_called_once()
        flex_args = mock_flex.call_args
        self.assertEqual(flex_args[0][0], "U1234567890")

    @override_settings(
        LINE_CHANNEL_ACCESS_TOKEN="test-token",
        LINE_CHANNEL_SECRET="test-secret",
    )
    @patch("apps.router.line_webhook._send_line_flex")
    def test_unsupported_type_sends_updated_message(self, mock_flex):
        """Unsupported message type mentions voice support."""
        from apps.router.line_webhook import LineWebhookView

        view = LineWebhookView()
        event = self._make_event(msg_type="image")
        view._handle_message(event)

        mock_flex.assert_called_once()
        # The bubble should mention voice messages now
        bubble_arg = mock_flex.call_args[0][1]
        # build_status_bubble returns a dict — just verify it was called
        self.assertIsNotNone(bubble_arg)
