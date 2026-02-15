"""Tests for journal serializers, models, and API."""
from __future__ import annotations

from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant
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


class JournalApiTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Journal API User", telegram_chat_id=900001)
        self.other_tenant = create_tenant(display_name="Other Journal User", telegram_chat_id=900002)
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _create_entry(self, *, tenant: Tenant | None = None, **overrides) -> JournalEntry:
        target = tenant or self.tenant
        defaults = {
            "date": date(2026, 2, 15),
            "mood": "focused",
            "energy": "medium",
            "wins": ["Shipped feature"],
            "challenges": ["Long meeting"],
            "reflection": "Protect maker time.",
            "raw_text": "summary",
        }
        defaults.update(overrides)
        return JournalEntry.objects.create(tenant=target, **defaults)

    def test_list_returns_only_own_entries(self):
        own = self._create_entry()
        self._create_entry(tenant=self.other_tenant)

        response = self.client.get("/api/v1/journal/")
        self.assertEqual(response.status_code, 200)
        returned_ids = {item["id"] for item in response.json()}
        self.assertEqual(returned_ids, {str(own.id)})

    def test_create_sets_tenant_from_auth(self):
        response = self.client.post(
            "/api/v1/journal/",
            data={
                "date": "2026-02-15",
                "mood": "calm",
                "energy": "high",
                "wins": ["Launched"],
                "challenges": [],
                "reflection": "Good day.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        entry = JournalEntry.objects.get(id=response.json()["id"])
        self.assertEqual(entry.tenant, self.tenant)

    def test_raw_text_auto_generated(self):
        response = self.client.post(
            "/api/v1/journal/",
            data={
                "date": "2026-02-15",
                "mood": "happy",
                "energy": "high",
                "wins": ["Win A"],
                "challenges": ["Challenge B"],
                "reflection": "Reflecting.",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        entry = JournalEntry.objects.get(id=response.json()["id"])
        self.assertIn("Mood: happy", entry.raw_text)
        self.assertIn("Win A", entry.raw_text)

    def test_detail_returns_404_for_other_tenant(self):
        other_entry = self._create_entry(tenant=self.other_tenant)
        response = self.client.get(f"/api/v1/journal/{other_entry.id}/")
        self.assertEqual(response.status_code, 404)

    def test_patch_updates_entry(self):
        entry = self._create_entry()
        response = self.client.patch(
            f"/api/v1/journal/{entry.id}/",
            data={"mood": "energized"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.mood, "energized")
        self.assertIn("Mood: energized", entry.raw_text)

    def test_delete_removes_entry(self):
        entry = self._create_entry()
        response = self.client.delete(f"/api/v1/journal/{entry.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(JournalEntry.objects.filter(id=entry.id).exists())

    def test_date_filtering(self):
        self._create_entry(date=date(2026, 2, 10))
        target = self._create_entry(date=date(2026, 2, 15))
        self._create_entry(date=date(2026, 2, 20))

        response = self.client.get("/api/v1/journal/?date_from=2026-02-14&date_to=2026-02-16")
        self.assertEqual(response.status_code, 200)
        returned_ids = {item["id"] for item in response.json()}
        self.assertEqual(returned_ids, {str(target.id)})

    def test_validation_rejects_empty_mood(self):
        response = self.client.post(
            "/api/v1/journal/",
            data={
                "date": "2026-02-15",
                "mood": "  ",
                "energy": "low",
                "wins": [],
                "challenges": [],
                "reflection": "",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
