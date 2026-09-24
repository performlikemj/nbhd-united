"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

import inspect

from django.test import SimpleTestCase
from google import genai
from google.genai import types

from apps.core import render


class GoogleGenaiSdkContractTest(SimpleTestCase):
    def test_38_request_preserves_metadata_on_the_wire(self):
        from google.genai import _common
        from google.genai.models import _GenerateContentParameters_to_mldev

        with genai.Client(api_key="offline-key") as client:
            request = render.gemini_tts_request("Breathe gently.", "Achernar", render.DEFAULT_MODEL, "unhurried")
            inspect.signature(client.models.generate_content).bind(**request)
            params = types._GenerateContentParameters(**request)
            wire = _GenerateContentParameters_to_mldev(client._api_client, params, None, params)
            wire = _common.convert_to_dict(wire)

        part = wire["contents"][0]["parts"][0]
        self.assertEqual(part["text"], "Breathe gently.")
        self.assertIn("unhurried", part["speechMetadata"]["style"])
        self.assertEqual(
            wire["generationConfig"]["speechConfig"]["voice_config"]["prebuilt_voice_config"]["voice_name"],
            "Achernar",
        )

    def test_client_constructor_and_generate_signature_accept_our_calls(self):
        client = genai.Client(api_key="offline-key", http_options=types.HttpOptions(timeout=1_000))
        config = types.GenerateContentConfig(response_modalities=["AUDIO"])

        inspect.signature(client.models.generate_content).bind(
            model="gemini-tts-model",
            contents="Speak this text",
            config=config,
        )

    def test_nested_speech_config_models_accept_our_kwargs(self):
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore"))
            ),
        )

        self.assertEqual(config.response_modalities, ["AUDIO"])
        self.assertEqual(config.speech_config.voice_config.prebuilt_voice_config.voice_name, "Kore")

    def test_real_response_models_keep_the_audio_attribute_path(self):
        response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[types.Part(inline_data=types.Blob(data=b"pcm", mime_type="audio/pcm"))]
                    )
                )
            ]
        )

        self.assertEqual(render._extract_audio(response), (b"pcm", "audio/pcm"))
