"""Tests for Phase 2 proxy features: reply context, documents, video metadata, contacts, forwards."""

from unittest.mock import MagicMock

from django.test import TestCase

from apps.router.poller import TelegramPoller


class ReplyContextTest(TestCase):
    """Tests for reply-to context extraction."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_reply_to_bot_message(self):
        update = {
            "message": {
                "text": "yes please",
                "reply_to_message": {
                    "text": "Would you like me to set up reminders?",
                    "from": {"is_bot": True, "first_name": "NBHD Bot"},
                },
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("Replying to", result)
        self.assertIn("reminders", result)
        self.assertIn("yes please", result)

    def test_reply_to_user_message_ignored(self):
        update = {
            "message": {
                "text": "agree",
                "reply_to_message": {
                    "text": "Let's go hiking",
                    "from": {"is_bot": False, "first_name": "Alice"},
                },
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertEqual(result, "agree")  # No reply prefix

    def test_no_reply(self):
        update = {"message": {"text": "hello"}}
        result = self.poller._extract_message_text(update)
        self.assertEqual(result, "hello")

    def test_long_reply_truncated(self):
        update = {
            "message": {
                "text": "ok",
                "reply_to_message": {
                    "text": "A" * 500,
                    "from": {"is_bot": True},
                },
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("…", result)
        # Original 500 chars should be truncated to 200
        self.assertLess(len(result), 300)


class DocumentTest(TestCase):
    """Tests for document handling."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_text_file_downloaded(self):
        get_file_resp = MagicMock()
        get_file_resp.is_success = True
        get_file_resp.json.return_value = {"result": {"file_path": "documents/notes.txt"}}

        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"Hello world\nThis is my notes file."

        self.poller._http.post.return_value = get_file_resp
        self.poller._http.get.return_value = dl_resp

        update = {
            "message": {
                "document": {
                    "file_id": "abc123",
                    "file_name": "notes.txt",
                    "mime_type": "text/plain",
                    "file_size": 100,
                }
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("notes.txt", result)
        self.assertIn("Hello world", result)
        self.assertIn("📄", result)

    def test_unsupported_format(self):
        update = {
            "message": {
                "document": {
                    "file_id": "abc123",
                    "file_name": "photo.psd",
                    "mime_type": "image/vnd.adobe.photoshop",
                    "file_size": 5000,
                }
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("photo.psd", result)
        self.assertIn("image/vnd.adobe.photoshop", result)

    def test_too_large(self):
        update = {
            "message": {
                "document": {
                    "file_id": "abc123",
                    "file_name": "huge.csv",
                    "mime_type": "text/csv",
                    "file_size": 15 * 1024 * 1024,
                }
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("too large", result)

    def test_json_file(self):
        get_file_resp = MagicMock()
        get_file_resp.is_success = True
        get_file_resp.json.return_value = {"result": {"file_path": "documents/data.json"}}

        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b'{"key": "value"}'

        self.poller._http.post.return_value = get_file_resp
        self.poller._http.get.return_value = dl_resp

        update = {
            "message": {
                "document": {
                    "file_id": "abc123",
                    "file_name": "data.json",
                    "mime_type": "application/json",
                    "file_size": 50,
                }
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn('"key"', result)


class VideoMetadataTest(TestCase):
    """Tests for video metadata extraction."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_video_with_metadata(self):
        update = {
            "message": {
                "video": {
                    "duration": 30,
                    "file_size": 5 * 1024 * 1024,
                }
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("30s", result)
        self.assertIn("5.0 MB", result)

    def test_video_with_caption(self):
        update = {
            "message": {
                "video": {"duration": 10, "file_size": 1024 * 1024},
                "caption": "Check this out!",
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("Check this out!", result)
        self.assertIn("10s", result)


class ContactTest(TestCase):
    """Tests for contact sharing."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_contact(self):
        update = {
            "message": {
                "contact": {
                    "first_name": "John",
                    "last_name": "Doe",
                    "phone_number": "+1234567890",
                }
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("John Doe", result)
        self.assertIn("+1234567890", result)
        self.assertIn("📇", result)


class ForwardDetectionTest(TestCase):
    """Tests for forwarded message detection."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_forwarded_from_user(self):
        update = {
            "message": {
                "text": "Interesting article about AI",
                "forward_from": {"first_name": "Alice"},
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("Forwarded from Alice", result)
        self.assertIn("Interesting article", result)

    def test_forwarded_from_channel(self):
        update = {
            "message": {
                "text": "Breaking news!",
                "forward_from_chat": {"title": "Tech News Channel"},
            }
        }
        result = self.poller._extract_message_text(update)
        self.assertIn("Tech News Channel", result)
        self.assertIn("Breaking news", result)
