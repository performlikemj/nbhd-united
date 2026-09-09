"""Player-only completion evidence, local-day readers, and playable history."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.envelope import render_core
from apps.core.models import MeditationSession, MeditationStatus
from apps.core.services import _recent_meditation_entries
from apps.insights.snapshots import compute_core_snapshot
from apps.insights.yesterdays_signals import _core_signals
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True, NBHD_INTERNAL_API_KEY="test-internal-key")
class CompletionTruthTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Completion", telegram_chat_id=901990)
        self.tenant.user.timezone = "Asia/Tokyo"
        self.tenant.user.save(update_fields=["timezone"])
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.tenant.user).access_token}")
        # UTC yesterday, but today in Tokyo. Composed a day before listening.
        self.now = datetime(2026, 9, 7, 16, tzinfo=UTC)
        self.today = date(2026, 9, 8)
        self.yesterday = self.today - timedelta(days=1)
        self.session = MeditationSession.objects.create(
            tenant=self.tenant, date=self.yesterday, status=MeditationStatus.READY, title="A new sit", theme="Attention"
        )
        self.url = f"/api/v1/core/sessions/{self.session.id}/complete/"

    def test_complete_stamps_once_and_returns_detail_contract(self):
        with patch("apps.core.views.timezone.now", return_value=self.now):
            first = self.client.post(self.url, {"listened_seconds": 600}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["status"], "done")
        self.assertEqual(first.data["lesson"], {})
        self.session.refresh_from_db()
        self.assertEqual(self.session.completed_at, self.now)
        updated_at = self.session.updated_at
        second = self.client.post(self.url, {}, format="json")
        self.assertEqual(second.data, first.data)
        self.session.refresh_from_db()
        self.assertEqual(self.session.updated_at, updated_at)
        self.assertEqual(self.client.get(self.url.removesuffix("complete/")).data, first.data)

    def test_delivered_is_completable_but_unrendered_states_conflict(self):
        for state in ("pending", "rendering", "failed", "delivered"):
            with self.subTest(state=state):
                MeditationSession.objects.filter(pk=self.session.pk).update(status=state)
                response = self.client.post(self.url, {}, format="json")
                if state == "delivered":
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.data, {"error": "not_ready"})

    def test_other_tenant_is_404_and_jwt_is_required(self):
        other = create_tenant(display_name="Other", telegram_chat_id=901991)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(other.user).access_token}")
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 404)
        self.client.credentials()
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 401)
        self.assertEqual(self.client.post(self.url, {}, format="json", **self._headers()).status_code, 401)

    def _headers(self):
        return {"HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}

    def test_all_readers_use_done_and_completion_local_day(self):
        for state in ("ready", "delivered", "pending", "rendering", "failed"):
            MeditationSession.objects.create(tenant=self.tenant, date=self.today, status=state, completed_at=self.now)
        with patch("apps.core.views.timezone.now", return_value=self.now):
            self.client.post(self.url, {}, format="json")
        signals = _core_signals(self.tenant, today=self.today, yesterday=self.yesterday)
        self.assertEqual(signals["yesterday"]["sessions"], 0)
        self.assertEqual(signals["today_so_far"]["sessions"], 1)
        self.assertIsNone(signals["days_since_last_session"])
        snapshot = compute_core_snapshot(self.tenant, today=self.today)
        self.assertEqual(snapshot["totals"], {"sessions_7d": 1, "sessions_28d": 1, "practice_streak_days": 1})
        self.assertEqual(snapshot["last_session_date"], "2026-09-08")
        with patch("apps.common.tenant_tz.tenant_today", return_value=self.today):
            envelope = render_core(self.tenant)
        self.assertIn("**Last completed sit**: A new sit (2026-09-08)", envelope)
        self.assertIn("1 completed sit(s)", envelope)
        self.assertIn("(not yet listened)", envelope)
        from apps.router.siri_spoken import compose_spoken_status

        self.tenant.core_enabled = True
        with patch("apps.common.tenant_tz.tenant_today", return_value=self.today):
            self.assertIn("1 meditation this week", compose_spoken_status(self.tenant))
        self.client.credentials()
        summary = self.client.get(f"/api/v1/core/runtime/{self.tenant.id}/summary/", **self._headers()).data
        self.assertEqual(summary["total_sessions"], 1)
        self.assertEqual(summary["last"]["id"], str(self.session.id))
        self.assertEqual(summary["last"]["date"], "2026-09-07")
        self.assertEqual(summary["last"]["completed_at"], self.now.isoformat())
        self.assertIsNotNone(summary["ready_unplayed"])

    def test_ready_only_is_not_completion_evidence(self):
        with patch("apps.common.tenant_tz.tenant_today", return_value=self.today):
            envelope = render_core(self.tenant)
        self.assertNotIn("Last completed", envelope)
        self.assertIn("0 completed sit(s)", envelope)
        self.assertIn("Ready to play", envelope)
        self.client.credentials()
        summary = self.client.get(f"/api/v1/core/runtime/{self.tenant.id}/summary/", **self._headers()).data
        self.assertEqual(summary["total_sessions"], 0)
        self.assertIsNone(summary["last"])

    def test_done_remains_in_library_and_variety_history(self):
        self.client.post(self.url, {}, format="json")
        response = self.client.get("/api/v1/core/sessions/")
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(rows[0]["id"], str(self.session.id))
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(_recent_meditation_entries(self.tenant)[0]["title"], "A new sit")
        # The feedback path cannot forge or erase completion evidence.
        self.client.patch(
            self.url.removesuffix("complete/"),
            {"status": "ready", "completed_at": None, "lesson": {"tradition": "zen"}},
            format="json",
        )
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "done")
        self.assertIsNotNone(self.session.completed_at)
        self.assertEqual(self.session.lesson, {})

    def test_compose_persists_lesson_through_registered_pii_leaves(self):
        from apps.core.services import compose_meditation
        from apps.core.tests import _valid_manifest
        from apps.pii.authoring import AuthoredText

        self.session.status = MeditationStatus.PENDING
        self.session.save(update_fields=["status"])
        manifest = _valid_manifest()
        for field in ("core_teaching", "summary", "practice"):
            manifest["lesson"][field] = f"PrivateName {field}"

        def author_leaf(tenant, text, **kwargs):
            return AuthoredText(text=text.replace("PrivateName", "[PERSON_1]"), receipt={"state": "clean"})

        with (
            patch("apps.core.compose.author_manifest", return_value=manifest),
            patch("apps.core.services.gather_meditation_signals", return_value={}),
            patch("apps.pii.authoring.author_text", side_effect=author_leaf) as leaf,
            patch("apps.cron.publish.publish_task"),
        ):
            compose_meditation(self.session)
        self.session.refresh_from_db()
        self.assertEqual(self.session.lesson["tradition"], "taoist")
        self.assertEqual(self.session.lesson["teaching_slug"], "wu-wei")
        for field in ("core_teaching", "summary", "practice"):
            self.assertEqual(self.session.lesson[field], f"[PERSON_1] {field}")
        lesson_calls = [c for c in leaf.call_args_list if c.kwargs["field"] == "lesson"]
        self.assertEqual(len(lesson_calls), 3)
        self.assertTrue(all(c.kwargs["writer"] == "background" for c in lesson_calls))
        self.assertIn("lesson", self.session.pii_receipts)
        self.session.status = MeditationStatus.READY
        self.session.save(update_fields=["status"])
        self.assertEqual(
            _recent_meditation_entries(self.tenant)[0]["lesson"]["core_teaching"], "[PERSON_1] core_teaching"
        )
