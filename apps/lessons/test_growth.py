"""Tests for star growth — notes/connections promote a star's stage (monotonic)."""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant

from . import growth
from .models import Lesson, LessonConnection, StarJournalEntry


def _star(tenant: Tenant, text: str = "A lesson", **overrides) -> Lesson:
    defaults = {
        "text": text,
        "context": "from testing",
        "source_type": "experience",
        "source_ref": "test",
        "tags": ["test"],
        "status": "approved",
        "star_stage": "proto",
    }
    defaults.update(overrides)
    return Lesson.objects.create(tenant=tenant, **defaults)


class ComputeStageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant = create_tenant(display_name="Growth Tenant", telegram_chat_id=300400)

    def test_untouched_star_is_proto(self):
        star = _star(self.tenant)
        self.assertEqual(growth.compute_star_stage(star), "proto")

    def test_one_note_ignites(self):
        star = _star(self.tenant)
        StarJournalEntry.objects.create(tenant=self.tenant, star=star, text="n1")
        self.assertEqual(growth.compute_star_stage(star), "ignited")

    def test_three_notes_radiant(self):
        star = _star(self.tenant)
        for i in range(3):
            StarJournalEntry.objects.create(tenant=self.tenant, star=star, text=f"n{i}")
        self.assertEqual(growth.compute_star_stage(star), "radiant")

    def test_eight_notes_supernova(self):
        star = _star(self.tenant)
        for i in range(8):
            StarJournalEntry.objects.create(tenant=self.tenant, star=star, text=f"n{i}")
        self.assertEqual(growth.compute_star_stage(star), "supernova")

    def test_one_connection_ignites(self):
        star = _star(self.tenant)
        other = _star(self.tenant, text="other")
        LessonConnection.objects.create(
            from_lesson=star, to_lesson=other, similarity=1.0, connection_type="user_linked"
        )
        self.assertEqual(growth.compute_star_stage(star), "ignited")

    def test_apply_growth_is_monotonic(self):
        # A star already radiant (e.g. from tutoring) is NOT demoted by a low-signal
        # recompute.
        star = _star(self.tenant, star_stage="radiant")
        StarJournalEntry.objects.create(tenant=self.tenant, star=star, text="just one note")
        self.assertEqual(growth.apply_star_growth(star), "radiant")
        star.refresh_from_db()
        self.assertEqual(star.star_stage, "radiant")

    def test_apply_growth_promotes_and_saves(self):
        star = _star(self.tenant)
        for i in range(3):
            StarJournalEntry.objects.create(tenant=self.tenant, star=star, text=f"n{i}")
        self.assertEqual(growth.apply_star_growth(star), "radiant")
        star.refresh_from_db()
        self.assertEqual(star.star_stage, "radiant")


class GrowthEndpointTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Growth Ep Tenant", telegram_chat_id=300401)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        self.star = _star(self.tenant)

    def test_first_note_grows_star_and_reports_stage(self):
        resp = self.client.post(
            f"/api/v1/lessons/{self.star.id}/journal/create/",
            {"text": "my reflection"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["star_stage"], "ignited")
        self.star.refresh_from_db()
        self.assertEqual(self.star.star_stage, "ignited")

    def test_three_notes_reach_radiant(self):
        for i in range(3):
            resp = self.client.post(
                f"/api/v1/lessons/{self.star.id}/journal/create/",
                {"text": f"reflection {i}"},
                format="json",
            )
        self.assertEqual(resp.json()["star_stage"], "radiant")

    def test_connect_grows_both_stars(self):
        other = _star(self.tenant, text="other star")
        resp = self.client.post(
            f"/api/v1/lessons/{self.star.id}/connect/",
            {"target_star_id": other.id, "connection_type": "user_linked"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["source_star_stage"], "ignited")
        self.assertEqual(body["target_star_stage"], "ignited")
        self.star.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.star.star_stage, "ignited")
        self.assertEqual(other.star_stage, "ignited")

    def test_bare_land_does_not_grow(self):
        # A flyby (land) sets last_visited_at but must NOT grow the star — growth
        # is earned by doing something.
        resp = self.client.post(f"/api/v1/lessons/{self.star.id}/land/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["star_stage"], "proto")
