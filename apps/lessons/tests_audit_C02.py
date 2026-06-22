from __future__ import annotations

"""Regression tests for fix cluster C02.

Covers:
  * FA-0792 — ``create_connections`` rebuilds the lesson's auto-similarity edges
    from scratch (stale 'similar' edges removed; user-curated edges preserved).
  * FA-0657 — ``validate_kind_slug`` rejects non-date slugs for kind='daily' on
    every write path, not just the GET branch.
"""

from unittest import skipUnless
from unittest.mock import patch

from django.db import connection
from django.test import TestCase

from apps.journal.path_validation import validate_kind_slug
from apps.tenants.services import create_tenant

from .models import Lesson, LessonConnection


def _vector_with_dims(first: float = 0.0, second: float = 0.0, third: float = 0.0) -> list[float]:
    vector = [0.0] * 1536
    vector[0] = first
    vector[1] = second
    vector[2] = third
    return vector


@skipUnless(connection.vendor == "postgresql", "pgvector query annotations require PostgreSQL in tests")
class CreateConnectionsRebuildTests(TestCase):
    """FA-0792: stale similarity edges must not survive an embedding rewrite."""

    def setUp(self):
        self.tenant = create_tenant(display_name="C02 Lessons Tenant", telegram_chat_id=987654)
        self.source = Lesson.objects.create(
            tenant=self.tenant,
            text="Source lesson",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(1.0, 0.0, 0.0),
            tags=["t1"],
            source_ref="ref-a",
        )
        self.old_peer = Lesson.objects.create(
            tenant=self.tenant,
            text="Old peer",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(0.0, 1.0, 0.0),
            tags=["t2"],
            source_ref="ref-b",
        )
        self.new_peer = Lesson.objects.create(
            tenant=self.tenant,
            text="New peer",
            context="source",
            source_type="journal",
            status="approved",
            embedding=_vector_with_dims(0.0, 0.0, 1.0),
            tags=["t3"],
            source_ref="ref-c",
        )

    def test_rewrite_drops_stale_similar_edges(self):
        from .services import create_connections

        # First pass: source is similar to old_peer.
        with patch("apps.lessons.services.find_similar_lessons", return_value=[(self.old_peer, 0.91)]):
            create_connections(self.source)
        self.assertTrue(LessonConnection.objects.filter(from_lesson=self.source, to_lesson=self.old_peer).exists())

        # Rewrite: source is now similar to new_peer only. The stale old_peer
        # edges (both directions) must be gone, not merely supplemented.
        with patch("apps.lessons.services.find_similar_lessons", return_value=[(self.new_peer, 0.88)]):
            create_connections(self.source)

        self.assertFalse(
            LessonConnection.objects.filter(from_lesson=self.source, to_lesson=self.old_peer).exists(),
            "stale forward edge survived rewrite",
        )
        self.assertFalse(
            LessonConnection.objects.filter(from_lesson=self.old_peer, to_lesson=self.source).exists(),
            "stale reverse edge survived rewrite",
        )
        self.assertTrue(LessonConnection.objects.filter(from_lesson=self.source, to_lesson=self.new_peer).exists())
        self.assertTrue(LessonConnection.objects.filter(from_lesson=self.new_peer, to_lesson=self.source).exists())

    def test_rewrite_preserves_user_curated_edges(self):
        from .services import create_connections

        # A user-curated edge between source and old_peer must outlive a rewrite
        # that no longer considers them similar.
        LessonConnection.objects.create(
            from_lesson=self.source,
            to_lesson=self.old_peer,
            similarity=0.5,
            connection_type="user_linked",
        )

        with patch("apps.lessons.services.find_similar_lessons", return_value=[(self.new_peer, 0.88)]):
            create_connections(self.source)

        edge = LessonConnection.objects.get(from_lesson=self.source, to_lesson=self.old_peer)
        self.assertEqual(edge.connection_type, "user_linked")


class ValidateDailySlugTests(TestCase):
    """FA-0657: kind='daily' requires a YYYY-MM-DD slug on all write paths."""

    def test_daily_requires_iso_date_slug(self):
        err = validate_kind_slug("daily", "my-notes")
        self.assertIsNotNone(err)
        self.assertEqual(err[0], "invalid_slug")

    def test_daily_accepts_iso_date_slug(self):
        self.assertIsNone(validate_kind_slug("daily", "2026-06-15"))

    def test_non_daily_kind_unaffected(self):
        # A non-daily kind with a non-date slug remains valid.
        self.assertIsNone(validate_kind_slug("memory", "preferences"))
