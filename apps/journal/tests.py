"""Tests for journal serializers and models."""
from __future__ import annotations

from datetime import date

from django.test import TestCase

from apps.tenants.services import create_tenant

from .models import JournalEntry, WeeklyReview
from .serializers import JournalEntryRuntimeSerializer, WeeklyReviewRuntimeSerializer


class JournalRuntimeSerializerTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Journal Tester", telegram_chat_id=808080)

    def test_creates_journal_entry_with_tenant_from_context(self):
        serializer = JournalEntryRuntimeSerializer(
            data={
                "date": "2026-02-12",
                "mood": "focused",
                "energy": "medium",
                "wins": ["Shipped feature"],
                "challenges": ["Long meeting"],
                "reflection": "Protect maker time tomorrow.",
                "raw_text": "Session summary",
            },
            context={"tenant": self.tenant},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        entry = serializer.save()

        self.assertEqual(entry.tenant, self.tenant)
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_rejects_wins_above_max_items(self):
        serializer = JournalEntryRuntimeSerializer(
            data={
                "date": "2026-02-12",
                "mood": "ok",
                "energy": "low",
                "wins": [f"item-{idx}" for idx in range(11)],
                "challenges": [],
                "raw_text": "summary",
            },
            context={"tenant": self.tenant},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("wins", serializer.errors)


class WeeklyReviewRuntimeSerializerTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Review Tester", telegram_chat_id=818181)

    def test_rejects_invalid_week_window(self):
        serializer = WeeklyReviewRuntimeSerializer(
            data={
                "week_start": "2026-02-12",
                "week_end": "2026-02-06",
                "mood_summary": "Uneven",
                "top_wins": ["A"],
                "top_challenges": ["B"],
                "lessons": [],
                "week_rating": "meh",
                "intentions_next_week": ["Plan better"],
                "raw_text": "summary",
            },
            context={"tenant": self.tenant},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("week_end", serializer.errors)

    def test_saves_weekly_review(self):
        serializer = WeeklyReviewRuntimeSerializer(
            data={
                "week_start": date(2026, 2, 6),
                "week_end": date(2026, 2, 12),
                "mood_summary": "Steady finish",
                "top_wins": ["Ship release"],
                "top_challenges": ["Scope creep"],
                "lessons": ["Set limits earlier"],
                "week_rating": "thumbs-up",
                "intentions_next_week": ["Protect deep work blocks"],
                "raw_text": "summary",
            },
            context={"tenant": self.tenant},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        review = serializer.save()

        self.assertEqual(review.tenant, self.tenant)
        self.assertEqual(WeeklyReview.objects.count(), 1)
