"""Tests for the per-tenant speech-to-text vocabulary.

Regression guard for the "Rakuten" -> "Rocketen" voice-transcription garble
class. The incident itself entered via iOS on-device STT (Apple's recognizer;
no server-side audio path exists for iOS) — the app fixes that channel by
feeding ``SFSpeechRecognitionRequest.contextualStrings`` from
``GET /api/v1/chat/transcription-vocab/``. The two server-side Whisper channels
(Telegram poller, LINE webhook) are hardened against the same class by passing
the vocabulary as the Whisper ``prompt``.

The acoustic win itself can't be unit-tested (recognizers are mocked/remote),
so these lock in that (a) the vocabulary is assembled from the right sources
with budget caps, (b) both Whisper channels actually forward it, and (c) the
iOS endpoint requires auth and returns the same terms.
"""

import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from apps.router.transcription import build_transcription_prompt, collect_transcription_vocab

_VOCAB_URL = "/api/v1/chat/transcription-vocab/"


def _tenant(denylist=None, display_name="Michael Jones"):
    return SimpleNamespace(
        pii_denylist=denylist if denylist is not None else {},
        # SimpleNamespace has no `.workspaces` manager; the helper guards the
        # lookup and degrades to [] — exercising the no-workspace path.
        user=SimpleNamespace(display_name=display_name),
    )


class CollectTranscriptionVocabTests(SimpleTestCase):
    """The shared term collector both the Whisper prompt and the iOS endpoint use."""

    def test_none_tenant_returns_empty(self):
        self.assertEqual(collect_transcription_vocab(None), [])

    def test_terms_sourced_from_denylist_and_display_name(self):
        terms = collect_transcription_vocab(_tenant(denylist={"rakuten": {}}, display_name="Kiho Tanaka"))
        self.assertIn("Rakuten", terms)  # denylist key, title-cased proper noun
        self.assertIn("Kiho Tanaka", terms)

    def test_budget_caps_term_count(self):
        big = {f"brandterm{i:03d}": {} for i in range(500)}
        terms = collect_transcription_vocab(_tenant(denylist=big))
        self.assertLessEqual(len(terms), 48)

    def test_never_raises_on_malformed_tenant(self):
        # Denylist not a dict, no user attr — must degrade to [], never raise.
        self.assertEqual(collect_transcription_vocab(SimpleNamespace(pii_denylist=["oops"])), [])


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


class TranscriptionVocabEndpointTests(TestCase):
    """``GET /api/v1/chat/transcription-vocab/`` — the iOS consumer surface.

    Same JWT-authed surface as the other ``/api/v1/chat/`` endpoints; the app
    feeds the returned terms into ``SFSpeechRecognitionRequest.contextualStrings``.
    """

    def setUp(self):
        from apps.tenants.models import Tenant, User

        self.user = User.objects.create_user(
            username=f"vocab_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
        )
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            container_fqdn="oc-vocab.example.com",
            pii_denylist={"rakuten": {"reason": "manual"}, "sautai": {"reason": "arbiter"}},
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_requires_auth(self):
        resp = APIClient().get(_VOCAB_URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_returns_terms_shape(self):
        resp = self.client.get(_VOCAB_URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(set(body.keys()), {"terms"})
        self.assertIsInstance(body["terms"], list)
        self.assertIn("Rakuten", body["terms"])
        self.assertIn("Sautai", body["terms"])

    def test_includes_workspace_names(self):
        from apps.journal.models import Workspace

        Workspace.objects.create(tenant=self.tenant, name="Moonshot", slug="moonshot")
        terms = self.client.get(_VOCAB_URL).json()["terms"]
        self.assertIn("Moonshot", terms)

    def test_budget_capped(self):
        self.tenant.pii_denylist = {f"brandterm{i:03d}": {} for i in range(500)}
        self.tenant.save(update_fields=["pii_denylist"])
        terms = self.client.get(_VOCAB_URL).json()["terms"]
        self.assertLessEqual(len(terms), 48)

    def test_user_without_tenant_gets_404(self):
        from apps.tenants.models import User

        lone = User.objects.create_user(
            username=f"lone_{secrets.token_hex(4)}",
            email=f"{secrets.token_hex(4)}@example.com",
        )
        client = APIClient()
        client.force_authenticate(user=lone)
        resp = client.get(_VOCAB_URL)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": "no_tenant"})


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
