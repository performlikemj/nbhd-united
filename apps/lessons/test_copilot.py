"""Tests for the galaxy co-pilot — spatial-evidence builder + reflect endpoint.

Two layers, per the continuity spec's verification plan:
  * Pure unit tests of ``build_spatial_context`` / ``_pick_suggestion`` (the
    spatial evidence — no LLM, no I/O).
  * Endpoint tests of ``POST galaxy/reflect/``: tenant scoping, the deterministic
    fallback when the LLM is off, the LLM path (mocked), PII rehydration of the
    returned line, and caching.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.models import Tenant

from . import copilot
from .models import Lesson, LessonConnection


def _make_tenant(name: str, chat_id: int) -> Tenant:
    from apps.tenants.services import create_tenant

    return create_tenant(display_name=name, telegram_chat_id=chat_id)


def _make_star(tenant: Tenant, text: str, **overrides) -> Lesson:
    defaults = {
        "text": text,
        "context": "from testing",
        "source_type": "experience",
        "source_ref": "test",
        "tags": ["test"],
        "status": "approved",
        "star_stage": "proto",
        "position_x": 0.0,
        "position_y": 0.0,
    }
    defaults.update(overrides)
    return Lesson.objects.create(tenant=tenant, **defaults)


class SpatialContextTests(TestCase):
    """The pure evidence builder — the part the LLM only phrases."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant = _make_tenant("Spatial Tenant", 200300)

    def test_nearest_is_idea_space_distance_to_target(self):
        target = _make_star(self.tenant, "anchor", position_x=0.0, position_y=0.0)
        near = _make_star(self.tenant, "near idea", position_x=0.1, position_y=0.0)
        far = _make_star(self.tenant, "far idea", position_x=0.9, position_y=0.9)

        ctx = copilot.build_spatial_context(target, [target, near, far], {}, [])

        ids = [n["id"] for n in ctx["nearest"]]
        self.assertEqual(ids[0], near.id)  # closest in PCA space comes first
        self.assertIn(far.id, ids)
        self.assertNotIn(target.id, ids)  # target never lists itself

    def test_cluster_state_counts_visited_and_staleness(self):
        now = timezone.now()
        recent = now - timezone.timedelta(days=2)
        old = now - timezone.timedelta(days=40)
        target = _make_star(self.tenant, "t", cluster_id=1, cluster_label="Finance", last_visited_at=old)
        sib_fresh = _make_star(self.tenant, "s1", cluster_id=1, cluster_label="Finance", last_visited_at=recent)
        sib_old = _make_star(self.tenant, "s2", cluster_id=1, cluster_label="Finance")

        ctx = copilot.build_spatial_context(target, [target, sib_fresh, sib_old], {}, [], now=now)

        self.assertEqual(ctx["cluster"]["label"], "Finance")
        self.assertEqual(ctx["cluster"]["size"], 3)
        self.assertEqual(ctx["cluster"]["visited"], 2)  # target + sib_fresh
        self.assertFalse(ctx["cluster"]["stale"])  # sib_fresh visited 2 days ago

    def test_recent_path_resolves_newest_first_and_drops_target(self):
        target = _make_star(self.tenant, "current")
        a = _make_star(self.tenant, "came from A")
        b = _make_star(self.tenant, "came from B")

        ctx = copilot.build_spatial_context(target, [target, a, b], {}, [a.id, b.id, target.id])

        texts = [r["text"] for r in ctx["recent_path"]]
        self.assertEqual(texts, ["came from A", "came from B"])  # order preserved, target excluded

    def test_suggestion_prefers_nearby_unvisited(self):
        target = _make_star(self.tenant, "t", position_x=0.0, position_y=0.0, last_visited_at=timezone.now())
        unvisited = _make_star(self.tenant, "unopened", position_x=0.05, position_y=0.0)
        visited = _make_star(self.tenant, "opened", position_x=0.02, position_y=0.0, last_visited_at=timezone.now())

        ctx = copilot.build_spatial_context(target, [target, unvisited, visited], {}, [])

        self.assertIsNotNone(ctx["suggestion"])
        self.assertEqual(ctx["suggestion"]["id"], unvisited.id)
        self.assertEqual(ctx["suggestion"]["reason"], "nearby_unvisited")

    def test_suggestion_falls_back_to_drifted_bright_star(self):
        now = timezone.now()
        target = _make_star(self.tenant, "t", position_x=0.0, position_y=0.0, last_visited_at=now)
        # Only neighbour is already visited → no nearby_unvisited; it's radiant and stale.
        drifted = _make_star(
            self.tenant,
            "bright but neglected",
            position_x=0.1,
            position_y=0.0,
            star_stage="radiant",
            last_visited_at=now - timezone.timedelta(days=60),
        )

        ctx = copilot.build_spatial_context(target, [target, drifted], {}, [], now=now)

        self.assertEqual(ctx["suggestion"]["id"], drifted.id)
        self.assertEqual(ctx["suggestion"]["reason"], "drifted_bright")

    def test_similar_uses_edges(self):
        target = _make_star(self.tenant, "t")
        linked = _make_star(self.tenant, "linked idea")
        ctx = copilot.build_spatial_context(target, [target, linked], {target.id: [(linked.id, 0.91)]}, [])
        self.assertEqual(ctx["similar"][0]["id"], linked.id)


class ReflectEndpointTests(TestCase):
    """POST /api/v1/lessons/galaxy/reflect/."""

    def setUp(self):
        self.tenant = _make_tenant("Reflect Tenant", 200310)
        self.other = _make_tenant("Other Reflect Tenant", 200311)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        self.star = _make_star(self.tenant, "Small steps compound into big change.")

    @override_settings(COPILOT_LLM_ENABLED=False)
    def test_fallback_line_when_llm_disabled(self):
        resp = self.client.post(
            "/api/v1/lessons/galaxy/reflect/",
            {"star_id": self.star.id, "recent_star_ids": []},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["source"], "fallback")
        self.assertTrue(body["line"])
        self.assertIn("point", body)

    @override_settings(COPILOT_LLM_ENABLED=True)
    @patch("apps.lessons.copilot._copilot_request", return_value="A grounded little line.")
    def test_llm_line_when_enabled(self, mock_req):
        resp = self.client.post(
            "/api/v1/lessons/galaxy/reflect/",
            {"star_id": self.star.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["source"], "llm")
        self.assertEqual(body["line"], "A grounded little line.")
        mock_req.assert_called_once()

    @override_settings(COPILOT_LLM_ENABLED=True)
    @patch("apps.lessons.copilot._copilot_request")
    def test_llm_failure_degrades_to_fallback(self, mock_req):
        mock_req.side_effect = RuntimeError("openrouter 401")
        resp = self.client.post(
            "/api/v1/lessons/galaxy/reflect/",
            {"star_id": self.star.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["source"], "fallback")
        self.assertTrue(body["line"])

    @override_settings(COPILOT_LLM_ENABLED=True)
    @patch("apps.lessons.copilot._copilot_request", return_value="You and [PERSON_1] keep circling this.")
    def test_response_is_rehydrated(self, _mock_req):
        # Tenant has a known placeholder; the model echoed it — it must be
        # rehydrated before reaching the panel, never leaked as a token.
        self.tenant.pii_entity_map = {"[PERSON_1]": "Sam"}
        self.tenant.save(update_fields=["pii_entity_map"])

        resp = self.client.post(
            "/api/v1/lessons/galaxy/reflect/",
            {"star_id": self.star.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        line = resp.json()["line"]
        self.assertIn("Sam", line)
        self.assertNotIn("[PERSON_1]", line)

    @override_settings(COPILOT_LLM_ENABLED=True)
    @patch("apps.lessons.copilot._copilot_request", return_value="Ask [PERSON_9] what they think.")
    def test_hallucinated_placeholder_is_scrubbed(self, _mock_req):
        # The model emitted a placeholder that maps to nothing — it must be
        # scrubbed at the egress boundary, never leaked to the panel.
        resp = self.client.post(
            "/api/v1/lessons/galaxy/reflect/",
            {"star_id": self.star.id},
            format="json",
        )
        line = resp.json()["line"]
        self.assertNotIn("[PERSON_9]", line)
        self.assertIn("someone", line)

    def test_finalize_egress_rehydrates_live_and_scrubs(self):
        # Egress-boundary contract: rehydrate against the LIVE tenant map UNIONED
        # with the call's fresh mints, scrub any unmapped token, and drop _mints.
        self.tenant.pii_entity_map = {"[PERSON_1]": "Sam"}
        result = {
            "line": "You, [PERSON_1] and [PERSON_2] meet [PERSON_9].",
            "point": {"star_id": 1, "label": "[PERSON_1]'s note", "reason": "x"},
            "source": "llm",
            "_mints": {"[PERSON_2]": "Pat"},
        }
        out = copilot.finalize_egress(self.tenant, result)
        self.assertEqual(out["line"], "You, Sam and Pat meet someone.")
        self.assertEqual(out["point"]["label"], "Sam's note")
        self.assertNotIn("_mints", out)

    @override_settings(COPILOT_LLM_ENABLED=False)
    def test_tenant_isolation_404_on_other_tenant_star(self):
        their_star = _make_star(self.other, "Their private lesson.")
        resp = self.client.post(
            "/api/v1/lessons/galaxy/reflect/",
            {"star_id": their_star.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_requires_auth(self):
        resp = APIClient().post("/api/v1/lessons/galaxy/reflect/", {"star_id": self.star.id}, format="json")
        self.assertEqual(resp.status_code, 401)

    @override_settings(COPILOT_LLM_ENABLED=True)
    @patch("apps.lessons.copilot._copilot_request", return_value="cached line")
    def test_llm_line_is_cached(self, mock_req):
        payload = {"star_id": self.star.id, "ship": {"x": 10.0, "y": 10.0}}
        first = self.client.post("/api/v1/lessons/galaxy/reflect/", payload, format="json")
        second = self.client.post("/api/v1/lessons/galaxy/reflect/", payload, format="json")
        self.assertEqual(first.json()["line"], "cached line")
        self.assertEqual(second.json()["line"], "cached line")
        # Same star + same ship cell → served from cache, model called once.
        mock_req.assert_called_once()

    @override_settings(COPILOT_LLM_ENABLED=True)
    @patch("apps.lessons.copilot._copilot_request", return_value="x")
    def test_point_targets_unvisited_neighbour(self, _mock_req):
        # A close, unvisited neighbour should surface as the waypoint.
        self.star.position_x, self.star.position_y = 0.0, 0.0
        self.star.last_visited_at = timezone.now()
        self.star.save(update_fields=["position_x", "position_y", "last_visited_at"])
        neighbour = _make_star(self.tenant, "Go open me next.", position_x=0.05, position_y=0.0)

        resp = self.client.post(
            "/api/v1/lessons/galaxy/reflect/",
            {"star_id": self.star.id},
            format="json",
        )
        point = resp.json()["point"]
        self.assertIsNotNone(point)
        self.assertEqual(point["star_id"], neighbour.id)
        self.assertEqual(point["reason"], "nearby_unvisited")


class LoadEdgesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = _make_tenant("Edges Tenant", 200320)

    def test_load_edges_collects_both_directions(self):
        a = _make_star(self.tenant, "a")
        b = _make_star(self.tenant, "b")
        LessonConnection.objects.create(from_lesson=a, to_lesson=b, similarity=0.8, connection_type="similar")

        edges = copilot.load_edges_for(a.id, {a.id, b.id})
        self.assertIn(a.id, edges)
        self.assertEqual(edges[a.id][0][0], b.id)
