"""Core pillar tests — render engine (pure math + validation), the render
orchestration (mocked TTS/ffmpeg), notify-on-ready, manifest validation at the
runtime boundary, and a real-ffmpeg stitch test (skipped when ffmpeg is absent).

Live Gemini TTS is never called in CI; the orchestration tests mock the engine,
and the real-ffmpeg test renders placeholder tones (no key, no network).
"""

from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from unittest import TestCase as UnitTestCase
from unittest import skipUnless
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core import compose, render, services
from apps.core.models import CoreProfile, MeditationSession, MeditationStatus
from apps.lessons.models import Lesson, StarJournalEntry, TutoringSession
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _valid_manifest(*, total: int = 600) -> dict:
    """A minimal manifest that passes ``validate_manifest`` (canonical arc)."""
    return {
        "schema_version": 1,
        "title": "Letting go",
        "theme": "release work tension",
        "voice": "Achernar",
        "global_tone": "soft, slow, warm",
        "total_target_seconds": total,
        "ambient": None,
        "phases": [
            {
                "name": "arrival",
                "target_seconds": 60,
                "segments": [
                    {"type": "speech", "text": "Welcome. Let yourself arrive.", "tone": "warm"},
                    {"type": "silence", "seconds": 8},
                    {"type": "silence", "seconds": "flex"},
                ],
            },
            {
                "name": "breath_anchor",
                "target_seconds": 75,
                "segments": [
                    {"type": "speech", "text": "Notice your breath.", "tone": "calm"},
                    {"type": "silence", "seconds": "flex"},
                ],
            },
            {
                "name": "body_scan",
                "target_seconds": 150,
                "segments": [
                    {"type": "speech", "text": "Soften your shoulders.", "tone": "soft"},
                    {"type": "silence", "seconds": "flex"},
                ],
            },
            {
                "name": "core_practice",
                "target_seconds": 210,
                "segments": [
                    {"type": "speech", "text": "Set down what you carry.", "tone": "gentle"},
                    {"type": "silence", "seconds": "flex"},
                ],
            },
            {
                "name": "integration",
                "target_seconds": 60,
                "segments": [
                    {"type": "speech", "text": "Widen your awareness.", "tone": "calm"},
                    {"type": "silence", "seconds": "flex"},
                ],
            },
            {
                "name": "closing",
                "target_seconds": 45,
                "segments": [
                    {"type": "speech", "text": "Bring this stillness with you.", "tone": "warm"},
                ],
            },
        ],
    }


# ═════════════════════════════════════════════════════════════════════
# 1. Pure: manifest validation
# ═════════════════════════════════════════════════════════════════════


class ManifestValidationTests(UnitTestCase):
    def test_valid_manifest_has_no_errors(self):
        self.assertEqual(render.validate_manifest(_valid_manifest()), [])

    def test_non_dict_rejected(self):
        self.assertTrue(render.validate_manifest("nope"))
        self.assertTrue(render.validate_manifest(None))

    def test_empty_phases_rejected(self):
        self.assertTrue(render.validate_manifest({"phases": []}))

    def test_wrong_phase_order_rejected(self):
        m = _valid_manifest()
        m["phases"][0]["name"], m["phases"][1]["name"] = "breath_anchor", "arrival"
        errors = render.validate_manifest(m)
        self.assertTrue(any("exactly, in order" in e for e in errors))

    def test_silence_out_of_range_rejected(self):
        m = _valid_manifest()
        m["phases"][0]["segments"][1]["seconds"] = 200  # > SILENCE_MAX (150)
        errors = render.validate_manifest(m)
        self.assertTrue(any("out of" in e for e in errors))

    def test_long_explicit_silence_allowed(self):
        # A minutes-long hold is valid now (holistic pacing).
        m = _valid_manifest()
        m["phases"][3]["segments"] = [
            {"type": "speech", "text": "Set it down.", "tone": "gentle"},
            {"type": "silence", "seconds": 120},
        ]
        self.assertEqual(render.validate_manifest(m), [])

    def test_phase_without_flex_is_allowed(self):
        # A phase can pin its own length with explicit silences — no flex required.
        m = _valid_manifest()
        m["phases"][0]["segments"] = [
            {"type": "speech", "text": "Welcome. Let yourself arrive.", "tone": "warm"},
            {"type": "silence", "seconds": 50},
        ]
        self.assertEqual(render.validate_manifest(m), [])

    def test_silent_non_closing_phase_is_allowed(self):
        # A phase can be pure held space — as long as the sit has enough spoken
        # anchors overall and closing still lands on speech.
        m = _valid_manifest()
        m["phases"][2]["segments"] = [{"type": "silence", "seconds": 90}]  # body_scan: no words
        self.assertEqual(render.validate_manifest(m), [])

    def test_too_few_speech_segments_rejected(self):
        m = _valid_manifest()
        # Strip speech from 3 phases → only 3 spoken segments remain (< MIN 4).
        for i in (0, 1, 2):
            m["phases"][i]["segments"] = [{"type": "silence", "seconds": 40}]
        errors = render.validate_manifest(m)
        self.assertTrue(any("too few spoken" in e for e in errors))

    def test_too_many_speech_segments_rejected(self):
        m = _valid_manifest()
        # Pile 12 extra spoken cues into core_practice → well over MAX (12).
        m["phases"][3]["segments"] = [
            {"type": "speech", "text": f"Breathe, line {i}.", "tone": "soft"} for i in range(12)
        ] + [{"type": "silence", "seconds": "flex"}]
        errors = render.validate_manifest(m)
        self.assertTrue(any("too many spoken" in e for e in errors))

    def test_closing_not_ending_on_speech_rejected(self):
        m = _valid_manifest()
        m["phases"][-1]["segments"].append({"type": "silence", "seconds": 5})
        errors = render.validate_manifest(m)
        self.assertTrue(any("END on" in e for e in errors))

    def test_empty_speech_rejected(self):
        m = _valid_manifest()
        m["phases"][0]["segments"][0]["text"] = "   "
        errors = render.validate_manifest(m)
        self.assertTrue(any("empty speech" in e for e in errors))

    def test_overlong_speech_rejected(self):
        m = _valid_manifest()
        m["phases"][0]["segments"][0]["text"] = "word " * 200  # > MAX_SPEECH_CHARS
        errors = render.validate_manifest(m)
        self.assertTrue(any("too long" in e for e in errors))

    def test_missing_target_seconds_rejected(self):
        m = _valid_manifest()
        del m["phases"][2]["target_seconds"]
        errors = render.validate_manifest(m)
        self.assertTrue(any("target_seconds" in e for e in errors))

    def test_overlong_manifest_rejected(self):
        # Uncapped explicit silences that push the estimate far past the target are
        # rejected (the runaway-length guard) — otherwise the sit renders ~25 min
        # for a 10-min target and strands the player. Each silence is individually
        # legal (≤150s) and the speech count stays in band; only the TOTAL is over.
        m = _valid_manifest()
        for phase in m["phases"][:-1]:
            phase["segments"] = [
                {"type": "speech", "text": "Rest here.", "tone": "soft"},
                {"type": "silence", "seconds": 150},
                {"type": "silence", "seconds": 150},
                {"type": "silence", "seconds": 150},
            ]
        errors = render.validate_manifest(m)
        self.assertTrue(any("exceeds the ceiling" in e for e in errors), errors)

    def test_target_length_sit_within_ceiling_allowed(self):
        # A sit a little over target (≤1.3x) is fine — TTS length varies; we only
        # reject genuine runaways.
        m = _valid_manifest()
        m["phases"][3]["segments"] = [
            {"type": "speech", "text": "Set it down.", "tone": "gentle"},
            {"type": "silence", "seconds": 150},
        ]
        self.assertEqual(render.validate_manifest(m), [])


# ═════════════════════════════════════════════════════════════════════
# 2. Pure: planning + timing math
# ═════════════════════════════════════════════════════════════════════


class RenderMathTests(UnitTestCase):
    def test_reconcile_normal(self):
        # target 60, 10s speech, 8s fixed, 2 flex -> (60-18)/2 = 21 each
        self.assertAlmostEqual(render.reconcile_phase_flex(60, 10, 8, 2), 21.0)

    def test_reconcile_clamps_floor_when_over_budget(self):
        # remaining negative -> clamped to FLEX_FLOOR
        self.assertEqual(render.reconcile_phase_flex(30, 40, 0, 1), render.FLEX_FLOOR)

    def test_reconcile_clamps_ceiling(self):
        self.assertEqual(render.reconcile_phase_flex(10_000, 0, 0, 1), render.FLEX_CEIL)

    def test_reconcile_zero_flex_returns_zero(self):
        self.assertEqual(render.reconcile_phase_flex(60, 10, 8, 0), 0.0)

    def test_estimate_total_seconds_in_band_for_flex_sit(self):
        # _valid_manifest fills each phase's flex toward its target (~600 total,
        # minus closing which is speech-only) — the estimate should land in band.
        est = render.estimate_total_seconds(_valid_manifest())
        self.assertGreater(est, 400)
        self.assertLess(est, 700)

    def test_estimate_total_seconds_counts_explicit_silence(self):
        # Two 150s holds in one phase dominate the estimate.
        m = _valid_manifest()
        m["phases"][3]["segments"] = [
            {"type": "speech", "text": "Rest.", "tone": "soft"},
            {"type": "silence", "seconds": 150},
            {"type": "silence", "seconds": 150},
        ]
        self.assertGreater(render.estimate_total_seconds(m), 300)

    def test_plan_segments_order_and_kinds(self):
        plan = render.plan_segments(_valid_manifest())
        # first three segments are arrival's speech, fixed silence, flex
        self.assertEqual(plan[0].kind, "speech")
        self.assertEqual(plan[0].phase, "arrival")
        self.assertEqual(plan[1].kind, "silence")
        self.assertEqual(plan[1].seconds, 8.0)
        self.assertEqual(plan[2].kind, "flex")
        # last segment is closing's speech
        self.assertEqual(plan[-1].kind, "speech")
        self.assertEqual(plan[-1].phase, "closing")

    def test_flatten_guidance_text_joins_speech_in_order(self):
        text = render.flatten_guidance_text(_valid_manifest())
        self.assertIn("Welcome. Let yourself arrive.", text)
        self.assertIn("Bring this stillness with you.", text)
        self.assertLess(text.index("Welcome"), text.index("Bring this stillness"))

    def test_estimate_speech_seconds_floor(self):
        self.assertEqual(render.estimate_speech_seconds("hi"), 1.2)

    def test_is_rate_limit(self):
        self.assertTrue(render._is_rate_limit("429 RESOURCE_EXHAUSTED ..."))
        self.assertTrue(render._is_rate_limit("google.genai.errors.ClientError: RESOURCE_EXHAUSTED"))
        self.assertFalse(render._is_rate_limit("500 Internal Server Error"))
        self.assertFalse(render._is_rate_limit("output too short"))

    def test_rate_limit_delay_prefers_server_retry_delay(self):
        # retryDelay honored (+1s slack), capped at the backoff ceiling.
        self.assertEqual(render._rate_limit_delay("...'retryDelay': '34s'...", 1), 35.0)
        self.assertEqual(render._rate_limit_delay("'retryDelay': '120s'", 1), render.TTS_RATE_LIMIT_BACKOFF_CAP_S)

    def test_rate_limit_delay_falls_back_to_exponential(self):
        self.assertEqual(render._rate_limit_delay("429 no delay", 1), 5.0)  # 5 * 2^0
        self.assertEqual(render._rate_limit_delay("429 no delay", 3), 20.0)  # 5 * 2^2
        self.assertEqual(render._rate_limit_delay("429 no delay", 9), render.TTS_RATE_LIMIT_BACKOFF_CAP_S)  # capped


# ═════════════════════════════════════════════════════════════════════
# 3. render_meditation orchestration (engine + I/O mocked)
# ═════════════════════════════════════════════════════════════════════


def _fake_result():
    return render.RenderResult(
        mp3_bytes=b"ID3fake-mp3",
        ogg_bytes=b"OggSfake-ogg",
        duration_ms=601_000,
        guidance_text="flattened narration",
        speech_count=6,
        failed_count=0,
    )


@override_settings(GEMINI_API_KEY="test-key", API_BASE_URL="https://api.example.test", FRONTEND_URL="https://app.test")
class RenderMeditationOrchestrationTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Core Test", telegram_chat_id=900100)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status"])

    def _session(self, manifest=None, status=MeditationStatus.PENDING):
        return MeditationSession.objects.create(
            tenant=self.tenant,
            date=date.today(),
            status=status,
            title="Letting go",
            manifest=manifest if manifest is not None else _valid_manifest(),
        )

    def test_happy_path_sets_ready_and_fields(self):
        session = self._session()
        with (
            patch.object(render, "render_manifest_to_audio", return_value=_fake_result()) as mock_render,
            patch.object(services, "upload_workspace_file_binary") as mock_upload,
            patch.object(services, "notify_meditation_ready") as mock_notify,
        ):
            services.render_meditation(session)

        mock_render.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)
        self.assertEqual(session.duration_ms, 601_000)
        self.assertEqual(session.guidance_text, "flattened narration")
        self.assertEqual(session.model, "gemini-2.5-flash-preview-tts")
        self.assertEqual(session.artifact_manifest_sha256, services._manifest_sha256(session.manifest))
        tid = str(self.tenant.id)
        self.assertEqual(session.audio_url, f"https://api.example.test/api/v1/meditations/{tid}/{session.id}.mp3")
        self.assertEqual(session.ogg_url, f"https://api.example.test/api/v1/meditations/{tid}/{session.id}.ogg")
        # mp3 + ogg both uploaded
        self.assertEqual(mock_upload.call_count, 2)
        upload_paths = [c.args[1] for c in mock_upload.call_args_list]
        self.assertIn(f"workspace/meditations/{session.id}.mp3", upload_paths)
        self.assertIn(f"workspace/meditations/{session.id}.ogg", upload_paths)
        mock_notify.assert_called_once()

    def test_idempotent_double_fire_renders_once(self):
        session = self._session()
        with (
            patch.object(render, "render_manifest_to_audio", return_value=_fake_result()) as mock_render,
            patch.object(services, "upload_workspace_file_binary"),
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(session)
            # Simulate a QStash retry loading the row fresh — it's now READY.
            services.render_meditation(MeditationSession.objects.get(id=session.id))

        self.assertEqual(mock_render.call_count, 1)

    def test_already_rendering_is_skipped(self):
        session = self._session(status=MeditationStatus.RENDERING)
        with patch.object(render, "render_manifest_to_audio", return_value=_fake_result()) as mock_render:
            services.render_meditation(session)
        mock_render.assert_not_called()

    def test_invalid_manifest_marks_failed_without_rendering(self):
        session = self._session(manifest={"phases": []})
        with patch.object(render, "render_manifest_to_audio") as mock_render:
            services.render_meditation(session)
        mock_render.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertEqual(session.failure_class, "terminal")
        self.assertIn("invalid_manifest", session.error)

    @override_settings(GEMINI_API_KEY="")
    def test_missing_api_key_marks_failed(self):
        session = self._session()
        with patch.object(render, "render_manifest_to_audio") as mock_render:
            services.render_meditation(session)
        mock_render.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertEqual(session.failure_class, "terminal")
        self.assertIn("GEMINI_API_KEY", session.error)

    def test_transient_error_marks_failed_and_reraises(self):
        session = self._session()
        with (
            patch.object(
                render,
                "render_manifest_to_audio",
                side_effect=[RuntimeError("ffmpeg boom"), _fake_result()],
            ) as mock_render,
            patch.object(services, "upload_workspace_file_binary"),
            patch.object(services, "notify_meditation_ready"),
        ):
            with self.assertRaises(RuntimeError):
                services.render_meditation(session)
            session.refresh_from_db()
            self.assertEqual(session.status, MeditationStatus.FAILED)
            self.assertEqual(session.failure_class, "transient")
            self.assertEqual(session.attempt_count, 1)
            self.assertIn("render_error", session.error)

            services.render_meditation(session)

        self.assertEqual(mock_render.call_count, 2)
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)
        self.assertEqual(session.failure_class, "")
        self.assertEqual(session.attempt_count, 2)

    def test_mostly_rate_limited_marks_failed_without_reraise(self):
        # Most segments rate-limited out → fail clearly (don't ship near-silent),
        # don't upload, don't notify, don't reraise.
        session = self._session()
        throttled = render.RenderResult(
            mp3_bytes=b"x",
            ogg_bytes=None,
            duration_ms=600_000,
            guidance_text="",
            speech_count=6,
            failed_count=5,
            quota_failed_count=5,
        )
        with (
            patch.object(render, "render_manifest_to_audio", return_value=throttled),
            patch.object(services, "upload_workspace_file_binary") as mock_upload,
            patch.object(services, "notify_meditation_ready") as mock_notify,
        ):
            services.render_meditation(session)  # must NOT raise
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertEqual(session.failure_class, "terminal")
        self.assertIn("tts_quota", session.error)
        mock_upload.assert_not_called()
        mock_notify.assert_not_called()

    def test_minority_rate_limited_still_ready(self):
        # A few throttled segments (placeholders) is acceptable — still ships ready.
        session = self._session()
        result = render.RenderResult(
            mp3_bytes=b"ID3x",
            ogg_bytes=b"OggSx",
            duration_ms=600_000,
            guidance_text="g",
            speech_count=6,
            failed_count=1,
            quota_failed_count=1,
        )
        with (
            patch.object(render, "render_manifest_to_audio", return_value=result),
            patch.object(services, "upload_workspace_file_binary"),
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(session)
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)

    def test_failed_transient_session_can_be_reclaimed(self):
        session = self._session(status=MeditationStatus.FAILED)
        session.failure_class = "transient"
        session.error = "render_error: temporary"
        session.save(update_fields=["failure_class", "error", "updated_at"])
        with (
            patch.object(render, "render_manifest_to_audio", return_value=_fake_result()) as mock_render,
            patch.object(services, "upload_workspace_file_binary"),
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(session)
        mock_render.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)

    def test_failed_terminal_session_is_not_claimed(self):
        session = self._session(status=MeditationStatus.FAILED)
        session.failure_class = "terminal"
        session.error = "invalid_manifest: bad arc"
        session.save(update_fields=["failure_class", "error", "updated_at"])
        with patch.object(render, "render_manifest_to_audio") as mock_render:
            services.render_meditation(session)
        mock_render.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertEqual(session.failure_class, "terminal")
        self.assertEqual(session.attempt_count, 0)

    @override_settings(CORE_RENDER_MAX_ATTEMPTS=3)
    def test_attempts_exhausted_finalizes_terminal_without_rendering(self):
        session = self._session(status=MeditationStatus.FAILED)
        session.failure_class = "transient"
        session.attempt_count = 3
        session.error = "render_error: temporary"
        session.save(update_fields=["failure_class", "attempt_count", "error", "updated_at"])
        with patch.object(render, "render_manifest_to_audio") as mock_render:
            services.render_meditation(session)
        mock_render.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertEqual(session.failure_class, "terminal")
        self.assertEqual(session.attempt_count, 3)
        self.assertEqual(session.error, "render_error: temporary; attempts exhausted")

    def test_persist_failure_marks_failed_and_reraises(self):
        # A successful render but a failed share upload must not strand the row at
        # RENDERING — it follows the FAILED-then-reraise (QStash retry) contract.
        session = self._session()
        with (
            patch.object(render, "render_manifest_to_audio", return_value=_fake_result()),
            patch.object(services, "upload_workspace_file_binary", side_effect=RuntimeError("SMB throttle")),
            patch.object(services, "notify_meditation_ready") as mock_notify,
            self.assertRaises(RuntimeError),
        ):
            services.render_meditation(session)
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertEqual(session.failure_class, "transient")
        self.assertIn("persist_error", session.error)
        mock_notify.assert_not_called()

    def test_persist_retry_with_matching_artifact_skips_tts(self):
        session = self._session(status=MeditationStatus.FAILED)
        session.failure_class = "transient"
        session.attempt_count = 1
        session.error = "persist_error: database unavailable"
        session.artifact_manifest_sha256 = services._manifest_sha256(session.manifest)
        session.duration_ms = 601_000
        session.guidance_text = "flattened narration"
        session.model = "checkpoint-model"
        session.voice = "Aoede"
        session.save(
            update_fields=[
                "failure_class",
                "attempt_count",
                "error",
                "artifact_manifest_sha256",
                "duration_ms",
                "guidance_text",
                "model",
                "voice",
                "updated_at",
            ]
        )

        with (
            patch.object(render, "render_manifest_to_audio") as mock_render,
            patch.object(services, "_meditation_artifact_exists", side_effect=[True, False]) as mock_probe,
            patch.object(services, "upload_workspace_file_binary") as mock_upload,
            patch.object(services, "notify_meditation_ready") as mock_notify,
        ):
            services.render_meditation(session)

        mock_render.assert_not_called()
        mock_upload.assert_not_called()
        tenant_id = str(self.tenant.id)
        mock_probe.assert_has_calls(
            [
                call(tenant_id, f"workspace/meditations/{session.id}.mp3"),
                call(tenant_id, f"workspace/meditations/{session.id}.ogg"),
            ]
        )
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)
        self.assertEqual(session.failure_class, "")
        self.assertEqual(session.attempt_count, 2)
        self.assertEqual(
            session.audio_url,
            f"https://api.example.test/api/v1/meditations/{tenant_id}/{session.id}.mp3",
        )
        self.assertEqual(session.ogg_url, "")
        self.assertEqual(session.duration_ms, 601_000)
        self.assertEqual(session.model, "checkpoint-model")
        self.assertEqual(session.voice, "Aoede")
        mock_notify.assert_called_once()

    def test_consumer_reset_pending_persist_retry_still_skips_tts(self):
        session = self._session(status=MeditationStatus.PENDING)
        session.failure_class = "transient"
        session.attempt_count = 1
        session.error = "persist_error: database unavailable"
        session.artifact_manifest_sha256 = services._manifest_sha256(session.manifest)
        session.duration_ms = 601_000
        session.guidance_text = "flattened narration"
        session.save(
            update_fields=[
                "failure_class",
                "attempt_count",
                "error",
                "artifact_manifest_sha256",
                "duration_ms",
                "guidance_text",
                "updated_at",
            ]
        )

        with (
            patch.object(render, "render_manifest_to_audio") as mock_render,
            patch.object(services, "_meditation_artifact_exists", side_effect=[True, False]),
            patch.object(services, "upload_workspace_file_binary") as mock_upload,
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(session)

        mock_render.assert_not_called()
        mock_upload.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)
        self.assertEqual(session.attempt_count, 2)

    def test_dispatch_failure_after_consumer_reset_still_skips_tts(self):
        session = self._session(status=MeditationStatus.PENDING)
        session.failure_class = "transient"
        session.attempt_count = 1
        session.error = "dispatch_error: qstash unavailable"
        session.artifact_manifest_sha256 = services._manifest_sha256(session.manifest)
        session.duration_ms = 601_000
        session.guidance_text = "flattened narration"
        session.save(
            update_fields=[
                "failure_class",
                "attempt_count",
                "error",
                "artifact_manifest_sha256",
                "duration_ms",
                "guidance_text",
                "updated_at",
            ]
        )

        with (
            patch.object(render, "render_manifest_to_audio") as mock_render,
            patch.object(services, "_meditation_artifact_exists", side_effect=[True, False]),
            patch.object(services, "upload_workspace_file_binary") as mock_upload,
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(session)

        mock_render.assert_not_called()
        mock_upload.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)
        self.assertEqual(session.attempt_count, 2)

    def test_stale_rendering_with_checkpoint_skips_tts(self):
        session = self._session(status=MeditationStatus.RENDERING)
        session.attempt_count = 1
        session.artifact_manifest_sha256 = services._manifest_sha256(session.manifest)
        session.duration_ms = 601_000
        session.guidance_text = "flattened narration"
        session.save(
            update_fields=[
                "attempt_count",
                "artifact_manifest_sha256",
                "duration_ms",
                "guidance_text",
                "updated_at",
            ]
        )
        MeditationSession.objects.filter(id=session.id).update(updated_at=timezone.now() - timedelta(minutes=30))

        with (
            patch.object(render, "render_manifest_to_audio") as mock_render,
            patch.object(services, "_meditation_artifact_exists", side_effect=[True, False]),
            patch.object(services, "upload_workspace_file_binary") as mock_upload,
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(MeditationSession.objects.get(id=session.id))

        mock_render.assert_not_called()
        mock_upload.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)
        self.assertEqual(session.attempt_count, 2)

    def test_notify_failure_does_not_undo_ready(self):
        # Notify is best-effort: a send exception must NOT propagate or un-ready
        # an already-stored render (that would re-render + re-bill on retry).
        session = self._session()
        with (
            patch.object(render, "render_manifest_to_audio", return_value=_fake_result()),
            patch.object(services, "upload_workspace_file_binary"),
            patch.object(services, "notify_meditation_ready", side_effect=RuntimeError("telegram down")),
        ):
            services.render_meditation(session)  # must NOT raise
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)

    def test_stale_rendering_is_reclaimed(self):
        # A RENDERING row whose claim has gone stale (worker killed mid-render) is
        # re-taken; a fresh RENDERING row (test_already_rendering_is_skipped) is not.
        session = self._session(status=MeditationStatus.RENDERING)
        stale = timezone.now() - timedelta(minutes=30)
        MeditationSession.objects.filter(id=session.id).update(updated_at=stale)
        with (
            patch.object(render, "render_manifest_to_audio", return_value=_fake_result()) as mock_render,
            patch.object(services, "upload_workspace_file_binary"),
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(MeditationSession.objects.get(id=session.id))
        mock_render.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)

    def test_save_recovers_from_render_killed_db_connection(self):
        # The multi-minute render holds an idle DB connection that Postgres kills;
        # the post-render write must drop it, restore the service-role RLS GUC, and
        # retry — otherwise the status update is lost and the row wedges at RENDERING.
        from django.db.utils import OperationalError

        session = self._session()
        real_save = session.save
        calls = {"n": 0}

        def flaky_save(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("terminating connection due to idle-session timeout")
            return real_save(*args, **kwargs)

        with (
            patch.object(session, "save", side_effect=flaky_save),
            patch("django.db.connection.close") as mock_close,
            patch("apps.tenants.middleware.set_rls_context") as mock_rls,
        ):
            services._save_session(session, ["status", "error", "updated_at"])

        self.assertEqual(calls["n"], 2)  # failed once, retried once
        mock_close.assert_called_once()  # dead connection dropped
        mock_rls.assert_called_once()  # service-role RLS GUC restored on the fresh connection


# ═════════════════════════════════════════════════════════════════════
# 4. notify_meditation_ready (all-channels, deterministic, non-fatal)
# ═════════════════════════════════════════════════════════════════════


@override_settings(FRONTEND_URL="https://app.test")
class NotifyMeditationReadyTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Notify Test", telegram_chat_id=900200)
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status"])

    def _session(self):
        return MeditationSession.objects.create(
            tenant=self.tenant, date=date.today(), status=MeditationStatus.READY, title="Calm"
        )

    def test_telegram_send_and_record(self):
        session = self._session()
        with (
            patch("apps.router.services.send_telegram_message", return_value=True) as mock_send,
            patch("apps.router.proactive_context.record_proactive_outbound") as mock_record,
        ):
            delivered = services.notify_meditation_ready(session)
        self.assertTrue(delivered)
        mock_send.assert_called_once()
        chat_id, text = mock_send.call_args.args[0], mock_send.call_args.args[1]
        self.assertEqual(chat_id, 900200)
        self.assertIn("Calm", text)
        self.assertIn("https://app.test/core", text)
        mock_record.assert_called_once()

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="line-token")
    def test_line_send_and_record(self):
        # LINE-ONLY user (no telegram_chat_id, no device token). resolve_user_channel
        # is app → telegram → line and no longer consults preferred_channel, so the
        # LINE path is reached by LINKAGE alone — this is the line-only cohort
        # (e.g. Kiho) whose delivery still runs through _send_line_text.
        user = self.tenant.user
        user.line_user_id = "U" + "a" * 32
        user.telegram_chat_id = None
        user.save(update_fields=["line_user_id", "telegram_chat_id"])
        session = self._session()

        fake_resp = MagicMock()
        fake_resp.is_success = True
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"sentMessages": [{"id": "1"}]}

        with (
            patch("httpx.post", return_value=fake_resp) as mock_post,
            patch("apps.router.line_webhook._record_line_outbound") as mock_line_record,
            patch("apps.router.proactive_context.record_proactive_outbound") as mock_record,
        ):
            delivered = services.notify_meditation_ready(session)

        self.assertTrue(delivered)
        mock_post.assert_called_once()
        self.assertIn("api.line.me", mock_post.call_args.args[0])
        mock_line_record.assert_called_once()
        mock_record.assert_called_once()

    @override_settings(LINE_CHANNEL_ACCESS_TOKEN="line-token")
    def test_line_failure_trips_quota_and_returns_false(self):
        # LINE-ONLY user, as above — keeps the monthly-quota trip on a LINE 429
        # genuinely covered rather than silently routing to Telegram.
        user = self.tenant.user
        user.line_user_id = "U" + "b" * 32
        user.telegram_chat_id = None
        user.save(update_fields=["line_user_id", "telegram_chat_id"])
        session = self._session()

        fake_resp = MagicMock()
        fake_resp.is_success = False
        fake_resp.status_code = 429
        fake_resp.text = "monthly limit exceeded"

        with (
            patch("httpx.post", return_value=fake_resp),
            patch("apps.router.line_webhook._maybe_trip_monthly_quota") as mock_trip,
            patch("apps.router.proactive_context.record_proactive_outbound") as mock_record,
        ):
            delivered = services.notify_meditation_ready(session)

        self.assertFalse(delivered)
        mock_trip.assert_called_once()
        mock_record.assert_not_called()  # nothing recorded when delivery failed

    def test_pii_title_rehydrated_before_send(self):
        # The title is assistant-authored and may carry [PERSON_N] placeholders;
        # they must be rehydrated before the text hits the channel.
        self.tenant.pii_entity_map = {"[PERSON_0]": "Alice"}
        self.tenant.save(update_fields=["pii_entity_map"])
        session = MeditationSession.objects.create(
            tenant=self.tenant,
            date=date.today(),
            status=MeditationStatus.READY,
            title="Letting go for [PERSON_0]",
        )
        with (
            patch("apps.router.services.send_telegram_message", return_value=True) as mock_send,
            patch("apps.router.proactive_context.record_proactive_outbound"),
        ):
            services.notify_meditation_ready(session)
        text = mock_send.call_args.args[1]
        self.assertIn("Alice", text)
        self.assertNotIn("[PERSON_0]", text)

    def test_no_channel_linked_does_not_send(self):
        self.tenant.user.telegram_chat_id = None
        self.tenant.user.save(update_fields=["telegram_chat_id"])
        session = self._session()
        with patch("apps.router.services.send_telegram_message") as mock_send:
            delivered = services.notify_meditation_ready(session)
        self.assertFalse(delivered)
        mock_send.assert_not_called()

    def test_app_only_user_delivered_via_app_channel(self):
        # iOS-only user (no Telegram/LINE, has a registered device) → delivered
        # via the 'app' channel: no messaging-channel send, recorded as
        # channel='app' (which fires the iOS push + the ?since= feed row).
        from apps.router.models import DeviceToken

        user = self.tenant.user
        user.telegram_chat_id = None
        user.save(update_fields=["telegram_chat_id"])
        DeviceToken.objects.create(user=user, tenant=self.tenant, token="a" * 64, environment="sandbox")
        session = self._session()
        with (
            patch("apps.router.services.send_telegram_message") as mock_send,
            patch("apps.router.proactive_context.record_proactive_outbound") as mock_record,
        ):
            delivered = services.notify_meditation_ready(session)
        self.assertTrue(delivered)
        mock_send.assert_not_called()
        mock_record.assert_called_once()
        self.assertEqual(mock_record.call_args.kwargs["channel"], "app")
        self.assertEqual(mock_record.call_args.kwargs["channel_user_id"], str(user.id))

    def test_eval_sink_records_evidence_without_telegram(self):
        self.tenant.is_synthetic = True
        self.tenant.is_eval_sink = True
        self.tenant.save(update_fields=["is_synthetic", "is_eval_sink"])
        session = self._session()
        with (
            patch("apps.router.services.send_telegram_message") as mock_send,
            patch("apps.router.proactive_context.record_proactive_outbound") as mock_record,
        ):
            delivered = services.notify_meditation_ready(session)

        self.assertTrue(delivered)
        mock_send.assert_not_called()
        mock_record.assert_called_once()
        self.assertEqual(mock_record.call_args.kwargs["channel"], "eval")
        self.assertEqual(mock_record.call_args.kwargs["channel_user_id"], str(self.tenant.user_id))

    def test_inactive_tenant_does_not_send(self):
        self.tenant.status = Tenant.Status.PENDING
        self.tenant.save(update_fields=["status"])
        session = self._session()
        with patch("apps.router.services.send_telegram_message") as mock_send:
            delivered = services.notify_meditation_ready(session)
        self.assertFalse(delivered)
        mock_send.assert_not_called()


# ═════════════════════════════════════════════════════════════════════
# 5. Runtime manifest validation at the create boundary
# ═════════════════════════════════════════════════════════════════════


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeMeditationCreateViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Runtime Core", telegram_chat_id=900300)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }

    def _url(self):
        return f"/api/v1/core/runtime/{self.tenant.id}/meditation/"

    def test_invalid_manifest_rejected_400_no_row(self):
        resp = self.client.post(self._url(), {"manifest": {"phases": []}}, format="json", **self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["error"], "invalid_manifest")
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 0)

    def test_valid_manifest_creates_pending_and_enqueues(self):
        with patch("apps.cron.publish.publish_task") as mock_publish:
            resp = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json", **self.headers)
        self.assertEqual(resp.status_code, 201)
        sessions = MeditationSession.objects.filter(tenant=self.tenant)
        self.assertEqual(sessions.count(), 1)
        session = sessions.first()
        self.assertEqual(session.status, MeditationStatus.PENDING)
        mock_publish.assert_called_once()
        self.assertEqual(mock_publish.call_args.args[0], "render_meditation")
        self.assertEqual(mock_publish.call_args.args[1], str(session.id))

    def test_transient_failed_session_is_reused_and_republished(self):
        from apps.common.tenant_tz import tenant_today

        stored_manifest = _valid_manifest()
        session = MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant),
            status=MeditationStatus.FAILED,
            failure_class="transient",
            attempt_count=1,
            error="render_error: temporary",
            manifest=stored_manifest,
        )
        submitted_manifest = _valid_manifest()
        submitted_manifest["title"] = "A different valid request"
        with patch("apps.cron.publish.publish_task") as mock_publish:
            resp = self.client.post(self._url(), {"manifest": submitted_manifest}, format="json", **self.headers)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["meditation_id"], str(session.id))
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 1)
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.PENDING)
        self.assertEqual(session.manifest, stored_manifest)
        mock_publish.assert_called_once_with("render_meditation", str(session.id))

    @override_settings(CORE_RENDER_MAX_ATTEMPTS=3)
    def test_attempt_exhausted_transient_session_is_not_reused(self):
        from apps.common.tenant_tz import tenant_today

        failed = MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant),
            status=MeditationStatus.FAILED,
            failure_class="transient",
            attempt_count=3,
            error="render_error: temporary",
            manifest=_valid_manifest(),
        )
        with patch("apps.cron.publish.publish_task") as mock_publish:
            resp = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json", **self.headers)

        self.assertEqual(resp.status_code, 201)
        self.assertNotEqual(resp.data["meditation_id"], str(failed.id))
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 2)
        mock_publish.assert_called_once_with("render_meditation", resp.data["meditation_id"])

    def test_in_flight_coalescing_is_date_scoped(self):
        from apps.common.tenant_tz import tenant_today

        old = MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant) - timedelta(days=1),
            status=MeditationStatus.RENDERING,
            manifest=_valid_manifest(),
        )
        with patch("apps.cron.publish.publish_task") as mock_publish:
            created = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json", **self.headers)
            coalesced = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json", **self.headers)

        self.assertEqual(created.status_code, 201)
        self.assertEqual(coalesced.status_code, 200)
        self.assertNotEqual(created.data["meditation_id"], str(old.id))
        self.assertEqual(coalesced.data["meditation_id"], created.data["meditation_id"])
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 2)
        mock_publish.assert_called_once_with("render_meditation", created.data["meditation_id"])

    def test_terminal_failed_session_creates_new_row(self):
        from apps.common.tenant_tz import tenant_today

        failed = MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant),
            status=MeditationStatus.FAILED,
            failure_class="terminal",
            error="invalid_manifest: terminal",
            manifest=_valid_manifest(),
        )
        with patch("apps.cron.publish.publish_task") as mock_publish:
            resp = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json", **self.headers)

        self.assertEqual(resp.status_code, 201)
        self.assertNotEqual(resp.data["meditation_id"], str(failed.id))
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 2)
        mock_publish.assert_called_once_with("render_meditation", resp.data["meditation_id"])

    def test_publish_failure_is_durable_and_returns_created(self):
        with patch("apps.cron.publish.publish_task", side_effect=RuntimeError("qstash unavailable")):
            resp = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json", **self.headers)

        self.assertEqual(resp.status_code, 201)
        session = MeditationSession.objects.get(tenant=self.tenant)
        self.assertEqual(session.status, MeditationStatus.PENDING)
        self.assertEqual(session.failure_class, "transient")
        self.assertEqual(session.error, "dispatch_error: qstash unavailable")

    def test_ambiguous_publish_failure_does_not_overwrite_progressed_worker(self):
        def delivered_then_timed_out(_task_name, meditation_id):
            MeditationSession.objects.filter(id=meditation_id).update(
                status=MeditationStatus.READY,
                failure_class="",
                error="",
            )
            raise RuntimeError("timeout after accept")

        with patch("apps.cron.publish.publish_task", side_effect=delivered_then_timed_out):
            resp = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json", **self.headers)

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], MeditationStatus.READY)
        session = MeditationSession.objects.get(tenant=self.tenant)
        self.assertEqual(session.status, MeditationStatus.READY)
        self.assertEqual(session.failure_class, "")
        self.assertEqual(session.error, "")

    def test_auth_required(self):
        resp = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json")
        self.assertEqual(resp.status_code, 401)


class RenderMeditationTaskTests(TestCase):
    """The QStash entry point resolves by id; the service owns all claims."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Task Test", telegram_chat_id=900400)

    def _session(self, status=MeditationStatus.PENDING):
        return MeditationSession.objects.create(tenant=self.tenant, date=date.today(), status=status, title="T")

    def test_missing_session_returns_cleanly(self):
        from apps.core import tasks

        with patch.object(services, "render_meditation") as mock_render:
            tasks.render_meditation_task(str(uuid4()))  # bogus id — must not raise
        mock_render.assert_not_called()

    def test_ready_session_is_delegated_to_service_claim_authority(self):
        from apps.core import tasks

        session = self._session(status=MeditationStatus.READY)
        with patch.object(services, "render_meditation") as mock_render:
            tasks.render_meditation_task(str(session.id))
        mock_render.assert_called_once_with(session)

    def test_pending_session_dispatches_once(self):
        from apps.core import tasks

        session = self._session(status=MeditationStatus.PENDING)
        with patch.object(services, "render_meditation") as mock_render:
            tasks.render_meditation_task(str(session.id))
        mock_render.assert_called_once()

    def test_task_is_registered(self):
        from apps.cron.views import TASK_MAP

        self.assertEqual(
            TASK_MAP["render_meditation"],
            "apps.core.tasks.render_meditation_task",
        )


@override_settings(CORE_RENDER_MAX_ATTEMPTS=3, CORE_RENDER_STALE_MINUTES=15)
class ReapMeditationsTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Meditation Reaper", telegram_chat_id=900401)

    def _session(self, status, *, manifest=None, failure_class="", attempt_count=0):
        return MeditationSession.objects.create(
            tenant=self.tenant,
            date=date.today(),
            status=status,
            manifest=manifest if manifest is not None else {},
            failure_class=failure_class,
            attempt_count=attempt_count,
        )

    def _age(self, *sessions, minutes):
        MeditationSession.objects.filter(id__in=[session.id for session in sessions]).update(
            updated_at=timezone.now() - timedelta(minutes=minutes)
        )

    def test_reaps_each_recoverable_stranded_state(self):
        from apps.core import tasks

        stale_rendering = self._session(MeditationStatus.RENDERING, manifest=_valid_manifest())
        pending_with_manifest = self._session(MeditationStatus.PENDING, manifest=_valid_manifest())
        pending_without_manifest = self._session(MeditationStatus.PENDING)
        failed_transient = self._session(
            MeditationStatus.FAILED,
            manifest=_valid_manifest(),
            failure_class="transient",
            attempt_count=2,
        )
        self._age(stale_rendering, pending_with_manifest, pending_without_manifest, failed_transient, minutes=20)

        with patch("apps.cron.publish.publish_task") as mock_publish:
            result = tasks.reap_meditations()

        self.assertEqual(result["candidates"], 4)
        self.assertEqual(result["render_published"], 3)
        self.assertEqual(result["compose_published"], 1)
        self.assertEqual(result["errors"], 0)
        mock_publish.assert_has_calls(
            [
                call("render_meditation", str(stale_rendering.id)),
                call("render_meditation", str(pending_with_manifest.id)),
                call("compose_meditation", str(pending_without_manifest.id)),
                call("render_meditation", str(failed_transient.id)),
            ],
            any_order=True,
        )

    def test_reaper_skips_fresh_terminal_and_attempt_exhausted_rows(self):
        from apps.core import tasks

        fresh_pending = self._session(MeditationStatus.PENDING)
        terminal = self._session(
            MeditationStatus.FAILED,
            manifest=_valid_manifest(),
            failure_class="terminal",
        )
        attempts_exhausted = self._session(
            MeditationStatus.FAILED,
            manifest=_valid_manifest(),
            failure_class="transient",
            attempt_count=3,
        )
        self._age(terminal, attempts_exhausted, minutes=20)

        with patch("apps.cron.publish.publish_task") as mock_publish:
            result = tasks.reap_meditations()

        self.assertEqual(result["candidates"], 0)
        mock_publish.assert_not_called()
        fresh_pending.refresh_from_db()
        terminal.refresh_from_db()
        attempts_exhausted.refresh_from_db()
        self.assertEqual(fresh_pending.status, MeditationStatus.PENDING)
        self.assertEqual(terminal.failure_class, "terminal")
        self.assertEqual(attempts_exhausted.failure_class, "transient")

    def test_reaper_bounds_each_run_to_fifty_oldest_rows(self):
        from apps.core import tasks

        sessions = [self._session(MeditationStatus.PENDING) for _ in range(51)]
        self._age(*sessions, minutes=20)

        with patch("apps.cron.publish.publish_task") as mock_publish:
            result = tasks.reap_meditations()

        self.assertEqual(result["candidates"], 50)
        self.assertEqual(result["compose_published"], 50)
        self.assertEqual(mock_publish.call_count, 50)

    def test_reaper_continues_after_individual_publish_failure(self):
        from apps.core import tasks

        first = self._session(MeditationStatus.PENDING)
        second = self._session(MeditationStatus.PENDING)
        self._age(first, second, minutes=20)

        with patch(
            "apps.cron.publish.publish_task",
            side_effect=[RuntimeError("qstash down"), None],
        ) as mock_publish:
            result = tasks.reap_meditations()

        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["compose_published"], 1)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(mock_publish.call_count, 2)

    def test_reaper_is_registered(self):
        from apps.cron.views import TASK_MAP

        self.assertEqual(TASK_MAP["reap_meditations"], "apps.core.tasks.reap_meditations")


@override_settings(CORE_RENDER_MAX_ATTEMPTS=3)
class MeditationSessionSerializerRetryTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Retry Serializer", telegram_chat_id=900402)

    def test_retryable_and_attempt_count_projection(self):
        from apps.core.serializers import MeditationSessionSerializer

        session = MeditationSession.objects.create(
            tenant=self.tenant,
            date=date.today(),
            status=MeditationStatus.FAILED,
            failure_class="transient",
            attempt_count=2,
        )
        data = MeditationSessionSerializer(session).data
        self.assertEqual(data["attempt_count"], 2)
        self.assertIs(data["retryable"], True)
        self.assertNotIn("failure_class", data)

        serializer = MeditationSessionSerializer(session, data={"attempt_count": 0}, partial=True)
        self.assertTrue(serializer.is_valid())
        serializer.save()
        session.refresh_from_db()
        self.assertEqual(session.attempt_count, 2)

        session.attempt_count = 3
        data = MeditationSessionSerializer(session).data
        self.assertIs(data["retryable"], False)

        session.attempt_count = 1
        session.failure_class = "terminal"
        data = MeditationSessionSerializer(session).data
        self.assertIs(data["retryable"], False)

        session.failure_class = "transient"
        session.status = MeditationStatus.PENDING
        data = MeditationSessionSerializer(session).data
        self.assertIs(data["retryable"], False)


class MeditationRetryMigrationTests(TestCase):
    def test_existing_failed_rows_are_backfilled_by_error_prefix(self):
        from importlib import import_module

        from django.apps import apps as django_apps

        migration = import_module("apps.core.migrations.0004_meditationsession_retry_state")
        tenant = create_tenant(display_name="Retry Migration", telegram_chat_id=900403)
        render_failed = MeditationSession.objects.create(
            tenant=tenant,
            date=date.today(),
            status=MeditationStatus.FAILED,
            error="render_error: ffmpeg unavailable",
        )
        persist_failed = MeditationSession.objects.create(
            tenant=tenant,
            date=date.today(),
            status=MeditationStatus.FAILED,
            error="persist_error: share timeout",
        )
        terminal_failed = MeditationSession.objects.create(
            tenant=tenant,
            date=date.today(),
            status=MeditationStatus.FAILED,
            error="invalid_manifest: bad arc",
        )

        migration.classify_existing_failures(django_apps, None)

        render_failed.refresh_from_db()
        persist_failed.refresh_from_db()
        terminal_failed.refresh_from_db()
        self.assertEqual(render_failed.failure_class, "transient")
        self.assertEqual(persist_failed.failure_class, "transient")
        self.assertEqual(terminal_failed.failure_class, "terminal")


# ═════════════════════════════════════════════════════════════════════
# 6. Real ffmpeg stitch (skipped when ffmpeg is unavailable). Mock TTS tones,
#    so no Gemini key / network — exercises the actual silence/stitch/transcode.
# ═════════════════════════════════════════════════════════════════════


@skipUnless(_HAS_FFMPEG, "ffmpeg/ffprobe not on PATH")
class RealFfmpegRenderTests(UnitTestCase):
    def _tiny_manifest(self) -> dict:
        targets = {
            "arrival": 4,
            "breath_anchor": 4,
            "body_scan": 5,
            "core_practice": 5,
            "integration": 4,
            "closing": 3,
        }
        phases = []
        for name in render.REQUIRED_PHASES:
            segs = [{"type": "speech", "text": "Breathe in.", "tone": "calm"}]
            if name != "closing":
                segs.append({"type": "silence", "seconds": "flex"})
            phases.append({"name": name, "target_seconds": targets[name], "segments": segs})
        return {"global_tone": "soft", "total_target_seconds": 25, "phases": phases}

    def test_render_produces_valid_audio(self):
        result = render.render_manifest_to_audio(
            self._tiny_manifest(),
            voice=render.DEFAULT_VOICE,
            model=render.DEFAULT_MODEL,
            mock=True,
            concurrency=2,
            want_ogg=True,
        )
        self.assertEqual(result.speech_count, 6)
        self.assertEqual(result.failed_count, 0)
        self.assertGreater(len(result.mp3_bytes), 500)
        self.assertIsNotNone(result.ogg_bytes)
        self.assertGreater(len(result.ogg_bytes), 200)
        # ~23s expected; allow a generous band for codec/loudnorm overhead.
        self.assertGreater(result.duration_ms, 12_000)
        self.assertLess(result.duration_ms, 45_000)

    def test_render_without_ogg(self):
        result = render.render_manifest_to_audio(
            self._tiny_manifest(),
            voice=render.DEFAULT_VOICE,
            model=render.DEFAULT_MODEL,
            mock=True,
            concurrency=1,
            want_ogg=False,
        )
        self.assertIsNone(result.ogg_bytes)
        self.assertGreater(len(result.mp3_bytes), 500)


# ═════════════════════════════════════════════════════════════════════
# 7. Compose — LLM manifest authoring (OpenRouter mocked, no network)
# ═════════════════════════════════════════════════════════════════════


def _over_segmented_manifest() -> dict:
    """A structurally-correct manifest that trips ONLY the MAX_SPEECH_SEGMENTS
    ceiling — the 2026-06-25 'too many spoken segments (15 > 12)' failure."""
    m = _valid_manifest()
    core = next(p for p in m["phases"] if p["name"] == "core_practice")
    over = render.MAX_SPEECH_SEGMENTS + 2  # exceed the cap on its own
    core["segments"] = [
        {"type": "speech", "text": f"Notice this, moment {i}.", "tone": "soft"} for i in range(over)
    ] + [{"type": "silence", "seconds": 10}]
    return m


@override_settings(OPENROUTER_API_KEY="test-or-key")
class ComposeAuthoringTests(SimpleTestCase):
    def _ok(self, content, model="test/primary"):
        """A ``chat_completion`` return value: ``(response_json, model_used)``."""
        return ({"choices": [{"message": {"content": content}}]}, model)

    def test_authors_and_normalizes_valid_manifest(self):
        with patch("apps.core.compose.chat_completion", return_value=self._ok(json.dumps(_valid_manifest()))) as cc:
            out = compose.author_manifest({"additional_context": "work stress"}, voice="Achernar")
        self.assertEqual(render.validate_manifest(out), [])
        self.assertEqual(out["voice"], "Achernar")  # normalized
        self.assertEqual(out["total_target_seconds"], 600)
        cc.assert_called_once()  # primary answered — no fallback needed

    def test_non_json_raises(self):
        # Every candidate in the chain returns non-JSON → terminal ComposeError.
        with (
            patch("apps.core.compose.chat_completion", return_value=self._ok("not json {")),
            self.assertRaises(compose.ComposeError),
        ):
            compose.author_manifest({})

    def test_invalid_manifest_raises(self):
        bad = json.dumps({"phases": [{"name": "arrival", "segments": []}]})
        with (
            patch("apps.core.compose.chat_completion", return_value=self._ok(bad)),
            self.assertRaises(compose.ComposeError),
        ):
            compose.author_manifest({})

    @override_settings(OPENROUTER_API_KEY="")
    def test_missing_key_raises(self):
        with self.assertRaises(compose.ComposeError):
            compose.author_manifest({})

    # ── model fallback (the fix for the 2026-06-25 "can't generate" incident) ──

    def test_chain_is_low_cost_steerable_first(self):
        models = compose._compose_models()
        self.assertEqual(models[0], compose.GEMMA_MODEL)  # steerable, structured-output-capable
        self.assertIn(compose.DEEPSEEK_FLASH_MODEL, models)  # cheap fallback
        self.assertIn(compose.DEEPSEEK_MODEL, models)  # V4 Pro last resort
        self.assertEqual(len(models), len(set(models)))  # de-duped

    def test_explicit_model_pins_a_single_model_no_fallback(self):
        with (
            patch("apps.core.compose.chat_completion", return_value=self._ok("not json")) as cc,
            self.assertRaises(compose.ComposeError),
        ):
            compose.author_manifest({}, model="some/model")
        cc.assert_called_once()

    def test_falls_back_when_primary_truncates_json(self):
        # Primary drifts to truncated (here, non-English) JSON — the live 06-25
        # failure; the next candidate returns a valid manifest → compose succeeds.
        truncated = '{"schema_version": 1, "title": "轻放责备", "theme": "温柔'
        with patch(
            "apps.core.compose.chat_completion",
            side_effect=[self._ok(truncated), self._ok(json.dumps(_valid_manifest()))],
        ) as cc:
            out = compose.author_manifest({})
        self.assertEqual(render.validate_manifest(out), [])
        self.assertEqual(cc.call_count, 2)

    def test_falls_back_when_primary_returns_non_dict_json(self):
        # Valid JSON that ISN'T a manifest object (top-level array) — _normalize
        # raises ComposeError; it must fall through, not abort the chain.
        with patch(
            "apps.core.compose.chat_completion",
            side_effect=[self._ok("[1, 2, 3]"), self._ok(json.dumps(_valid_manifest()))],
        ) as cc:
            out = compose.author_manifest({})
        self.assertEqual(render.validate_manifest(out), [])
        self.assertEqual(cc.call_count, 2)

    def test_falls_back_when_primary_over_produces_segments(self):
        over = _over_segmented_manifest()
        self.assertTrue(any("too many spoken" in e for e in render.validate_manifest(over)))
        with patch(
            "apps.core.compose.chat_completion",
            side_effect=[self._ok(json.dumps(over)), self._ok(json.dumps(_valid_manifest()))],
        ) as cc:
            out = compose.author_manifest({})
        self.assertEqual(render.validate_manifest(out), [])
        self.assertEqual(cc.call_count, 2)

    def test_falls_back_when_primary_transport_errors(self):
        # Primary transport-errors on BOTH its attempt and its one transient
        # retry, so it's truly abandoned and the next model answers. (502 is
        # transient → the retry fires; patch sleep so the test doesn't wait.)
        with (
            patch("apps.core.compose.time.sleep"),
            patch(
                "apps.core.compose.chat_completion",
                side_effect=[
                    RuntimeError("502 upstream"),
                    RuntimeError("502 upstream"),
                    self._ok(json.dumps(_valid_manifest())),
                ],
            ) as cc,
        ):
            out = compose.author_manifest({})
        self.assertEqual(render.validate_manifest(out), [])
        self.assertEqual(cc.call_count, 3)  # primary attempt + retry, then fallback

    def test_transient_error_retried_once_on_same_model(self):
        # A single transient hiccup on the primary is recovered by the one
        # backed-off retry — no fallback needed, the primary itself succeeds.
        with (
            patch("apps.core.compose.time.sleep") as sleep,
            patch(
                "apps.core.compose.chat_completion",
                side_effect=[RuntimeError("read timeout"), self._ok(json.dumps(_valid_manifest()))],
            ) as cc,
        ):
            out = compose.author_manifest({})
        self.assertEqual(render.validate_manifest(out), [])
        self.assertEqual(cc.call_count, 2)  # attempt + retry, same model
        sleep.assert_called_once()  # backoff before the retry

    def test_content_failure_is_not_retried(self):
        # A non-transient failure (unparseable content) must NOT trigger the
        # same-model retry — it falls straight through to the next candidate.
        with (
            patch("apps.core.compose.time.sleep") as sleep,
            patch(
                "apps.core.compose.chat_completion",
                side_effect=[self._ok("not json {"), self._ok(json.dumps(_valid_manifest()))],
            ) as cc,
        ):
            out = compose.author_manifest({})
        self.assertEqual(render.validate_manifest(out), [])
        self.assertEqual(cc.call_count, 2)  # one call per model, no retry
        sleep.assert_not_called()

    def test_terminal_error_carries_every_candidate_reason(self):
        # All three candidates fail differently → the raised reason names each, so
        # session.error is diagnosable (not just the last candidate's failure).
        # The 3rd candidate transient-errors on both its attempt and its retry
        # (503 is transient); patch sleep so the backoff doesn't wait.
        over = json.dumps(_over_segmented_manifest())
        with (
            patch("apps.core.compose.time.sleep"),
            patch(
                "apps.core.compose.chat_completion",
                side_effect=[self._ok(over), self._ok("not json"), RuntimeError("503"), RuntimeError("503")],
            ),
            self.assertRaises(compose.ComposeError) as ctx,
        ):
            compose.author_manifest({})
        msg = str(ctx.exception)
        self.assertIn("too many spoken", msg)
        self.assertIn("non-JSON", msg)
        self.assertIn("503", msg)

    def test_strips_code_fences(self):
        fenced = "```json\n" + json.dumps(_valid_manifest()) + "\n```"
        with patch("apps.core.compose.chat_completion", return_value=self._ok(fenced)):
            out = compose.author_manifest({})
        self.assertEqual(render.validate_manifest(out), [])


class ComposeTargetLengthTests(UnitTestCase):
    """The preferred-duration wiring (compose targets the user's chosen length)."""

    def test_target_seconds_default(self):
        self.assertEqual(compose._target_seconds_from_signals({}), 600.0)

    def test_target_seconds_from_minutes(self):
        self.assertEqual(compose._target_seconds_from_signals({"preferred_duration_minutes": 5}), 300.0)

    def test_target_seconds_clamped_to_band(self):
        self.assertEqual(
            compose._target_seconds_from_signals({"preferred_duration_minutes": 99}), render.HARD_MAX_TOTAL_SECONDS
        )
        self.assertEqual(compose._target_seconds_from_signals({"preferred_duration_minutes": 1}), 180.0)

    def test_normalize_pins_total_target_to_request(self):
        m = compose._normalize({"phases": []}, "Achernar", 300.0)
        self.assertEqual(m["total_target_seconds"], 300)

    def test_format_signals_states_target_minutes(self):
        self.assertIn("5 minutes", compose._format_signals({}, 300.0))


# ═════════════════════════════════════════════════════════════════════
# 8. Compose — signals, consumer view, task, service orchestration (DB)
# ═════════════════════════════════════════════════════════════════════


class GatherSignalsTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Gather", telegram_chat_id=900500)

    def test_includes_profile_context_and_last_theme(self):
        from apps.core.models import CoreProfile

        CoreProfile.objects.create(tenant=self.tenant, additional_context="please help me wind down at night")
        MeditationSession.objects.create(
            tenant=self.tenant, date=date.today(), status=MeditationStatus.READY, theme="letting go of work"
        )
        sig = services.gather_meditation_signals(self.tenant)
        self.assertEqual(sig["tenant_id"], str(self.tenant.id))
        self.assertIn("wind down", sig["additional_context"])
        self.assertIn("letting go", sig["last_meditation_theme"])

    def test_includes_preferred_duration(self):
        from apps.core.models import CoreProfile

        CoreProfile.objects.create(tenant=self.tenant, preferred_duration_minutes=15)
        sig = services.gather_meditation_signals(self.tenant)
        self.assertEqual(sig["preferred_duration_minutes"], 15)


class CoreComposeViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Compose View", telegram_chat_id=900600)
        self.tenant.core_enabled = True
        self.tenant.save(update_fields=["core_enabled"])
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_compose_creates_pending_and_enqueues(self):
        with patch("apps.cron.publish.publish_task") as mock_pub:
            resp = self.client.post("/api/v1/core/compose/")
        self.assertEqual(resp.status_code, 201)
        sessions = MeditationSession.objects.filter(tenant=self.tenant)
        self.assertEqual(sessions.count(), 1)
        self.assertEqual(sessions.first().status, MeditationStatus.PENDING)
        mock_pub.assert_called_once()
        self.assertEqual(mock_pub.call_args.args[0], "compose_meditation")
        self.assertEqual(mock_pub.call_args.args[1], str(sessions.first().id))

    def test_compose_dedups_in_flight(self):
        from apps.common.tenant_tz import tenant_today

        MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant),
            status=MeditationStatus.RENDERING,
        )
        with patch("apps.cron.publish.publish_task") as mock_pub:
            resp = self.client.post("/api/v1/core/compose/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 1)  # no new row
        mock_pub.assert_not_called()

    def test_compose_ignores_in_flight_row_from_prior_day(self):
        from apps.common.tenant_tz import tenant_today

        MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant) - timedelta(days=1),
            status=MeditationStatus.RENDERING,
        )
        with patch("apps.cron.publish.publish_task") as mock_pub:
            resp = self.client.post("/api/v1/core/compose/")

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 2)
        created = MeditationSession.objects.get(id=resp.data["meditation_id"])
        self.assertEqual(created.date, tenant_today(self.tenant))
        mock_pub.assert_called_once_with("compose_meditation", str(created.id))

    def test_compose_reuses_transient_failed_session_today(self):
        from apps.common.tenant_tz import tenant_today

        session = MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant),
            status=MeditationStatus.FAILED,
            failure_class="transient",
            attempt_count=1,
            error="render_error: temporary",
            manifest=_valid_manifest(),
        )
        with patch("apps.cron.publish.publish_task") as mock_pub:
            resp = self.client.post("/api/v1/core/compose/")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["meditation_id"], str(session.id))
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 1)
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.PENDING)
        mock_pub.assert_called_once_with("render_meditation", str(session.id))

    def test_compose_does_not_reuse_terminal_failed_session(self):
        from apps.common.tenant_tz import tenant_today

        recoverable_older = MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant),
            status=MeditationStatus.FAILED,
            failure_class="transient",
            attempt_count=1,
            error="render_error: temporary",
            manifest=_valid_manifest(),
        )
        terminal = MeditationSession.objects.create(
            tenant=self.tenant,
            date=tenant_today(self.tenant),
            status=MeditationStatus.FAILED,
            failure_class="terminal",
            error="invalid_manifest: terminal",
            manifest=_valid_manifest(),
        )
        with patch("apps.cron.publish.publish_task") as mock_pub:
            resp = self.client.post("/api/v1/core/compose/")

        self.assertEqual(resp.status_code, 201)
        self.assertNotEqual(resp.data["meditation_id"], str(terminal.id))
        self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 3)
        recoverable_older.refresh_from_db()
        self.assertEqual(recoverable_older.status, MeditationStatus.FAILED)
        mock_pub.assert_called_once_with("compose_meditation", resp.data["meditation_id"])

    @override_settings(CORE_RENDER_MAX_ATTEMPTS=3)
    def test_compose_does_not_reuse_exhausted_or_invalid_transient_failure(self):
        from apps.common.tenant_tz import tenant_today

        cases = (
            {"attempt_count": 3, "manifest": _valid_manifest()},
            {"attempt_count": 1, "manifest": {}},
        )
        for case in cases:
            with self.subTest(case=case):
                MeditationSession.objects.filter(tenant=self.tenant).delete()
                failed = MeditationSession.objects.create(
                    tenant=self.tenant,
                    date=tenant_today(self.tenant),
                    status=MeditationStatus.FAILED,
                    failure_class="transient",
                    error="render_error: temporary",
                    **case,
                )
                with patch("apps.cron.publish.publish_task") as mock_pub:
                    resp = self.client.post("/api/v1/core/compose/")

                self.assertEqual(resp.status_code, 201)
                self.assertNotEqual(resp.data["meditation_id"], str(failed.id))
                self.assertEqual(MeditationSession.objects.filter(tenant=self.tenant).count(), 2)
                mock_pub.assert_called_once_with("compose_meditation", resp.data["meditation_id"])

    def test_compose_publish_failure_is_durable_and_reaper_rescuable(self):
        from apps.core import tasks

        with patch("apps.cron.publish.publish_task", side_effect=RuntimeError("qstash unavailable")):
            resp = self.client.post("/api/v1/core/compose/")

        self.assertEqual(resp.status_code, 201)
        session = MeditationSession.objects.get(id=resp.data["meditation_id"])
        self.assertEqual(session.status, MeditationStatus.PENDING)
        self.assertEqual(session.failure_class, "transient")
        self.assertEqual(session.error, "dispatch_error: qstash unavailable")

        MeditationSession.objects.filter(id=session.id).update(updated_at=timezone.now() - timedelta(minutes=11))
        with patch("apps.cron.publish.publish_task") as mock_republish:
            result = tasks.reap_meditations()

        self.assertEqual(result["compose_published"], 1)
        mock_republish.assert_called_once_with("compose_meditation", str(session.id))

    def test_compose_ambiguous_publish_failure_preserves_terminal_worker_state(self):
        def delivered_then_timed_out(_task_name, meditation_id):
            MeditationSession.objects.filter(id=meditation_id).update(
                status=MeditationStatus.FAILED,
                failure_class="terminal",
                error="compose_error: refused",
            )
            raise RuntimeError("timeout after accept")

        with patch("apps.cron.publish.publish_task", side_effect=delivered_then_timed_out):
            resp = self.client.post("/api/v1/core/compose/")

        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["status"], MeditationStatus.FAILED)
        session = MeditationSession.objects.get(id=resp.data["meditation_id"])
        self.assertEqual(session.failure_class, "terminal")
        self.assertEqual(session.error, "compose_error: refused")

    def test_compose_requires_core_enabled(self):
        self.tenant.core_enabled = False
        self.tenant.save(update_fields=["core_enabled"])
        resp = self.client.post("/api/v1/core/compose/")
        self.assertEqual(resp.status_code, 403)

    def test_compose_requires_auth(self):
        resp = APIClient().post("/api/v1/core/compose/")
        self.assertEqual(resp.status_code, 401)


class ComposeMeditationTaskTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Compose Task", telegram_chat_id=900700)

    def _session(self, status=MeditationStatus.PENDING):
        return MeditationSession.objects.create(tenant=self.tenant, date=date.today(), status=status)

    def test_pending_dispatches(self):
        from apps.core import tasks

        session = self._session(MeditationStatus.PENDING)
        with patch.object(services, "compose_meditation") as mock_compose:
            tasks.compose_meditation_task(str(session.id))
        mock_compose.assert_called_once()

    def test_non_pending_skipped(self):
        from apps.core import tasks

        session = self._session(MeditationStatus.READY)
        with patch.object(services, "compose_meditation") as mock_compose:
            tasks.compose_meditation_task(str(session.id))
        mock_compose.assert_not_called()

    def test_missing_session_clean(self):
        from apps.core import tasks

        with patch.object(services, "compose_meditation") as mock_compose:
            tasks.compose_meditation_task(str(uuid4()))
        mock_compose.assert_not_called()


class ComposeMeditationServiceTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Compose Svc", telegram_chat_id=900800)

    def _session(self):
        return MeditationSession.objects.create(tenant=self.tenant, date=date.today(), status=MeditationStatus.PENDING)

    def test_authors_saves_manifest_then_publishes_render(self):
        session = self._session()
        manifest = _valid_manifest()
        with (
            patch.object(compose, "author_manifest", return_value=manifest) as mock_author,
            patch.object(services, "render_meditation") as mock_render,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            services.compose_meditation(session)
        mock_author.assert_called_once()
        mock_render.assert_not_called()
        session.refresh_from_db()
        self.assertTrue(session.manifest.get("phases"))
        self.assertEqual(session.title, manifest["title"])
        mock_publish.assert_called_once_with("render_meditation", str(session.id))

    def test_concurrent_compose_delivery_does_not_author_twice(self):
        session = self._session()
        manifest = _valid_manifest()

        def overlap_delivery(_signals, *, voice, tenant=None):
            del tenant
            services.compose_meditation(MeditationSession.objects.get(id=session.id))
            return manifest

        with (
            patch.object(compose, "author_manifest", side_effect=overlap_delivery) as mock_author,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            services.compose_meditation(session)

        mock_author.assert_called_once()
        mock_publish.assert_called_once_with("render_meditation", str(session.id))
        session.refresh_from_db()
        self.assertEqual(session.manifest, manifest)

    def test_redelivery_with_valid_manifest_skips_author_and_republishes(self):
        session = MeditationSession.objects.create(
            tenant=self.tenant,
            date=date.today(),
            status=MeditationStatus.PENDING,
            manifest=_valid_manifest(),
        )
        with (
            patch.object(compose, "author_manifest") as mock_author,
            patch("apps.cron.publish.publish_task") as mock_publish,
        ):
            services.compose_meditation(session)

        mock_author.assert_not_called()
        mock_publish.assert_called_once_with("render_meditation", str(session.id))

    def test_publish_failure_propagates_with_persisted_manifest(self):
        session = self._session()
        manifest = _valid_manifest()
        with (
            patch.object(compose, "author_manifest", return_value=manifest),
            patch("apps.cron.publish.publish_task", side_effect=RuntimeError("qstash unavailable")),
            self.assertRaises(RuntimeError),
        ):
            services.compose_meditation(session)

        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.PENDING)
        self.assertEqual(session.manifest, manifest)

    def test_compose_error_marks_failed_without_rendering(self):
        session = self._session()
        with (
            patch.object(compose, "author_manifest", side_effect=compose.ComposeError("refused")),
            patch.object(services, "render_meditation") as mock_render,
        ):
            services.compose_meditation(session)
        mock_render.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertEqual(session.failure_class, "terminal")
        self.assertIn("compose_error", session.error)


class MeditationSignalGatheringTests(TestCase):
    """gather_meditation_signals pulls constellation + journal context (ZDR-gated)."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Signals", telegram_chat_id=900300)
        Lesson.objects.filter(tenant=self.tenant).delete()

    def _star(self, **kwargs) -> Lesson:
        defaults = dict(
            tenant=self.tenant,
            text="Rest is productive",
            context="",
            source_type="reflection",
            source_ref="",
            tags=["recovery"],
            status="approved",
        )
        defaults.update(kwargs)
        return Lesson.objects.create(**defaults)

    def test_profile_context_and_last_theme(self):
        CoreProfile.objects.create(tenant=self.tenant, additional_context="going through a big move")
        MeditationSession.objects.create(
            tenant=self.tenant,
            date=timezone.now().date(),
            status=MeditationStatus.READY,
            theme="letting go of control",
        )
        signals = services.gather_meditation_signals(self.tenant)
        self.assertEqual(signals["additional_context"], "going through a big move")
        self.assertEqual(signals["last_meditation_theme"], "letting go of control")

    def test_gathers_active_constellation_star(self):
        star = self._star(galaxy_note="protect the off-days", star_stage="radiant")
        StarJournalEntry.objects.create(
            tenant=self.tenant, star=star, text="Took a full rest day, felt sharper.", entry_type="revisit"
        )
        TutoringSession.objects.create(star=star, phases_completed=["restate"], mastery_achieved=True)
        signals = services.gather_meditation_signals(self.tenant)
        stars = signals.get("constellation_stars")
        self.assertTrue(stars)
        self.assertEqual(stars[0]["id"], star.id)
        self.assertEqual(stars[0]["galaxy_note"], "protect the off-days")

    def test_gathers_recent_daily_note_snippets(self):
        from apps.journal.models import Document

        Document.objects.create(
            tenant=self.tenant,
            kind="daily",
            slug=str(timezone.now().date()),
            title="Today",
            markdown="# Daily\nFelt scattered this morning but found focus after a walk.",
        )
        snippets = services.gather_meditation_signals(self.tenant).get("recent_notes")
        self.assertTrue(snippets)
        self.assertIn("found focus after a walk", snippets[0])
        self.assertNotIn("# Daily", snippets[0])  # heading stripped

    def test_format_signals_renders_constellation(self):
        signals = {
            "constellation_stars": [
                {
                    "text": "Name the fear",
                    "stage": "ignited",
                    "galaxy_note": "say it out loud",
                    "journal_entries": [{"text": "it shrank once I named it"}],
                    "tutoring_insights": [{"mastery_achieved": True}],
                }
            ],
        }
        rendered = compose._format_signals(signals)
        self.assertIn("Name the fear", rendered)
        self.assertIn("say it out loud", rendered)
        self.assertIn("settling into", rendered)  # mastery → gentle engagement phrase

    def test_format_signals_universal_fallback_when_empty(self):
        rendered = compose._format_signals({"tenant_id": "x"})
        self.assertIn("little specific signal this week", rendered)


# ═════════════════════════════════════════════════════════════════════
# 9. Richer signals — active goals + consent-scoped fuel summary
# ═════════════════════════════════════════════════════════════════════


class RicherSignalsTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Richer", telegram_chat_id=900900)

    def test_gathers_active_goal_titles_only(self):
        from apps.journal.models import Goal

        Goal.objects.create(tenant=self.tenant, title="Run a 5k", description="secret plan", status=Goal.Status.ACTIVE)
        Goal.objects.create(tenant=self.tenant, title="Finished thing", status=Goal.Status.ACHIEVED)
        signals = services.gather_meditation_signals(self.tenant)
        self.assertIn("active_goals", signals)
        self.assertIn("Run a 5k", signals["active_goals"])
        self.assertNotIn("Finished thing", signals["active_goals"])  # only ACTIVE
        # Titles only — the description must not leak into the signal payload.
        self.assertNotIn("secret plan", " ".join(signals["active_goals"]))

    def test_fuel_summary_only_when_fuel_enabled(self):
        from apps.fuel.models import Workout, WorkoutStatus

        Workout.objects.create(tenant=self.tenant, date=date.today(), status=WorkoutStatus.DONE)
        # Fuel disabled → no fuel signal even though a workout exists.
        self.tenant.fuel_enabled = False
        self.tenant.save(update_fields=["fuel_enabled"])
        self.assertNotIn("fuel_summary", services.gather_meditation_signals(self.tenant))
        # Fuel enabled → the one-line summary appears.
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])
        signals = services.gather_meditation_signals(self.tenant)
        self.assertIn("fuel_summary", signals)
        self.assertIn("1 workout", signals["fuel_summary"])

    def test_fuel_summary_absent_when_no_recent_workouts(self):
        self.tenant.fuel_enabled = True
        self.tenant.save(update_fields=["fuel_enabled"])
        self.assertNotIn("fuel_summary", services.gather_meditation_signals(self.tenant))

    def test_format_signals_renders_goals_and_fuel(self):
        rendered = compose._format_signals(
            {"active_goals": ["Run a 5k"], "fuel_summary": "They completed 3 workouts in the last 7 days."}
        )
        self.assertIn("Run a 5k", rendered)
        self.assertIn("working toward", rendered)
        self.assertIn("3 workouts", rendered)


# ═════════════════════════════════════════════════════════════════════
# 10. Onboarding advancement (profile save → in_progress; first ready → completed)
# ═════════════════════════════════════════════════════════════════════


class OnboardingAdvancementTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Onboarding", telegram_chat_id=901000)

    def test_advance_is_forward_only_and_idempotent(self):
        profile = CoreProfile.objects.create(tenant=self.tenant)  # pending
        self.assertTrue(services.advance_core_onboarding(profile, "in_progress"))
        self.assertEqual(profile.onboarding_status, "in_progress")
        # Idempotent: advancing to a lower/equal rank is a no-op.
        self.assertFalse(services.advance_core_onboarding(profile, "pending"))
        self.assertFalse(services.advance_core_onboarding(profile, "in_progress"))
        self.assertEqual(profile.onboarding_status, "in_progress")

    def test_declined_is_never_auto_advanced(self):
        profile = CoreProfile.objects.create(tenant=self.tenant, onboarding_status="declined")
        self.assertFalse(services.advance_core_onboarding(profile, "completed"))
        self.assertEqual(profile.onboarding_status, "declined")

    def test_profile_patch_advances_pending_to_in_progress(self):
        client = APIClient()
        token = RefreshToken.for_user(self.tenant.user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        # get_or_create leaves it pending; the PATCH (a save) advances it.
        CoreProfile.objects.create(tenant=self.tenant)
        resp = client.patch("/api/v1/core/profile/", {"additional_context": "help me wind down"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["onboarding_status"], "in_progress")

    @override_settings(GEMINI_API_KEY="test-key", API_BASE_URL="https://api.example.test")
    def test_first_ready_meditation_completes_onboarding(self):
        self.tenant.status = Tenant.Status.ACTIVE
        self.tenant.save(update_fields=["status"])
        CoreProfile.objects.create(tenant=self.tenant, onboarding_status="in_progress")
        session = MeditationSession.objects.create(
            tenant=self.tenant, date=date.today(), status=MeditationStatus.PENDING, manifest=_valid_manifest()
        )
        with (
            patch.object(render, "render_manifest_to_audio", return_value=_fake_result()),
            patch.object(services, "upload_workspace_file_binary"),
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(session)
        profile = CoreProfile.objects.get(tenant=self.tenant)
        self.assertEqual(profile.onboarding_status, "completed")


# ═════════════════════════════════════════════════════════════════════
# 11. Feedback PATCH endpoint (thumbs + short note, tenant-scoped)
# ═════════════════════════════════════════════════════════════════════


class MeditationFeedbackViewTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Feedback", telegram_chat_id=901100)
        self.client = APIClient()
        token = RefreshToken.for_user(self.tenant.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.session = MeditationSession.objects.create(
            tenant=self.tenant, date=date.today(), status=MeditationStatus.READY, title="Calm"
        )

    def _url(self, sid=None):
        return f"/api/v1/core/sessions/{sid or self.session.id}/"

    def test_patch_sets_feedback_and_stamps_time(self):
        resp = self.client.patch(
            self._url(), {"user_feedback": "liked", "feedback_note": "  the silences helped  "}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.user_feedback, "liked")
        self.assertEqual(self.session.feedback_note, "the silences helped")  # trimmed
        self.assertIsNotNone(self.session.feedback_at)

    def test_patch_cannot_touch_readonly_fields(self):
        resp = self.client.patch(
            self._url(),
            {"user_feedback": "disliked", "status": "failed", "title": "hacked", "audio_url": "http://evil"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.session.refresh_from_db()
        self.assertEqual(self.session.user_feedback, "disliked")
        self.assertEqual(self.session.status, MeditationStatus.READY)  # unchanged
        self.assertEqual(self.session.title, "Calm")  # unchanged
        self.assertEqual(self.session.audio_url, "")  # unchanged

    def test_invalid_feedback_value_rejected(self):
        resp = self.client.patch(self._url(), {"user_feedback": "amazing"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_put_is_not_allowed(self):
        resp = self.client.put(self._url(), {"user_feedback": "liked"}, format="json")
        self.assertEqual(resp.status_code, 405)

    def test_cannot_patch_another_tenants_session(self):
        other = create_tenant(display_name="Other", telegram_chat_id=901101)
        other_session = MeditationSession.objects.create(
            tenant=other, date=date.today(), status=MeditationStatus.READY, title="Theirs"
        )
        resp = self.client.patch(self._url(other_session.id), {"user_feedback": "liked"}, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_patch_requires_auth(self):
        resp = APIClient().patch(self._url(), {"user_feedback": "liked"}, format="json")
        self.assertEqual(resp.status_code, 401)


# ═════════════════════════════════════════════════════════════════════
# 12. Compose failure is logged to platform_logs (fleet visibility)
# ═════════════════════════════════════════════════════════════════════


class ComposeFailureLoggingTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Compose Log", telegram_chat_id=901200)

    def test_compose_error_records_platform_issue(self):
        from apps.platform_logs.models import PlatformIssueLog

        session = MeditationSession.objects.create(
            tenant=self.tenant, date=date.today(), status=MeditationStatus.PENDING
        )
        with (
            patch.object(compose, "author_manifest", side_effect=compose.ComposeError("all models down")),
            patch.object(services, "render_meditation") as mock_render,
        ):
            services.compose_meditation(session)
        mock_render.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        issues = PlatformIssueLog.objects.filter(tenant=self.tenant, tool_name="core_compose")
        self.assertEqual(issues.count(), 1)
        self.assertIn("all models down", issues.first().detail)
