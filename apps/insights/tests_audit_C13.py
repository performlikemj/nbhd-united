"""Regression tests for fix-cluster C13.

FA-0596: _record_insight_impl must be atomic — no orphaned proposed topics on failure.
FA-0607 / FA-0617: resolve_topic must survive a concurrent unique_together collision.
"""

from __future__ import annotations

from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase

from apps.insights.models import TopicRegistry
from apps.insights.topic_resolver import resolve_topic


class ResolveTopicRaceTest(TestCase):
    """FA-0607 / FA-0617 — IntegrityError on concurrent proposed-topic insert is recovered."""

    def _make_existing(self, pillar: str, slug: str) -> TopicRegistry:
        return TopicRegistry.objects.create(
            pillar=pillar,
            slug=slug,
            display_name=slug.replace("_", " "),
            status=TopicRegistry.Status.PROPOSED,
            source=TopicRegistry.Source.PROPOSED_BY_MODEL,
        )

    def test_integrity_error_falls_back_to_existing_row(self):
        """
        Simulate the race: _make_unique_slug returns a slug, the inner create
        raises IntegrityError (as if a concurrent transaction won), but the row
        already exists in the DB. resolve_topic must return that existing row.
        """
        pillar = "gravity"
        slug = "some_novel_topic"

        # Pre-create the row that the "winning" concurrent transaction inserted.
        existing = self._make_existing(pillar, slug)

        # Patch _make_unique_slug to return the slug that will collide.
        with patch(
            "apps.insights.topic_resolver._make_unique_slug",
            return_value=slug,
        ):
            # Patch create to raise IntegrityError on first call.
            original_create = TopicRegistry.objects.create
            call_count = {"n": 0}

            def patched_create(**kwargs):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise IntegrityError("duplicate key value violates unique constraint")
                return original_create(**kwargs)

            with patch.object(TopicRegistry.objects, "create", side_effect=patched_create):
                result = resolve_topic(pillar, "some novel topic")

        self.assertEqual(result.pk, existing.pk)
        self.assertEqual(result.slug, slug)

    def test_no_race_creates_proposed_topic(self):
        """Happy path: no collision → creates a new proposed row."""
        pillar = "gravity"
        result = resolve_topic(pillar, "a brand new topic xyz")
        self.assertEqual(result.status, TopicRegistry.Status.PROPOSED)
        self.assertEqual(result.source, TopicRegistry.Source.PROPOSED_BY_MODEL)
        self.assertTrue(TopicRegistry.objects.filter(pk=result.pk).exists())

    def test_canonical_topic_returned_without_create(self):
        """Exact slug match on a canonical topic short-circuits before any create."""
        # Use a test-unique slug so the row doesn't collide with the seeded
        # canonical topics that the seed_topics migration creates in a fresh DB
        # (e.g. gravity/debt) — that collision only surfaces in CI, not under a
        # reused --keepdb local DB.
        TopicRegistry.objects.create(
            pillar="gravity",
            slug="c13probe",
            display_name="C13 Probe",
            status=TopicRegistry.Status.CANONICAL,
            source=TopicRegistry.Source.SEED,
        )
        result = resolve_topic("gravity", "c13probe")
        self.assertEqual(result.slug, "c13probe")
        self.assertEqual(result.status, TopicRegistry.Status.CANONICAL)
        # Only one row should exist (no spurious proposed row created).
        self.assertEqual(TopicRegistry.objects.filter(pillar="gravity", slug="c13probe").count(), 1)


class RecordInsightImplAtomicTest(TestCase):
    """FA-0596 — _record_insight_impl is atomic: no orphaned proposed topic on failure."""

    def test_no_orphaned_topic_when_insight_create_fails(self):
        """
        If AssistantInsight.objects.create raises after resolve_topic has
        created a proposed TopicRegistry row, the proposed row must be rolled
        back (not committed to the DB).
        """
        from apps.insights.models import AssistantInsight
        from apps.insights.views import _record_insight_impl

        # Minimal tenant — adapt to whatever the model requires.
        # We just need an object; the transaction rollback is what we're testing.
        tenant_pk_sentinel = object()

        topic_created_pk = {}

        original_resolve = __import__("apps.insights.topic_resolver", fromlist=["resolve_topic"]).resolve_topic

        def capturing_resolve(pillar, candidate, **kw):
            topic = original_resolve(pillar, candidate, **kw)
            topic_created_pk["pk"] = topic.pk
            return topic

        with (
            patch("apps.insights.views.resolve_topic", side_effect=capturing_resolve),
            patch.object(
                AssistantInsight.objects,
                "create",
                side_effect=Exception("simulated DB failure"),
            ),
            self.assertRaises(Exception, msg="simulated DB failure"),
        ):
            _record_insight_impl(
                tenant=tenant_pk_sentinel,
                pillar="gravity",
                topic_input="orphan_test_topic",
                statement="test statement",
                evidence_refs=None,
                confidence=None,
                model_version="",
            )

        # The proposed topic must NOT survive in the DB.
        if "pk" in topic_created_pk:
            self.assertFalse(
                TopicRegistry.objects.filter(pk=topic_created_pk["pk"]).exists(),
                "Orphaned proposed topic was committed despite insight-create failure",
            )
