"""Tests for galaxy summary counts and the galaxy/insights read endpoint."""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from .models import Lesson, TutoringSession


class GalaxyInsightsTests(TestCase):
    """galaxy/summary stage counts + galaxy/insights tenant-scoped read surface."""

    def setUp(self):
        self.tenant = create_tenant(
            display_name="Insights Tenant",
            telegram_chat_id=100020,
        )
        self.other_tenant = create_tenant(
            display_name="Other Insights Tenant",
            telegram_chat_id=100021,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _create_star(self, tenant: Tenant, **overrides) -> Lesson:
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

    def _create_session(self, star: Lesson, **overrides) -> TutoringSession:
        defaults = {
            "phases_completed": ["restate", "deepen"],
            "mastery_achieved": False,
            "new_star_stage": "ignited",
            "player_restated_accurately": True,
            "player_found_edge_cases": False,
            "connections_made": [],
            "topic_shifted": "",
        }
        defaults.update(overrides)
        return TutoringSession.objects.create(star=star, **defaults)

    # ── Galaxy summary ─────────────────────────────────────────

    def test_galaxy_summary_counts_by_stage(self):
        self._create_star(self.tenant, star_stage="proto")
        self._create_star(self.tenant, text="Star 2", star_stage="ignited")
        self._create_star(self.tenant, text="Star 3", star_stage="radiant")
        self._create_star(self.tenant, text="Star 4", star_stage="supernova")
        # Extra proto to confirm the per-stage filter is real, not a tie.
        self._create_star(self.tenant, text="Star 5", star_stage="proto")

        resp = self.client.get("/api/v1/lessons/galaxy/summary/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(body["total_stars"], 5)
        self.assertEqual(body["proto_count"], 2)
        self.assertEqual(body["ignited_count"], 1)
        self.assertEqual(body["radiant_count"], 1)
        self.assertEqual(body["supernova_count"], 1)

    def test_galaxy_summary_tenant_isolation(self):
        self._create_star(self.tenant, star_stage="proto")
        self._create_star(self.other_tenant, text="Their star", star_stage="ignited")

        resp = self.client.get("/api/v1/lessons/galaxy/summary/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_stars"], 1)
        self.assertEqual(body["proto_count"], 1)
        self.assertEqual(body["ignited_count"], 0)

    # ── Galaxy insights ────────────────────────────────────────

    def test_insights_returns_caller_tenant_sessions(self):
        star = self._create_star(self.tenant, text="My learnable star")
        self._create_session(
            star,
            phases_completed=["restate", "deepen", "stress_test"],
            player_restated_accurately=True,
            player_found_edge_cases=True,
            connections_made=[{"to_star_id": 42, "player_text": "links to X"}],
            topic_shifted="ergonomics",
            mastery_achieved=True,
            new_star_stage="radiant",
        )

        resp = self.client.get("/api/v1/lessons/galaxy/insights/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(len(body), 1)
        item = body[0]
        self.assertEqual(item["star_id"], star.id)
        self.assertEqual(item["star_text"], "My learnable star")
        self.assertEqual(item["phases_completed"], ["restate", "deepen", "stress_test"])
        self.assertTrue(item["player_restated_accurately"])
        self.assertTrue(item["player_found_edge_cases"])
        self.assertEqual(item["connections_made"], [{"to_star_id": 42, "player_text": "links to X"}])
        self.assertEqual(item["topic_shifted"], "ergonomics")
        self.assertTrue(item["mastery_achieved"])
        self.assertEqual(item["new_star_stage"], "radiant")
        self.assertIn("created_at", item)

    def test_insights_orders_most_recent_first(self):
        star = self._create_star(self.tenant)
        first = self._create_session(star, topic_shifted="first")
        second = self._create_session(star, topic_shifted="second")

        resp = self.client.get("/api/v1/lessons/galaxy/insights/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 2)
        # Most recent (second created) should come first.
        self.assertEqual(body[0]["id"], str(second.id))
        self.assertEqual(body[1]["id"], str(first.id))

    def test_insights_respects_limit(self):
        star = self._create_star(self.tenant)
        for _ in range(5):
            self._create_session(star)

        resp = self.client.get("/api/v1/lessons/galaxy/insights/?limit=3")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 3)

    def test_insights_tenant_isolation(self):
        my_star = self._create_star(self.tenant, text="Mine")
        their_star = self._create_star(self.other_tenant, text="Theirs")
        self._create_session(my_star, topic_shifted="mine")
        self._create_session(their_star, topic_shifted="theirs")

        resp = self.client.get("/api/v1/lessons/galaxy/insights/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()

        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["star_id"], my_star.id)
        topic_shifts = [item["topic_shifted"] for item in body]
        self.assertNotIn("theirs", topic_shifts)

    def test_insights_requires_auth(self):
        unauth = APIClient()
        resp = unauth.get("/api/v1/lessons/galaxy/insights/")
        self.assertEqual(resp.status_code, 401)
