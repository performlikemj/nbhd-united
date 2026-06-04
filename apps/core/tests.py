"""Core pillar tests — render engine (pure math + validation), the render
orchestration (mocked TTS/ffmpeg), notify-on-ready, manifest validation at the
runtime boundary, and a real-ffmpeg stitch test (skipped when ffmpeg is absent).

Live Gemini TTS is never called in CI; the orchestration tests mock the engine,
and the real-ffmpeg test renders placeholder tones (no key, no network).
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from unittest import TestCase as UnitTestCase
from unittest import skipUnless
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core import render, services
from apps.core.models import MeditationSession, MeditationStatus
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

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
        m["phases"][0]["segments"][1]["seconds"] = 90  # > 30
        errors = render.validate_manifest(m)
        self.assertTrue(any("out of" in e for e in errors))

    def test_non_closing_phase_without_flex_rejected(self):
        m = _valid_manifest()
        # arrival keeps a speech + a fixed silence but loses its flex.
        m["phases"][0]["segments"] = [
            {"type": "speech", "text": "Welcome.", "tone": "warm"},
            {"type": "silence", "seconds": 8},
        ]
        errors = render.validate_manifest(m)
        self.assertTrue(any("flex" in e for e in errors))

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
        self.assertIn("invalid_manifest", session.error)

    @override_settings(GEMINI_API_KEY="")
    def test_missing_api_key_marks_failed(self):
        session = self._session()
        with patch.object(render, "render_manifest_to_audio") as mock_render:
            services.render_meditation(session)
        mock_render.assert_not_called()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertIn("GEMINI_API_KEY", session.error)

    def test_transient_error_marks_failed_and_reraises(self):
        session = self._session()
        with (
            patch.object(render, "render_manifest_to_audio", side_effect=RuntimeError("ffmpeg boom")),
            self.assertRaises(RuntimeError),
        ):
            services.render_meditation(session)
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertIn("render_error", session.error)

    def test_quota_marks_failed_without_reraise(self):
        session = self._session()
        with patch.object(render, "render_manifest_to_audio", side_effect=render.QuotaExceeded("429")):
            services.render_meditation(session)  # must NOT raise
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.FAILED)
        self.assertIn("tts_quota", session.error)

    def test_failed_session_can_be_reclaimed(self):
        session = self._session(status=MeditationStatus.FAILED)
        with (
            patch.object(render, "render_manifest_to_audio", return_value=_fake_result()) as mock_render,
            patch.object(services, "upload_workspace_file_binary"),
            patch.object(services, "notify_meditation_ready"),
        ):
            services.render_meditation(session)
        mock_render.assert_called_once()
        session.refresh_from_db()
        self.assertEqual(session.status, MeditationStatus.READY)

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
        self.assertIn("persist_error", session.error)
        mock_notify.assert_not_called()

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
        user = self.tenant.user
        user.line_user_id = "U" + "a" * 32
        user.preferred_channel = "line"
        user.save(update_fields=["line_user_id", "preferred_channel"])
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
        user = self.tenant.user
        user.line_user_id = "U" + "b" * 32
        user.preferred_channel = "line"
        user.save(update_fields=["line_user_id", "preferred_channel"])
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

    def test_auth_required(self):
        resp = self.client.post(self._url(), {"manifest": _valid_manifest()}, format="json")
        self.assertEqual(resp.status_code, 401)


class RenderMeditationTaskTests(TestCase):
    """The QStash entry point: load-by-id + idempotency precheck (tasks.py)."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Task Test", telegram_chat_id=900400)

    def _session(self, status=MeditationStatus.PENDING):
        return MeditationSession.objects.create(tenant=self.tenant, date=date.today(), status=status, title="T")

    def test_missing_session_returns_cleanly(self):
        from apps.core import tasks

        with patch.object(services, "render_meditation") as mock_render:
            tasks.render_meditation_task(str(uuid4()))  # bogus id — must not raise
        mock_render.assert_not_called()

    def test_ready_session_is_skipped(self):
        from apps.core import tasks

        session = self._session(status=MeditationStatus.READY)
        with patch.object(services, "render_meditation") as mock_render:
            tasks.render_meditation_task(str(session.id))
        mock_render.assert_not_called()

    def test_pending_session_dispatches_once(self):
        from apps.core import tasks

        session = self._session(status=MeditationStatus.PENDING)
        with patch.object(services, "render_meditation") as mock_render:
            tasks.render_meditation_task(str(session.id))
        mock_render.assert_called_once()


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
