"""Tests for the per-tenant Whisper vocabulary hint.

Regression guard for the "Rakuten" -> "Rocketen" voice-transcription garble:
transcription now passes the tenant's own known non-PII vocabulary as the
Whisper ``prompt`` so distinctive brand/project names decode consistently. The
acoustic win itself can't be unit-tested (Whisper is mocked), so these lock in
that (a) the hint is assembled from the right sources and (b) both voice
channels actually forward it to the Whisper request.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from apps.router.transcription import build_transcription_prompt


def _tenant(denylist=None, display_name="Michael Jones"):
    return SimpleNamespace(
        pii_denylist=denylist if denylist is not None else {},
        # SimpleNamespace has no `.workspaces` manager; the helper guards the
        # lookup and degrades to [] — exercising the no-workspace path.
        user=SimpleNamespace(display_name=display_name),
    )


class BuildTranscriptionPromptTests(SimpleTestCase):
    def test_none_tenant_returns_none(self):
        self.assertIsNone(build_transcription_prompt(None))

    def test_no_vocabulary_returns_none(self):
        # Empty denylist + no display name + no workspaces => nothing to hint.
        self.assertIsNone(build_transcription_prompt(_tenant(denylist={}, display_name="")))

    def test_denylisted_brand_appears_titlecased(self):
        # "rakuten" is exactly where the brand lands once denylisted; the hint
        # must surface it as a proper noun so Whisper stops hearing "Rocketen".
        prompt = build_transcription_prompt(_tenant(denylist={"rakuten": {}, "sautai": {}}))
        self.assertIsNotNone(prompt)
        self.assertIn("Rakuten", prompt)
        self.assertIn("Sautai", prompt)

    def test_display_name_included(self):
        prompt = build_transcription_prompt(_tenant(denylist={}, display_name="Kiho Tanaka"))
        self.assertIsNotNone(prompt)
        self.assertIn("Kiho Tanaka", prompt)

    def test_deduplicates_case_insensitively(self):
        # Denylist "rakuten" (-> "Rakuten") and a display name "Rakuten" must not
        # both appear.
        prompt = build_transcription_prompt(_tenant(denylist={"rakuten": {}}, display_name="Rakuten"))
        self.assertEqual(prompt.count("Rakuten"), 1)

    def test_budget_caps_term_count(self):
        big = {f"brandterm{i:03d}": {} for i in range(500)}
        prompt = build_transcription_prompt(_tenant(denylist=big))
        # Well under Whisper's 224-token ceiling.
        self.assertLessEqual(len(prompt), 900)

    def test_never_raises_on_malformed_tenant(self):
        # Denylist not a dict, no user attr — must degrade to None, never raise.
        self.assertIsNone(build_transcription_prompt(SimpleNamespace(pii_denylist=["oops"])))


class TelegramVoicePromptTests(SimpleTestCase):
    """The Telegram poller forwards the hint into the Whisper request `data`."""

    def setUp(self):
        from apps.router.poller import TelegramPoller

        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def _wire_success(self):
        get_file_resp = MagicMock(is_success=True)
        get_file_resp.json.return_value = {"result": {"file_path": "voice/f.ogg"}}
        dl_resp = MagicMock(is_success=True, content=b"audio")
        whisper_resp = MagicMock(is_success=True)
        whisper_resp.json.return_value = {"text": "Rakuten meeting"}
        self.poller._http.post.side_effect = [get_file_resp, whisper_resp]
        self.poller._http.get.return_value = dl_resp

    @override_settings(OPENAI_API_KEY="test-key")
    def test_prompt_passed_when_tenant_has_vocab(self):
        self._wire_success()
        tenant = _tenant(denylist={"rakuten": {}})
        self.poller._transcribe_voice("file-id", tenant=tenant)

        whisper_call = self.poller._http.post.call_args_list[1]
        data = whisper_call.kwargs["data"]
        self.assertEqual(data["model"], "whisper-1")
        self.assertIn("prompt", data)
        self.assertIn("Rakuten", data["prompt"])

    @override_settings(OPENAI_API_KEY="test-key")
    def test_no_prompt_key_without_tenant(self):
        self._wire_success()
        self.poller._transcribe_voice("file-id")  # backwards-compatible call

        data = self.poller._http.post.call_args_list[1].kwargs["data"]
        self.assertEqual(data["model"], "whisper-1")
        self.assertNotIn("prompt", data)


@override_settings(OPENAI_API_KEY="test-key", LINE_CHANNEL_ACCESS_TOKEN="line-token")
class LineVoicePromptTests(SimpleTestCase):
    """The LINE webhook forwards the hint into the Whisper request `data`."""

    def _wire(self, mock_httpx):
        dl_resp = MagicMock(is_success=True, content=b"audio")
        dl_resp.headers = {"content-type": "audio/x-m4a"}
        whisper_resp = MagicMock(is_success=True)
        whisper_resp.json.return_value = {"text": "Rakuten meeting"}
        mock_httpx.get.return_value = dl_resp
        mock_httpx.post.return_value = whisper_resp

    @patch("apps.router.line_webhook.httpx")
    def test_prompt_passed_when_tenant_has_vocab(self, mock_httpx):
        from apps.router.line_webhook import _transcribe_line_audio

        self._wire(mock_httpx)
        _transcribe_line_audio("msg-1", tenant=_tenant(denylist={"rakuten": {}}))

        data = mock_httpx.post.call_args.kwargs["data"]
        self.assertEqual(data["model"], "whisper-1")
        self.assertIn("prompt", data)
        self.assertIn("Rakuten", data["prompt"])

    @patch("apps.router.line_webhook.httpx")
    def test_no_prompt_key_without_tenant(self, mock_httpx):
        from apps.router.line_webhook import _transcribe_line_audio

        self._wire(mock_httpx)
        _transcribe_line_audio("msg-1")  # backwards-compatible call

        data = mock_httpx.post.call_args.kwargs["data"]
        self.assertNotIn("prompt", data)
