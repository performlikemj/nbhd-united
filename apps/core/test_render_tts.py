"""Offline request and audio-format regressions for Gemini TTS."""

import io
import shutil
import tempfile
import wave
from pathlib import Path
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from google.genai import types

from apps.core import render


def wav_bytes(pcm, *, rate=24000, channels=1, width=2):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setframerate(rate)
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.writeframes(pcm)
    return output.getvalue()


def response(data, mime="audio/wav"):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    parts=[
                        types.Part(inline_data=types.Blob(data=data, mime_type=mime)),
                    ]
                )
            )
        ]
    )


class GeminiRequestTests(SimpleTestCase):
    def test_38_transcript_is_verbatim_and_delivery_is_metadata(self):
        for model in (render.DEFAULT_MODEL, "models/gemini-3.8-flash-tts"):
            with self.subTest(model=model):
                request = render.gemini_tts_request("Breathe gently.", "Achernar", model, "warm; reassuring")
                part = request["contents"][0].parts[0]
                self.assertEqual(part.text, "Breathe gently.")
                self.assertIn("soft, calm, slow, soothing meditation-guide voice", part.speech_metadata.style)
                self.assertIn("warm; reassuring", part.speech_metadata.style)
                self.assertIsNone(request["config"].temperature)
                self.assertEqual(
                    request["config"].speech_config.voice_config.prebuilt_voice_config.voice_name, "Achernar"
                )
                self.assertEqual(request["model"], model)

    def test_legacy_models_keep_exact_prompt_and_voice(self):
        for model in ("gemini-2.5-flash-preview-tts", "gemini-3.1-flash-tts-preview"):
            with self.subTest(model=model):
                request = render.gemini_tts_request("Breathe gently.", "Kore", model, "warm; reassuring")
                self.assertEqual(
                    request["contents"],
                    "Read the following aloud in a soft, calm, slow, soothing "
                    "meditation-guide voice. warm; reassuring. Do not read these instructions aloud.\n\n"
                    "Breathe gently.",
                )
                self.assertEqual(request["model"], model)
                self.assertEqual(request["config"].speech_config.voice_config.prebuilt_voice_config.voice_name, "Kore")

    def test_pipeline_combines_global_and_segment_tones(self):
        manifest = {
            "global_tone": "warm",
            "phases": [
                {
                    "name": name,
                    "target_seconds": 5,
                    "segments": [{"type": "speech", "text": "Breathe gently.", "tone": "reassuring"}],
                }
                for name in ("arrival", "settle", "closing")
            ],
        }
        client = Mock()
        client.models.generate_content.return_value = response(wav_bytes(b"\0\0" * 24000))

        def master(wavs, workdir, mp3, ogg):
            mp3.write_bytes(b"mp3")

        with (
            patch.object(render, "make_gemini_client", return_value=client),
            patch.object(render, "_ffprobe_seconds", return_value=1),
            patch.object(render, "_bake_fades"),
            patch.object(render, "_concat_and_master", side_effect=master),
        ):
            result = render.render_manifest_to_audio(
                manifest,
                voice="Achernar",
                model=render.DEFAULT_MODEL,
                api_key="offline-key",
                concurrency=1,
                want_ogg=False,
            )
        self.assertEqual(result.failed_count, 0)
        self.assertEqual(client.models.generate_content.call_count, 3)
        for call in client.models.generate_content.call_args_list:
            part = call.kwargs["contents"][0].parts[0]
            self.assertEqual(part.text, "Breathe gently.")
            self.assertIn("warm; reassuring", part.speech_metadata.style)

    def test_38_transcript_still_passes_through_redaction(self):
        client = Mock()
        client.models.generate_content.side_effect = RuntimeError("stop")
        with (
            patch("apps.pii.egress.redact_known_values", return_value="Breathe, [PERSON_1].") as redact,
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            render.render_gemini_segment(
                client, "Breathe, Alice.", "Achernar", render.DEFAULT_MODEL, "calm", Path("unused.wav"), attempts=1
            )
        redact.assert_called_once()
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["contents"][0].parts[0].text, "Breathe, [PERSON_1]."
        )


class GeminiAudioTests(SimpleTestCase):
    def test_wav_and_pcm_have_identical_samples_without_double_header(self):
        pcm = b"\x01\x02" * 24000
        for data, mime in (
            (wav_bytes(pcm), "audio/wav"),
            (wav_bytes(pcm), "audio/l16"),
            (wav_bytes(pcm), ""),
            (pcm, "audio/L16;codec=pcm;rate=24000"),
            (pcm, "audio/pcm"),
        ):
            with self.subTest(mime=mime, wav=data.startswith(b"RIFF")), tempfile.TemporaryDirectory() as tmp:
                dst = Path(tmp) / "audio.wav"
                render._write_audio_wav(data, mime, dst)
                with wave.open(str(dst), "rb") as wav:
                    self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))
                    self.assertEqual(wav.getnframes(), 24000)
                    self.assertEqual(wav.readframes(wav.getnframes()), pcm)

    @skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_noncanonical_wav_is_converted(self):
        for rate, channels, width in ((48000, 2, 2), (16000, 1, 1), (24000, 1, 3)):
            with self.subTest(rate=rate, channels=channels, width=width), tempfile.TemporaryDirectory() as tmp:
                dst = Path(tmp) / "audio.wav"
                data = wav_bytes(b"\0" * rate * channels * width, rate=rate, channels=channels, width=width)
                render._write_audio_wav(data, "audio/wav", dst)
                with wave.open(str(dst), "rb") as wav:
                    self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (24000, 1, 2))
                    self.assertAlmostEqual(wav.getnframes() / 24000, 1, places=2)
                self.assertFalse(dst.with_suffix(".source.wav").exists())

    @skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_malformed_wav_never_falls_back_to_pcm(self):
        for data, mime in ((b"RIFFbroken", "audio/l16"), (b"not a wav", "audio/wav")):
            with self.subTest(mime=mime), tempfile.TemporaryDirectory() as tmp:
                dst = Path(tmp) / "audio.wav"
                with self.assertRaises(RuntimeError):
                    render._write_audio_wav(data, mime, dst)
                self.assertFalse(dst.exists())

    def test_segment_normalizes_response_before_fades(self):
        pcm = b"\x01\x02" * 24000
        for data, mime in ((wav_bytes(pcm), "audio/wav"), (pcm, "audio/l16")):
            with self.subTest(mime=mime), tempfile.TemporaryDirectory() as tmp:
                client = Mock()
                client.models.generate_content.return_value = response(data, mime)

                def check_fades(src, dst):
                    with wave.open(str(src), "rb") as wav:
                        self.assertEqual(wav.readframes(wav.getnframes()), pcm)

                with (
                    patch.object(render, "_ffprobe_seconds", return_value=1),
                    patch.object(render, "_bake_fades", side_effect=check_fades) as fades,
                ):
                    render.render_gemini_segment(
                        client,
                        "Breathe gently.",
                        "Achernar",
                        render.DEFAULT_MODEL,
                        "calm",
                        Path(tmp) / "speech.wav",
                        attempts=1,
                    )
                fades.assert_called_once()

    def test_empty_or_blocked_response_has_no_audio(self):
        for resp in (None, types.GenerateContentResponse(), response(b"")):
            self.assertIsNone(render._extract_audio(resp))
