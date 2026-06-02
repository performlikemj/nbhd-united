"""Tests for galaxy, tutoring, star journal, and star action endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .models import Lesson, StarJournalEntry, TutoringSession


class ConstellationGameTests(TestCase):
    """Tests for the constellation game endpoints: galaxy, tutoring, star journal."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Game Tenant",
            telegram_chat_id=100010,
        )
        self.other_tenant = create_tenant(
            display_name="Other Tenant",
            telegram_chat_id=100011,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _create_star(self, tenant: Tenant, **overrides):
        defaults = {
            "text": "A test lesson",
            "context": "from testing",
            "source_type": "experience",
            "source_ref": "test-1",
            "tags": ["test"],
            "status": "approved",
            "star_stage": "proto",
            "position_x": 0.5,
            "position_y": -0.3,
        }
        defaults.update(overrides)
        return Lesson.objects.create(tenant=tenant, **defaults)

    # ── Galaxy endpoint ────────────────────────────────────────

    def test_galaxy_returns_stars_with_game_state(self):
        star = self._create_star(self.tenant, text="Star one", star_stage="radiant")

        resp = self.client.get("/api/v1/lessons/galaxy/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertIn("stars", body)
        self.assertIn("edges", body)
        self.assertEqual(len(body["stars"]), 1)
        self.assertEqual(body["stars"][0]["id"], star.id)
        self.assertEqual(body["stars"][0]["star_stage"], "radiant")
        self.assertEqual(body["stars"][0]["x"], 0.5)
        self.assertEqual(body["stars"][0]["y"], -0.3)
        self.assertEqual(body["stars"][0]["journal_count"], 0)

    def test_galaxy_returns_journal_count(self):
        star = self._create_star(self.tenant)
        StarJournalEntry.objects.create(tenant=self.tenant, star=star, text="Entry 1", entry_type="free")
        StarJournalEntry.objects.create(tenant=self.tenant, star=star, text="Entry 2", entry_type="tutoring")

        resp = self.client.get("/api/v1/lessons/galaxy/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["stars"][0]["journal_count"], 2)

    def test_galaxy_excludes_non_approved(self):
        self._create_star(self.tenant, status="approved")
        self._create_star(self.tenant, text="Pending star", status="pending")
        self._create_star(self.tenant, text="Dismissed star", status="dismissed")

        resp = self.client.get("/api/v1/lessons/galaxy/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["stars"]), 1)

    def test_galaxy_tenant_isolation(self):
        self._create_star(self.tenant, text="My star")
        self._create_star(self.other_tenant, text="Their star")

        resp = self.client.get("/api/v1/lessons/galaxy/")
        self.assertEqual(resp.status_code, 200)
        texts = [s["text"] for s in resp.json()["stars"]]
        self.assertIn("My star", texts)
        self.assertNotIn("Their star", texts)

    # ── Galaxy summary ─────────────────────────────────────────

    def test_galaxy_summary_counts_by_stage(self):
        self._create_star(self.tenant, star_stage="proto")
        self._create_star(self.tenant, text="Star 2", star_stage="ignited")
        self._create_star(self.tenant, text="Star 3", star_stage="radiant")
        self._create_star(self.tenant, text="Star 4", star_stage="supernova")

        resp = self.client.get("/api/v1/lessons/galaxy/summary/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(body["total_stars"], 4)
        self.assertEqual(body["proto_count"], 1)
        self.assertEqual(body["ignited_count"], 1)
        self.assertEqual(body["radiant_count"], 1)
        self.assertEqual(body["supernova_count"], 1)

    # ── Star landing ───────────────────────────────────────────

    def test_land_sets_last_visited_at(self):
        star = self._create_star(self.tenant)
        self.assertIsNone(star.last_visited_at)

        resp = self.client.post(f"/api/v1/lessons/{star.id}/land/")
        self.assertEqual(resp.status_code, 200)

        star.refresh_from_db()
        self.assertIsNotNone(star.last_visited_at)
        body = resp.json()
        self.assertEqual(body["star_stage"], "proto")
        self.assertIn("journal_entries", body)

    def test_land_rejects_non_approved_star(self):
        star = self._create_star(self.tenant, status="pending")

        resp = self.client.post(f"/api/v1/lessons/{star.id}/land/")
        self.assertEqual(resp.status_code, 400)

    def test_land_tenant_isolation(self):
        other_star = self._create_star(self.other_tenant)

        resp = self.client.post(f"/api/v1/lessons/{other_star.id}/land/")
        self.assertEqual(resp.status_code, 404)

    # ── Tutoring ───────────────────────────────────────────────

    @patch("apps.lessons.tutoring._tutor_request")
    def test_tutor_start_returns_session(self, mock_tutor):
        mock_tutor.return_value = {
            "text": "Let's explore this lesson. Can you explain it in your own words?",
            "current_phase": "restate",
            "phase_complete": False,
        }
        star = self._create_star(self.tenant)

        resp = self.client.post(f"/api/v1/lessons/{star.id}/tutor/start/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertIn("session_id", body)
        self.assertEqual(body["current_phase"], "restate")
        self.assertEqual(body["phase_index"], 0)
        self.assertEqual(body["total_phases"], 5)
        self.assertIsNotNone(body["message"])

    @patch("apps.lessons.tutoring._tutor_request")
    def test_tutor_message_continues_conversation(self, mock_tutor):
        mock_tutor.return_value = {
            "text": "Good — and why do you think that matters?",
            "current_phase": "restate",
            "phase_complete": True,
        }
        star = self._create_star(self.tenant)

        # Start session
        start = self.client.post(f"/api/v1/lessons/{star.id}/tutor/start/")
        session_id = start.json()["session_id"]

        # Send a message
        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/tutor/message/",
            {
                "session_id": session_id,
                "message": "The lesson is about temperature control in cooking.",
                "action": "continue",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["session_id"], session_id)
        self.assertIn("message", body)

    def test_tutor_message_requires_session_id(self):
        star = self._create_star(self.tenant)
        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/tutor/message/",
            {"message": "Hello", "action": "continue"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    @patch("apps.lessons.tutoring._tutor_request")
    def test_tutor_skip_advances_phase(self, mock_tutor):
        mock_tutor.return_value = {
            "text": "Okay, let's explore the 'why' behind this lesson...",
            "current_phase": "deepen",
            "phase_complete": False,
        }
        star = self._create_star(self.tenant)

        start = self.client.post(f"/api/v1/lessons/{star.id}/tutor/start/")
        session_id = start.json()["session_id"]

        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/tutor/message/",
            {
                "session_id": session_id,
                "message": "skip",
                "action": "skip",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    @patch("apps.lessons.tutoring._tutor_request")
    def test_tutor_end_persists_session_and_updates_star(self, mock_tutor):
        mock_tutor.return_value = {
            "text": "You've explained this well.",
            "current_phase": "restate",
            "phase_complete": True,
        }
        star = self._create_star(self.tenant)

        start = self.client.post(f"/api/v1/lessons/{star.id}/tutor/start/")
        session_id = start.json()["session_id"]

        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/tutor/end/",
            {"session_id": session_id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertIn("tutoring_session_id", body)
        self.assertEqual(body["new_star_stage"], "ignited")

        # Star should be updated
        star.refresh_from_db()
        self.assertEqual(star.star_stage, "ignited")
        self.assertEqual(star.tutoring_sessions_count, 1)
        self.assertIsNotNone(star.last_tutored_at)

        # Tutoring session should be persisted
        self.assertTrue(TutoringSession.objects.filter(id=body["tutoring_session_id"]).exists())

    @patch("apps.lessons.tutoring._tutor_request")
    def test_mastery_triggers_auto_close(self, mock_tutor):
        """When the tutor returns session_complete, the session auto-closes."""
        mock_tutor.return_value = {
            "text": "That was a thorough exploration. You've mastered this.",
            "current_phase": "apply",
            "phase_complete": True,
            "session_complete": True,
        }
        star = self._create_star(self.tenant)

        start = self.client.post(f"/api/v1/lessons/{star.id}/tutor/start/")
        session_id = start.json()["session_id"]

        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/tutor/message/",
            {
                "session_id": session_id,
                "message": "Here's how I'd apply this to my current situation...",
                "action": "continue",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json().get("mastery_achieved"))
        self.assertIn("session_close", resp.json())

        star.refresh_from_db()
        self.assertEqual(star.star_stage, "ignited")

    @patch("apps.lessons.tutoring._tutor_request")
    def test_radiant_after_three_sessions(self, mock_tutor):
        mock_tutor.return_value = {
            "text": "Great.",
            "current_phase": "restate",
            "phase_complete": True,
        }
        star = self._create_star(self.tenant, star_stage="ignited", tutoring_sessions_count=2)
        # Add some journal entries to bump engagement
        StarJournalEntry.objects.create(tenant=self.tenant, star=star, text="Reflection 1")
        StarJournalEntry.objects.create(tenant=self.tenant, star=star, text="Reflection 2")

        start = self.client.post(f"/api/v1/lessons/{star.id}/tutor/start/")
        session_id = start.json()["session_id"]

        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/tutor/end/",
            {"session_id": session_id},
            format="json",
        )
        self.assertEqual(resp.json()["new_star_stage"], "radiant")

    # ── Star journal ───────────────────────────────────────────

    def test_star_journal_create_and_list(self):
        star = self._create_star(self.tenant)

        create = self.client.post(
            f"/api/v1/lessons/{star.id}/journal/create/",
            {"text": "This lesson changed how I cook.", "entry_type": "tutoring", "tags": ["cooking"]},
            format="json",
        )
        self.assertEqual(create.status_code, 201)
        body = create.json()
        self.assertEqual(body["text"], "This lesson changed how I cook.")
        self.assertEqual(body["entry_type"], "tutoring")
        self.assertEqual(body["star"], star.id)

        # List
        list_resp = self.client.get(f"/api/v1/lessons/{star.id}/journal/")
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.json()), 1)

    def test_star_journal_tenant_isolation(self):
        star = self._create_star(self.tenant)
        other_star = self._create_star(self.other_tenant)

        # Try to create entry on another tenant's star
        resp = self.client.post(
            f"/api/v1/lessons/{other_star.id}/journal/create/",
            {"text": "Should fail", "entry_type": "free"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    # ── Pin note ───────────────────────────────────────────────

    def test_pin_note_updates_galaxy_note(self):
        star = self._create_star(self.tenant)

        resp = self.client.patch(
            f"/api/v1/lessons/{star.id}/pin-note/",
            {"note": "My pinned galaxy note"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        star.refresh_from_db()
        self.assertEqual(star.galaxy_note, "My pinned galaxy note")

    # ── Manual connect ─────────────────────────────────────────

    def test_connect_creates_bidirectional_edge(self):
        star1 = self._create_star(self.tenant, text="Star 1")
        star2 = self._create_star(self.tenant, text="Star 2")

        resp = self.client.post(
            f"/api/v1/lessons/{star1.id}/connect/",
            {"target_star_id": star2.id, "connection_type": "user_linked"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["source"], star1.id)
        self.assertEqual(resp.json()["target"], star2.id)

        # Bidirectional
        from .models import LessonConnection

        self.assertTrue(
            LessonConnection.objects.filter(from_lesson=star1, to_lesson=star2, connection_type="user_linked").exists()
        )
        self.assertTrue(
            LessonConnection.objects.filter(from_lesson=star2, to_lesson=star1, connection_type="user_linked").exists()
        )

    def test_connect_cannot_self_connect(self):
        star = self._create_star(self.tenant)

        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/connect/",
            {"target_star_id": star.id, "connection_type": "user_linked"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_connect_tenant_isolation(self):
        star = self._create_star(self.tenant)
        other_star = self._create_star(self.other_tenant)

        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/connect/",
            {"target_star_id": other_star.id, "connection_type": "user_linked"},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    # ── Auth ───────────────────────────────────────────────────

    def test_game_endpoints_require_auth(self):
        unauth = APIClient()
        star = self._create_star(self.tenant)

        endpoints = [
            ("get", "/api/v1/lessons/galaxy/"),
            ("get", "/api/v1/lessons/galaxy/summary/"),
            ("post", f"/api/v1/lessons/{star.id}/land/"),
            ("post", f"/api/v1/lessons/{star.id}/tutor/start/"),
            ("get", f"/api/v1/lessons/{star.id}/journal/"),
            ("post", f"/api/v1/lessons/{star.id}/journal/create/"),
            ("patch", f"/api/v1/lessons/{star.id}/pin-note/"),
            ("post", f"/api/v1/lessons/{star.id}/connect/"),
        ]

        for method, url in endpoints:
            if method == "get":
                resp = unauth.get(url)
            else:
                resp = unauth.post(url, {}, format="json") if method == "post" else unauth.patch(url, {}, format="json")
            self.assertEqual(resp.status_code, 401, f"{method.upper()} {url} should require auth")

    def test_game_endpoints_tenant_isolation_404(self):
        """Endpoints on another tenant's star return 404."""
        other_star = self._create_star(self.other_tenant)

        endpoints = [
            ("post", f"/api/v1/lessons/{other_star.id}/land/"),
            ("post", f"/api/v1/lessons/{other_star.id}/tutor/start/"),
            ("get", f"/api/v1/lessons/{other_star.id}/journal/"),
            ("post", f"/api/v1/lessons/{other_star.id}/journal/create/"),
        ]

        for method, url in endpoints:
            if method == "get":
                resp = self.client.get(url)
            else:
                resp = self.client.post(url, {"text": "test"}, format="json")
            self.assertEqual(resp.status_code, 404, f"{method.upper()} {url}")


class TutoringStageComputationTests(TestCase):
    """Unit tests for star stage computation logic."""

    def setUp(self):
        self.tenant = create_tenant(display_name="Stage Tenant", telegram_chat_id=100012)
        self.star = Lesson.objects.create(
            tenant=self.tenant,
            text="Test lesson",
            context="test",
            source_type="experience",
            tags=["test"],
            status="approved",
            star_stage="proto",
            tutoring_sessions_count=0,
        )

    def test_proto_to_ignited_first_session(self):
        from .tutoring import _compute_star_stage

        state = MagicMock()
        state.star = self.star
        state.connection_count = 0

        self.assertEqual(_compute_star_stage(state), "ignited")

    def test_ignited_to_radiant_after_three_sessions(self):
        from .tutoring import _compute_star_stage

        self.star.tutoring_sessions_count = 2  # +1 for this session = 3
        self.star.save()

        state = MagicMock()
        state.star = self.star

        self.assertEqual(_compute_star_stage(state), "radiant")

    def test_radiant_to_supernova_after_eight_sessions(self):
        from .tutoring import _compute_star_stage

        self.star.tutoring_sessions_count = 7  # +1 = 8
        self.star.save()

        state = MagicMock()
        state.star = self.star

        self.assertEqual(_compute_star_stage(state), "supernova")

    def test_supernova_from_journal_entries(self):
        from .tutoring import _compute_star_stage

        self.star.tutoring_sessions_count = 0
        self.star.save()
        for i in range(8):
            StarJournalEntry.objects.create(tenant=self.tenant, star=self.star, text=f"Entry {i}")

        state = MagicMock()
        state.star = self.star

        self.assertEqual(_compute_star_stage(state), "supernova")
