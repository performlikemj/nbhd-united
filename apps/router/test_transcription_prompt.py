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

import base64
import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse
from rest_framework.test import APIClient, APIRequestFactory

from apps.router.transcription import (
    InternalTranscriptionView,
    build_transcription_prompt,
    collect_transcription_vocab,
    transcribe_audio,
)

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

    def test_terms_sourced_only_from_denylist(self):
        terms = collect_transcription_vocab(_tenant(denylist={"rakuten": {}}, display_name="Kiho Tanaka"))
        self.assertIn("Rakuten", terms)  # denylist key, title-cased proper noun
        self.assertNotIn("Kiho Tanaka", terms)

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

    def test_display_name_excluded(self):
        prompt = build_transcription_prompt(_tenant(denylist={}, display_name="Kiho Tanaka"))
        self.assertIsNone(prompt)

    def test_deduplicates_case_insensitively(self):
        # Display names are excluded, so the denylist contributes one copy.
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

    def test_excludes_workspace_names(self):
        from apps.journal.models import Workspace

        Workspace.objects.create(tenant=self.tenant, name="Moonshot", slug="moonshot")
        terms = self.client.get(_VOCAB_URL).json()["terms"]
        self.assertNotIn("Moonshot", terms)

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


@override_settings(OPENROUTER_API_KEY="test-key", OPENROUTER_STT_MODEL="openai/whisper-large-v3-turbo")
class OpenRouterTranscriptionTests(SimpleTestCase):
    @patch("apps.router.transcription.requests.post")
    def test_host_body_and_zdr_policy(self, mock_post):
        response = MagicMock()
        response.json.return_value = {"text": "testing one two three"}
        mock_post.return_value = response

        result = transcribe_audio(b"audio", audio_format="ogg", tenant=_tenant(denylist={"rakuten": {}}))

        self.assertEqual(result, "testing one two three")
        self.assertEqual(mock_post.call_args.args[0], "https://openrouter.ai/api/v1/audio/transcriptions")
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "openai/whisper-large-v3-turbo")
        self.assertEqual(body["provider"], {"zdr": True, "data_collection": "deny"})
        self.assertEqual(body["input_audio"], {"data": base64.b64encode(b"audio").decode(), "format": "ogg"})
        self.assertNotIn("prompt", body)

    @patch("apps.router.transcription.requests.post", side_effect=requests.ConnectionError("offline"))
    def test_failure_has_no_direct_provider_fallback(self, mock_post):
        with self.assertRaises(requests.ConnectionError):
            transcribe_audio(b"audio", audio_format="m4a")

        self.assertEqual(mock_post.call_count, 1)
        self.assertNotIn("api.openai.com", mock_post.call_args.args[0])


class TelegramVoicePromptTests(SimpleTestCase):
    """The Telegram poller delegates audio to the shared transcription seam."""

    def setUp(self):
        from apps.router.poller import TelegramPoller

        self.poller = TelegramPoller.__new__(TelegramPoller)
        self.poller.bot_token = "test-token"
        self.poller._http = MagicMock()

    def _wire_success(self):
        get_file_resp = MagicMock(is_success=True)
        get_file_resp.json.return_value = {"result": {"file_path": "voice/f.ogg"}}
        dl_resp = MagicMock(is_success=True, content=b"audio")
        self.poller._http.post.return_value = get_file_resp
        self.poller._http.get.return_value = dl_resp

    @patch("apps.router.transcription.transcribe_audio", return_value="Rakuten meeting")
    def test_audio_passed_to_shared_helper(self, transcribe):
        self._wire_success()
        tenant = _tenant(denylist={"rakuten": {}})
        result = self.poller._transcribe_voice("file-id", tenant=tenant)

        self.assertEqual(result, "Rakuten meeting")
        transcribe.assert_called_once_with(b"audio", audio_format="ogg", tenant=tenant)

    @patch("apps.router.transcription.transcribe_audio", side_effect=requests.ConnectionError("offline"))
    def test_error_returns_none(self, _transcribe):
        self._wire_success()
        self.assertIsNone(self.poller._transcribe_voice("file-id"))


@override_settings(LINE_CHANNEL_ACCESS_TOKEN="line-token")
class LineVoicePromptTests(SimpleTestCase):
    """The LINE webhook delegates audio to the shared transcription seam."""

    def _wire(self, mock_httpx):
        dl_resp = MagicMock(is_success=True, content=b"audio")
        dl_resp.headers = {"content-type": "audio/x-m4a"}
        mock_httpx.get.return_value = dl_resp

    @patch("apps.router.transcription.transcribe_audio", return_value="Rakuten meeting")
    @patch("apps.router.line_webhook.httpx")
    def test_audio_passed_to_shared_helper(self, mock_httpx, transcribe):
        from apps.router.line_webhook import _transcribe_line_audio

        self._wire(mock_httpx)
        tenant = _tenant(denylist={"rakuten": {}})
        result = _transcribe_line_audio("msg-1", tenant=tenant)

        self.assertEqual(result, "Rakuten meeting")
        transcribe.assert_called_once_with(b"audio", audio_format="m4a", tenant=tenant)

    @patch("apps.router.transcription.transcribe_audio", side_effect=requests.ConnectionError("offline"))
    @patch("apps.router.line_webhook.httpx")
    def test_error_returns_none(self, mock_httpx, _transcribe):
        from apps.router.line_webhook import _transcribe_line_audio

        self._wire(mock_httpx)
        self.assertIsNone(_transcribe_line_audio("msg-1"))


class InternalTranscriptionViewTests(TestCase):
    def setUp(self):
        from apps.tenants.models import Tenant, User

        self.user = User.objects.create_user(username=f"internal_stt_{secrets.token_hex(4)}")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            internal_api_key="tenant-internal-key",
        )
        self.factory = APIRequestFactory()

    def _headers(self, key="tenant-internal-key"):
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def test_internal_transcription_url_resolves_to_view(self):
        self.assertEqual(reverse("internal-transcribe"), "/api/internal/transcribe/")
        match = resolve("/api/internal/transcribe/")
        self.assertIs(match.func.view_class, InternalTranscriptionView)

    def test_requires_internal_auth(self):
        request = self.factory.post(
            "/api/internal/transcribe/",
            {"file": SimpleUploadedFile("voice.wav", b"audio", content_type="audio/wav")},
            format="multipart",
        )

        response = InternalTranscriptionView.as_view()(request)

        self.assertEqual(response.status_code, 401)

    @patch("apps.router.transcription.transcribe_audio", return_value="testing one two three")
    def test_authenticated_multipart_calls_shared_helper(self, transcribe):
        request = self.factory.post(
            "/api/internal/transcribe/",
            {"file": SimpleUploadedFile("voice.wav", b"audio", content_type="audio/wav")},
            format="multipart",
            **self._headers(),
        )

        response = InternalTranscriptionView.as_view()(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"text": "testing one two three"})
        transcribe.assert_called_once_with(b"audio", audio_format="wav", tenant=self.tenant)

    @patch("apps.router.transcription.transcribe_audio", side_effect=requests.ConnectionError("offline"))
    def test_provider_error_is_content_free_and_fails_closed(self, _transcribe):
        request = self.factory.post(
            "/api/internal/transcribe/",
            {"input_audio": {"data": base64.b64encode(b"audio").decode(), "format": "ogg"}},
            format="json",
            **self._headers(),
        )

        with self.assertLogs("apps.router.transcription", level="WARNING") as logs:
            response = InternalTranscriptionView.as_view()(request)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["error"], "transcription_failed")
        self.assertNotIn("audio", "\n".join(logs.output))
        self.assertNotIn("offline", "\n".join(logs.output))
