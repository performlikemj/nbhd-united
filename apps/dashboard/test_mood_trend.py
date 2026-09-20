"""Mood trend score compatibility, tenant scoping, and check-in refresh."""

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.journal.models import JournalEntry
from apps.tenants.models import Tenant, User


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    NBHD_DISABLE_BACKGROUND_THREADS=True,
)
class MoodTrendTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(user=User.objects.create_user(username="mood-trend-owner"))
        self.other = Tenant.objects.create(user=User.objects.create_user(username="mood-trend-other"))
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        cache.clear()
        self.addCleanup(cache.clear)

    def test_trend_includes_scores_and_legacy_null_only_for_requesting_tenant(self):
        today = timezone.localdate()
        JournalEntry.objects.create(tenant=self.tenant, date=today, mood="calm", energy_score=8, raw_text="Check-in")
        JournalEntry.objects.create(
            tenant=self.tenant, date=today - timedelta(days=1), mood="steady", energy="medium", raw_text="Legacy"
        )
        JournalEntry.objects.create(tenant=self.other, date=today, mood="private", energy_score=2, raw_text="Private")
        response = self.client.get("/api/v1/dashboard/horizons/")
        self.assertEqual(response.status_code, 200)
        trend = response.data["mood_trend"]
        self.assertEqual(len(trend), 2)
        self.assertEqual([(row["energy_score"], row["energy"]) for row in trend], [(None, "medium"), (8, "high")])
        self.assertEqual([row["mood"] for row in trend], ["steady", "calm"])
        self.client.force_authenticate(user=self.other.user)
        other_trend = self.client.get("/api/v1/dashboard/horizons/").data["mood_trend"]
        self.assertEqual(len(other_trend), 1)
        self.assertEqual(other_trend[0]["energy_score"], 2)

    def test_check_in_invalidates_cached_trend(self):
        self.assertEqual(self.client.get("/api/v1/dashboard/horizons/").data["mood_trend"], [])
        for score in (3, 9):
            response = self.client.post("/api/v1/journal/mood/", {"energy_score": score}, format="json")
            self.assertIn(response.status_code, (200, 201))
            trend = self.client.get("/api/v1/dashboard/horizons/").data["mood_trend"]
            self.assertEqual(len(trend), 1)
            self.assertEqual(trend[0]["energy_score"], score)
