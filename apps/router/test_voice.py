"""Tests for voice message transcription."""
from unittest.mock import MagicMock, patch, PropertyMock

from django.test import TestCase, override_settings

from apps.router.poller import TelegramPoller


class TranscribeVoiceTest(TestCase):
    """Tests for TelegramPoller._transcribe_voice."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    @override_settings(OPENAI_API_KEY="test-key")
    def test_successful_transcription(self):
        """Voice file downloaded and transcribed successfully."""
        # Mock getFile response
        get_file_resp = MagicMock()
        get_file_resp.is_success = True
        get_file_resp.json.return_value = {"result": {"file_path": "voice/file.ogg"}}

        # Mock file download
        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"fake-audio-data"

        # Mock Whisper response
        whisper_resp = MagicMock()
        whisper_resp.is_success = True
        whisper_resp.json.return_value = {"text": "Hello world"}

        self.poller._http.post.side_effect = [get_file_resp, whisper_resp]
        self.poller._http.get.return_value = dl_resp

        result = self.poller._transcribe_voice("file-id-123")
        self.assertEqual(result, "Hello world")

    @override_settings(OPENAI_API_KEY="test-key")
    def test_getfile_fails(self):
        """getFile API failure returns None."""
        resp = MagicMock()
        resp.is_success = False
        resp.text = "Not Found"
        self.poller._http.post.return_value = resp

        result = self.poller._transcribe_voice("bad-file-id")
        self.assertIsNone(result)

    @override_settings(OPENAI_API_KEY="test-key")
    def test_whisper_fails(self):
        """Whisper API failure returns None."""
        get_file_resp = MagicMock()
        get_file_resp.is_success = True
        get_file_resp.json.return_value = {"result": {"file_path": "voice/file.ogg"}}

        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"fake-audio-data"

        whisper_resp = MagicMock()
        whisper_resp.is_success = False
        whisper_resp.status_code = 500
        whisper_resp.text = "Server error"

        self.poller._http.post.side_effect = [get_file_resp, whisper_resp]
        self.poller._http.get.return_value = dl_resp

        result = self.poller._transcribe_voice("file-id-123")
        self.assertIsNone(result)

    def test_no_api_key_returns_none(self):
        """No OpenAI API key returns None."""
        with self.settings(OPENAI_API_KEY=""):
            result = self.poller._transcribe_voice("file-id-123")
            self.assertIsNone(result)


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
        mock_transcribe.assert_called_once_with("abc123")

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
