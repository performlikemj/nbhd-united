"""Hardening tests for the constellation tutoring service (PR #754).

Covers the correctness + production fixes layered on top of test_game.py:
  * mastery auto-close path (the prior CI blocker)
  * cache-backed session round-trip (multi-replica correctness)
  * honest-signal capture from explicit model fields (not phase-advance proxies)
  * connection-id validation rejecting hallucinated / non-existent PKs

All LLM calls are mocked via ``apps.lessons.tutoring._tutor_request`` so no
network or billing fires. These run on CI's Django default test DB.
"""

from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from . import tutoring
from .models import Lesson, LessonConnection, TutoringSession


class TutoringHardeningTests(TestCase):
    """Service-level tests for the cache-backed, honest-signal tutoring flow."""

    def setUp(self):
        # LocMemCache persists across the test process — start each test clean
        # so sessions from a prior test can't bleed in.
        cache.clear()
        self.tenant = create_tenant(display_name="Tutor Tenant", telegram_chat_id=200010)
        self.other_tenant = create_tenant(display_name="Other Tutor Tenant", telegram_chat_id=200011)
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

    # ── Mastery auto-close ──────────────────────────────────────

    @patch("apps.lessons.tutoring._tutor_request")
    def test_mastery_auto_close_returns_mastery_and_session_close(self, mock_tutor):
        """session_complete from the model auto-closes and persists the session."""
        mock_tutor.return_value = {
            "text": "Great exploration — you've made this your own.",
            "current_phase": "apply",
            "phase_complete": True,
            "session_complete": True,
        }
        star = self._create_star(self.tenant)

        start = self.client.post(f"/api/v1/lessons/{star.id}/tutor/start/")
        session_id = start.json()["session_id"]

        resp = self.client.post(
            f"/api/v1/lessons/{star.id}/tutor/message/",
            {"session_id": session_id, "message": "Here's how I'd apply it.", "action": "continue"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("mastery_achieved"))
        self.assertIn("session_close", body)
        self.assertIn("tutoring_session_id", body["session_close"])

        # Session persisted + star advanced past proto.
        self.assertTrue(TutoringSession.objects.filter(star=star).exists())
        star.refresh_from_db()
        self.assertEqual(star.star_stage, "ignited")

    @patch("apps.lessons.tutoring._tutor_request")
    def test_phase_complete_on_last_phase_also_closes(self, mock_tutor):
        """continue_tutoring closes when phase_complete fires while on apply."""
        mock_tutor.return_value = {
            "text": "Let's begin.",
            "current_phase": "restate",
            "phase_complete": False,
        }
        star = self._create_star(self.tenant)

        start = tutoring.start_tutoring(star)
        session_id = start["session_id"]

        # Drive through restate→deepen→stress_test→connect with phase_complete.
        for phase in ["restate", "deepen", "stress_test", "connect"]:
            mock_tutor.return_value = {
                "text": f"Done with {phase}.",
                "current_phase": phase,
                "phase_complete": True,
            }
            result = tutoring.continue_tutoring(session_id, "ok")
            self.assertFalse(result.get("mastery_achieved"))

        # Now on apply: phase_complete (no session_complete) must still close.
        mock_tutor.return_value = {
            "text": "Applied it.",
            "current_phase": "apply",
            "phase_complete": True,
        }
        result = tutoring.continue_tutoring(session_id, "applied")
        self.assertTrue(result.get("mastery_achieved"))

    # ── Cache-backed round-trip ─────────────────────────────────

    @patch("apps.lessons.tutoring._tutor_request")
    def test_cache_backed_session_round_trip(self, mock_tutor):
        """State persists in the cache, not a module dict — simulates another replica."""
        mock_tutor.return_value = {
            "text": "Tell me more.",
            "current_phase": "restate",
            "phase_complete": False,
        }
        star = self._create_star(self.tenant)
        start = tutoring.start_tutoring(star)
        session_id = start["session_id"]

        # State is readable straight from the cache by key.
        cached = cache.get(f"tutoring:{session_id}")
        self.assertIsNotNone(cached)
        self.assertEqual(cached["star_id"], star.id)
        self.assertEqual(cached["tenant_id"], str(self.tenant.id))

        # get_tutoring_state reads it back (as a fresh replica would).
        state = tutoring.get_tutoring_state(session_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["star_id"], star.id)
        self.assertEqual(state["current_phase"], "restate")

        # Continuing reads + rewrites cache rather than relying on in-process state.
        mock_tutor.return_value = {
            "text": "Good — why does that work?",
            "current_phase": "restate",
            "phase_complete": True,
        }
        result = tutoring.continue_tutoring(session_id, "my restatement")
        self.assertEqual(result["session_id"], session_id)
        self.assertEqual(result["current_phase"], "deepen")
        self.assertEqual(cache.get(f"tutoring:{session_id}")["current_phase"], "deepen")

    def test_continue_unknown_session_returns_error(self):
        result = tutoring.continue_tutoring("does-not-exist", "hello")
        self.assertEqual(result.get("error"), "session_not_found")

    @patch("apps.lessons.tutoring._tutor_request")
    def test_end_tutoring_rejects_wrong_tenant(self, mock_tutor):
        """A session bound to one tenant can't be ended by another."""
        mock_tutor.return_value = {"text": "hi", "current_phase": "restate", "phase_complete": False}
        star = self._create_star(self.tenant)
        start = tutoring.start_tutoring(star)
        session_id = start["session_id"]

        wrong = tutoring.end_tutoring(session_id, tenant_id=str(self.other_tenant.id))
        self.assertEqual(wrong.get("error"), "session_not_found")
        # Still endable by the right tenant.
        ok = tutoring.end_tutoring(session_id, tenant_id=str(self.tenant.id))
        self.assertIn("tutoring_session_id", ok)

    # ── Honest signals ──────────────────────────────────────────

    @patch("apps.lessons.tutoring._tutor_request")
    def test_honest_signals_from_explicit_model_fields(self, mock_tutor):
        """restated_accurately / found_edge_cases come from the model, not phase-advance."""
        mock_tutor.return_value = {
            "text": "Let's begin.",
            "current_phase": "restate",
            "phase_complete": False,
        }
        star = self._create_star(self.tenant)
        start = tutoring.start_tutoring(star)
        session_id = start["session_id"]

        # Restate: model advances the phase BUT judges the restatement inaccurate.
        # The old proxy (player_restated_accurately = phase_complete) would have
        # recorded True; the honest signal must record the model's False.
        mock_tutor.return_value = {
            "text": "Let's dig into the why.",
            "current_phase": "restate",
            "phase_complete": True,
            "restated_accurately": False,
        }
        tutoring.continue_tutoring(session_id, "a vague restatement")

        # Stress-test: model reports they DID find edge cases.
        mock_tutor.return_value = {
            "text": "Nice edge case.",
            "current_phase": "stress_test",
            "phase_complete": False,
            "found_edge_cases": True,
        }
        tutoring.continue_tutoring(session_id, "what about when X?")

        cached = cache.get(f"tutoring:{session_id}")
        self.assertIs(cached["player_restated_accurately"], False)
        self.assertIs(cached["player_found_edge_cases"], True)

        # Persisted onto the row at end.
        end = tutoring.end_tutoring(session_id)
        ts = TutoringSession.objects.get(id=end["tutoring_session_id"])
        self.assertIs(ts.player_restated_accurately, False)
        self.assertIs(ts.player_found_edge_cases, True)

    # ── Connection validation ───────────────────────────────────

    @patch("apps.lessons.tutoring._tutor_request")
    def test_connection_id_validation_rejects_nonexistent_pk(self, mock_tutor):
        """Hallucinated ids are dropped; only real neighbor PKs are stored."""
        star = self._create_star(self.tenant, text="Primary star")
        neighbor = self._create_star(self.tenant, text="Neighbor star")
        # Real bidirectional edge so neighbor is a candidate.
        LessonConnection.objects.create(
            from_lesson=star, to_lesson=neighbor, similarity=1.0, connection_type="user_linked"
        )
        LessonConnection.objects.create(
            from_lesson=neighbor, to_lesson=star, similarity=1.0, connection_type="user_linked"
        )

        mock_tutor.return_value = {
            "text": "Let's begin.",
            "current_phase": "restate",
            "phase_complete": False,
        }
        start = tutoring.start_tutoring(star)
        session_id = start["session_id"]

        bogus_id = star.id + neighbor.id + 99999  # guaranteed-nonexistent PK
        mock_tutor.return_value = {
            "text": "You connected these.",
            "current_phase": "connect",
            "phase_complete": False,
            "connections_found": [neighbor.id, bogus_id],
        }
        tutoring.continue_tutoring(session_id, "this links to the neighbor")

        cached = cache.get(f"tutoring:{session_id}")
        stored_ids = [c["to_star_id"] for c in cached["connections_made"]]
        self.assertIn(neighbor.id, stored_ids)
        self.assertNotIn(bogus_id, stored_ids)

    @patch("apps.lessons.tutoring._tutor_request")
    def test_connection_falls_back_to_free_text_without_candidates(self, mock_tutor):
        """With no neighbors, a connection is captured as free text (to_star_id None)."""
        star = self._create_star(self.tenant, text="Lonely star")
        mock_tutor.return_value = {
            "text": "Let's begin.",
            "current_phase": "restate",
            "phase_complete": False,
        }
        start = tutoring.start_tutoring(star)
        session_id = start["session_id"]

        mock_tutor.return_value = {
            "text": "Interesting link.",
            "current_phase": "connect",
            "phase_complete": False,
            # Model invents an id (no candidates) — must be dropped …
            "connections_found": [424242],
            # … but the free-text connection is kept.
            "connection_text": "This reminds me of something I read about habits.",
        }
        tutoring.continue_tutoring(session_id, "it reminds me of habits")

        cached = cache.get(f"tutoring:{session_id}")
        conns = cached["connections_made"]
        self.assertEqual(len(conns), 1)
        self.assertIsNone(conns[0]["to_star_id"])
        self.assertIn("habits", conns[0]["player_text"])

    @patch("apps.lessons.tutoring._tutor_request")
    def test_validated_connections_persist_on_end(self, mock_tutor):
        """end_tutoring writes the validated connections onto the row."""
        star = self._create_star(self.tenant, text="Primary star")
        neighbor = self._create_star(self.tenant, text="Neighbor star")
        LessonConnection.objects.create(
            from_lesson=star, to_lesson=neighbor, similarity=1.0, connection_type="user_linked"
        )
        LessonConnection.objects.create(
            from_lesson=neighbor, to_lesson=star, similarity=1.0, connection_type="user_linked"
        )

        mock_tutor.return_value = {
            "text": "Let's begin.",
            "current_phase": "restate",
            "phase_complete": False,
        }
        start = tutoring.start_tutoring(star)
        session_id = start["session_id"]

        mock_tutor.return_value = {
            "text": "Connected.",
            "current_phase": "connect",
            "phase_complete": False,
            "connections_found": [neighbor.id],
        }
        tutoring.continue_tutoring(session_id, "links to neighbor")

        end = tutoring.end_tutoring(session_id)
        ts = TutoringSession.objects.get(id=end["tutoring_session_id"])
        self.assertEqual(ts.connections_made, [{"to_star_id": neighbor.id, "player_text": ""}])
