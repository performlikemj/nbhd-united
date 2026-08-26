"""Tests for voice message transcription."""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.router.poller import TelegramPoller


class TranscribeVoiceTest(TestCase):
    """Tests for TelegramPoller._transcribe_voice."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    @patch("apps.router.transcription.transcribe_audio", return_value="Hello world")
    def test_successful_transcription(self, mock_transcribe):
        """Voice file downloaded and transcribed successfully."""
        # Mock getFile response
        get_file_resp = MagicMock()
        get_file_resp.is_success = True
        get_file_resp.json.return_value = {"result": {"file_path": "voice/file.ogg"}}

        # Mock file download
        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"fake-audio-data"

        self.poller._http.post.return_value = get_file_resp
        self.poller._http.get.return_value = dl_resp

        result = self.poller._transcribe_voice("file-id-123")
        self.assertEqual(result, "Hello world")
        mock_transcribe.assert_called_once_with(b"fake-audio-data", audio_format="ogg", tenant=None)

    def test_getfile_fails(self):
        """getFile API failure returns None."""
        resp = MagicMock()
        resp.is_success = False
        resp.text = "Not Found"
        self.poller._http.post.return_value = resp

        result = self.poller._transcribe_voice("bad-file-id")
        self.assertIsNone(result)

    @patch("apps.router.transcription.transcribe_audio", side_effect=RuntimeError("upstream failed"))
    def test_openrouter_fails(self, mock_transcribe):
        """OpenRouter transcription failure returns None without fallback."""
        get_file_resp = MagicMock()
        get_file_resp.is_success = True
        get_file_resp.json.return_value = {"result": {"file_path": "voice/file.ogg"}}

        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"fake-audio-data"

        self.poller._http.post.return_value = get_file_resp
        self.poller._http.get.return_value = dl_resp

        result = self.poller._transcribe_voice("file-id-123")
        self.assertIsNone(result)
        mock_transcribe.assert_called_once()


class ExtractVoiceMessageTest(TestCase):
    """Tests for voice message extraction in _extract_message_text."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    @patch.object(TelegramPoller, "_transcribe_voice", return_value="Check the filters")
    def test_voice_message_transcribed(self, mock_transcribe):
        """Voice message returns transcribed text with prefix."""
        update = {
            "message": {
                "voice": {"file_id": "abc123", "duration": 5},
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertEqual(result, '🎤 Voice message: "Check the filters"')
        # Tenant is threaded through so voice transcription can hint Whisper;
        # the default caller here has no tenant, so it forwards None.
        mock_transcribe.assert_called_once_with("abc123", tenant=None)

    @patch.object(TelegramPoller, "_transcribe_voice", return_value=None)
    def test_voice_transcription_fails(self, mock_transcribe):
        """Failed transcription returns fallback text."""
        update = {
            "message": {
                "voice": {"file_id": "abc123", "duration": 5},
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("couldn't transcribe", result)


class TypingIndicatorTest(TestCase):
    """Tests for typing indicator."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_send_typing(self):
        """_send_typing calls sendChatAction."""
        self.poller._send_typing(12345)
        call_args = self.poller._http.post.call_args
        self.assertIn("sendChatAction", call_args[0][0])
        self.assertEqual(call_args[1]["json"]["chat_id"], 12345)
        self.assertEqual(call_args[1]["json"]["action"], "typing")
