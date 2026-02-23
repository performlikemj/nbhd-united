"""Tests for proxy features: message splitting, photo forwarding, location passthrough."""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.router.poller import TelegramPoller


class MessageSplittingTest(TestCase):
    """Tests for _split_message."""

    def test_short_message_unchanged(self):
        result = TelegramPoller._split_message("Hello world")
        self.assertEqual(result, ["Hello world"])

    def test_exact_limit_unchanged(self):
        text = "x" * 4096
        result = TelegramPoller._split_message(text)
        self.assertEqual(len(result), 1)

    def test_splits_on_paragraph(self):
        text = "A" * 3000 + "\n\n" + "B" * 3000
        result = TelegramPoller._split_message(text, max_len=4096)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[0].startswith("A"))
        self.assertTrue(result[1].startswith("B"))

    def test_splits_on_newline(self):
        text = "A" * 3000 + "\n" + "B" * 3000
        result = TelegramPoller._split_message(text, max_len=4096)
        self.assertEqual(len(result), 2)

    def test_hard_cut_no_whitespace(self):
        text = "A" * 5000
        result = TelegramPoller._split_message(text, max_len=4096)
        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 4096)

    def test_multiple_chunks(self):
        text = "A" * 10000
        result = TelegramPoller._split_message(text, max_len=4096)
        self.assertEqual(len(result), 3)

    def test_empty_string(self):
        result = TelegramPoller._split_message("")
        self.assertEqual(result, [""])


class PhotoDownloadTest(TestCase):
    """Tests for _download_photo."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_successful_download(self):
        get_file_resp = MagicMock()
        get_file_resp.is_success = True
        get_file_resp.json.return_value = {"result": {"file_path": "photos/file.jpg"}}

        dl_resp = MagicMock()
        dl_resp.is_success = True
        dl_resp.content = b"\xff\xd8\xff\xe0"  # JPEG header bytes

        self.poller._http.post.return_value = get_file_resp
        self.poller._http.get.return_value = dl_resp

        message = {"photo": [
            {"file_id": "small", "file_size": 1000},
            {"file_id": "large", "file_size": 50000},
        ]}
        result = self.poller._download_photo(message)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("data:image/jpg;base64,"))

    def test_no_photos(self):
        result = self.poller._download_photo({})
        self.assertIsNone(result)

    def test_photo_too_large(self):
        message = {"photo": [
            {"file_id": "huge", "file_size": 10 * 1024 * 1024},
        ]}
        result = self.poller._download_photo(message)
        self.assertIsNone(result)

    def test_download_failure(self):
        resp = MagicMock()
        resp.is_success = False
        self.poller._http.post.return_value = resp

        message = {"photo": [{"file_id": "abc", "file_size": 1000}]}
        result = self.poller._download_photo(message)
        self.assertIsNone(result)


class LocationPassthroughTest(TestCase):
    """Tests for location message extraction."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_location(self):
        update = {"message": {"location": {"latitude": 34.6937, "longitude": 135.5023}}}
        result = self.poller._extract_message_text(update)
        self.assertIn("34.6937", result)
        self.assertIn("135.5023", result)
        self.assertIn("maps.google.com", result)
        self.assertIn("📍", result)

    def test_venue(self):
        update = {"message": {
            "location": {"latitude": 34.6937, "longitude": 135.5023},
            "venue": {"title": "Osaka Castle", "address": "1-1 Osakajo, Chuo-ku"},
        }}
        result = self.poller._extract_message_text(update)
        self.assertIn("Osaka Castle", result)
        self.assertIn("1-1 Osakajo", result)
        self.assertIn("34.6937", result)


class PhotoExtractionTest(TestCase):
    """Tests for photo message text extraction."""

    def setUp(self):
        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def test_photo_with_caption(self):
        update = {"message": {
            "photo": [{"file_id": "abc", "file_size": 1000}],
            "caption": "Check this out!",
        }}
        result = self.poller._extract_message_text(update)
        self.assertEqual(result, "Check this out!")

    def test_photo_without_caption(self):
        update = {"message": {
            "photo": [{"file_id": "abc", "file_size": 1000}],
        }}
        result = self.poller._extract_message_text(update)
        self.assertEqual(result, "User sent a photo")
