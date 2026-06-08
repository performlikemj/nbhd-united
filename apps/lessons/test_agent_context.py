"""Tests for assistant-facing constellation context (``agent_context``)."""

from __future__ import annotations

from django.test import TestCase

from apps.lessons.agent_context import (
    build_constellation_context,
    build_star_context,
    constellation_notes_payload,
    recent_active_stars,
    render_constellation_envelope,
)
from apps.lessons.models import Lesson, StarJournalEntry, TutoringSession
from apps.tenants.services import create_tenant


class ConstellationAgentContextTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Galaxy", telegram_chat_id=909090)
        # Isolate from any starter rows so summary/count assertions are exact.
        Lesson.objects.filter(tenant=self.tenant).delete()

    def _star(self, text="Slow down to go fast", **kwargs) -> Lesson:
        defaults = dict(
            tenant=self.tenant,
            text=text,
            context="reflection",
            source_type="reflection",
            source_ref="",
            tags=["focus"],
            status="approved",
        )
        defaults.update(kwargs)
        return Lesson.objects.create(**defaults)

    def test_recent_active_stars_includes_noted_excludes_inactive(self):
        noted = self._star(text="Pinned wisdom", galaxy_note="remember this")
        self._star(text="Untouched lesson")  # no note / journal / tutoring → inactive
        active = recent_active_stars(self.tenant)
        self.assertEqual([s.id for s in active], [noted.id])

    def test_active_star_via_journal_entry(self):
        star = self._star(text="Reflected topic")
        StarJournalEntry.objects.create(
            tenant=self.tenant, star=star, text="I sat with this today", entry_type="revisit"
        )
        self.assertEqual([s.id for s in recent_active_stars(self.tenant)], [star.id])

    def test_active_star_via_tutoring_session(self):
        star = self._star(text="Tutored topic")
        TutoringSession.objects.create(star=star, phases_completed=["restate"])
        self.assertEqual([s.id for s in recent_active_stars(self.tenant)], [star.id])

    def test_build_star_context_surfaces_enrichment(self):
        star = self._star(galaxy_note="north star idea", star_stage="radiant")
        StarJournalEntry.objects.create(tenant=self.tenant, star=star, text="reflection body", entry_type="free")
        TutoringSession.objects.create(
            star=star,
            phases_completed=["restate", "deepen"],
            player_restated_accurately=True,
            player_found_edge_cases=False,
            connections_made=[{"to_star_id": None, "player_text": "links to discipline"}],
            mastery_achieved=False,
        )
        ctx = build_star_context(star)
        self.assertEqual(ctx["galaxy_note"], "north star idea")
        self.assertEqual(ctx["stage"], "radiant")
        self.assertEqual(len(ctx["journal_entries"]), 1)
        self.assertEqual(ctx["journal_entries"][0]["text"], "reflection body")
        self.assertEqual(len(ctx["tutoring_insights"]), 1)
        self.assertTrue(ctx["tutoring_insights"][0]["restated_accurately"])
        self.assertEqual(ctx["tutoring_insights"][0]["connections_made"], 1)

    def test_build_constellation_context_empty_when_quiet(self):
        self._star(text="inactive only")
        self.assertEqual(build_constellation_context(self.tenant), {})

    def test_build_constellation_context_has_active_and_summary(self):
        self._star(galaxy_note="note", star_stage="ignited")
        bundle = build_constellation_context(self.tenant)
        self.assertIn("active_stars", bundle)
        self.assertEqual(bundle["summary"]["total_stars"], 1)
        self.assertEqual(bundle["summary"]["by_stage"].get("ignited"), 1)

    def test_render_envelope_empty_then_populated(self):
        self.assertEqual(render_constellation_envelope(self.tenant), "")
        self._star(text="Keep promises to yourself", galaxy_note="non-negotiable")
        md = render_constellation_envelope(self.tenant)
        self.assertIn("Keep promises to yourself", md)
        self.assertIn("pinned note: non-negotiable", md)

    def test_notes_payload_star_mode(self):
        star = self._star(galaxy_note="deep note")
        payload = constellation_notes_payload(self.tenant, star_id=star.id)
        self.assertEqual(payload["mode"], "star")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["stars"][0]["galaxy_note"], "deep note")

    def test_notes_payload_star_mode_missing_returns_empty(self):
        payload = constellation_notes_payload(self.tenant, star_id=999999)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["stars"], [])

    def test_tenant_isolation(self):
        other = create_tenant(display_name="Other", telegram_chat_id=909091)
        Lesson.objects.filter(tenant=other).delete()
        self._star(galaxy_note="mine")
        self.assertEqual(recent_active_stars(other), [])
        self.assertEqual(build_constellation_context(other), {})
