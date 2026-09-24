"""DB coverage for the owner mood check-in contract."""

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.pii.testsupport import neural_ran
from apps.tenants.models import Tenant, User

from .models import JournalEntry


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    NBHD_DISABLE_BACKGROUND_THREADS=True,
)
class MoodCheckInTests(TestCase):
    url = "/api/v1/journal/mood/"

    def setUp(self):
        self.user = User.objects.create_user(username="mood-owner", password="password")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        cache.clear()
        self.addCleanup(cache.clear)

    def test_create_then_update_today_without_duplicate(self):
        first = self.client.post(self.url, {"energy_score": 1, "feeling": "tired"}, format="json")
        second = self.client.post(self.url, {"energy_score": 10, "feeling": "happy"}, format="json")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(second.data["energy_score"], 10)
        self.assertEqual(second.data["energy"], "high")
        entry = JournalEntry.objects.get(tenant=self.tenant, date=timezone.localdate())
        self.assertEqual(entry.energy_score, 10)
        self.assertEqual(entry.mood, "happy")
        self.assertEqual(JournalEntry.objects.filter(tenant=self.tenant).count(), 1)

    def test_score_only_create_synthesizes_text(self):
        response = self.client.post(self.url, {"energy_score": 6}, format="json")
        self.assertEqual(response.status_code, 201)
        entry = JournalEntry.objects.get(tenant=self.tenant)
        self.assertEqual(entry.mood, "")
        self.assertEqual(entry.raw_text, "Energy 6/10")
        self.assertIn("raw_text", entry.pii_receipts)

    def test_existing_entry_preserves_omitted_mood_text_and_receipts(self):
        entry = JournalEntry.objects.create(
            tenant=self.tenant,
            date=timezone.localdate(),
            mood="calm",
            energy="low",
            raw_text="An existing journal reflection",
            wins=["Finished a book"],
            pii_receipts={"mood": {"state": "clean"}, "raw_text": {"state": "clean"}},
        )
        receipts = entry.pii_receipts.copy()
        response = self.client.post(self.url, {"energy_score": 8}, format="json")
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.energy_score, 8)
        self.assertEqual(entry.energy, "high")
        self.assertEqual(entry.mood, "calm")
        self.assertEqual(entry.raw_text, "An existing journal reflection")
        self.assertEqual(entry.wins, ["Finished a book"])
        self.assertEqual(entry.pii_receipts, receipts)

    def test_explicit_date_upserts_that_day(self):
        day = timezone.localdate() - timedelta(days=2)
        for score, expected_status in ((4, 201), (7, 200)):
            response = self.client.post(self.url, {"energy_score": score, "date": day.isoformat()}, format="json")
            self.assertEqual(response.status_code, expected_status)
        entry = JournalEntry.objects.get(tenant=self.tenant)
        self.assertEqual(entry.date, day)
        self.assertEqual(entry.energy_score, 7)

    def test_invalid_scores_and_missing_score_do_not_write(self):
        for payload in ({}, *({"energy_score": score} for score in (0, 11, None, 1.5, True, "bad"))):
            with self.subTest(payload=payload):
                response = self.client.post(self.url, payload, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn("energy_score", response.data)
        self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant).exists())

    def test_invalid_date_and_feeling_do_not_write(self):
        for fields in ({"date": "not-a-date"}, {"feeling": "x" * 256}, {"feeling": None}, {"feeling": []}):
            with self.subTest(fields=fields):
                response = self.client.post(self.url, {"energy_score": 5, **fields}, format="json")
                self.assertEqual(response.status_code, 400)
        self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant).exists())

    def test_model_derives_all_enum_boundaries_and_partial_save(self):
        entry = JournalEntry(tenant=self.tenant, date=timezone.localdate(), mood="calm", raw_text="Check-in")
        for score, energy in ((1, "low"), (3, "low"), (4, "medium"), (7, "medium"), (8, "high"), (10, "high")):
            with self.subTest(score=score):
                entry.energy_score = score
                entry.energy = "incorrect"
                entry.save()
                entry.refresh_from_db()
                self.assertEqual(entry.energy, energy)
        entry.energy_score = 2
        entry.save(update_fields=["energy_score"])
        entry.refresh_from_db()
        self.assertEqual(entry.energy, "low")
        entry.energy_score = None
        entry.energy = "medium"
        entry.save()
        entry.refresh_from_db()
        self.assertIsNone(entry.energy_score)
        self.assertEqual(entry.energy, "medium")

    def test_model_score_validators(self):
        field = JournalEntry._meta.get_field("energy_score")
        for score in (0, 11):
            with self.subTest(score=score), self.assertRaises(ValidationError):
                field.clean(score, None)
        for score in (1, 10, None):
            self.assertEqual(field.clean(score, None), score)

    def test_feeling_uses_journal_pii_chokepoint_on_create_and_update(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        with (
            patch("apps.pii.redactor._detect_pii", side_effect=neural_ran([])),
            patch("apps.pii.authoring._detect_pii", side_effect=neural_ran([])),
        ):
            for feeling, expected_status in (("Inspired by Alice", 201), ("Grateful for Alice", 200)):
                response = self.client.post(self.url, {"energy_score": 7, "feeling": feeling}, format="json")
                self.assertEqual(response.status_code, expected_status)
                entry = JournalEntry.objects.get(tenant=self.tenant)
                self.assertNotIn("Alice", entry.mood)
                self.assertNotIn("Alice", entry.raw_text)
                self.assertIn("[PERSON_1]", entry.mood)
                self.assertIn("[PERSON_1]", entry.raw_text)
                for field in ("mood", "raw_text"):
                    self.assertEqual(entry.pii_receipts[field]["state"], "placeholder")
                    self.assertEqual(entry.pii_receipts[field]["writer"], "owner")
                self.assertEqual(response.data["mood"], feeling)
                self.assertEqual(
                    response.data["pii_receipts"]["mood"]["redactions"],
                    [{"placeholder": "[PERSON_1]", "value": "Alice"}],
                )
            receipts = entry.pii_receipts.copy()
            response = self.client.post(self.url, {"energy_score": 9}, format="json")
        self.assertEqual(response.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.pii_receipts, receipts)
        self.assertEqual(entry.mood, "Grateful for [PERSON_1]")

    def test_post_never_touches_another_tenants_entry(self):
        other = Tenant.objects.create(user=User.objects.create_user(username="other-mood-owner"))
        private = JournalEntry.objects.create(
            tenant=other, date=timezone.localdate(), mood="private", energy_score=2, raw_text="Private journal"
        )
        response = self.client.post(
            self.url, {"energy_score": 9, "tenant": str(other.pk), "id": str(private.pk)}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        response = self.client.post(self.url, {"energy_score": 5}, format="json")
        self.assertEqual(response.status_code, 200)
        private.refresh_from_db()
        self.assertEqual(private.energy_score, 2)
        self.assertEqual(private.mood, "private")
        self.assertEqual(private.raw_text, "Private journal")
        self.assertEqual(JournalEntry.objects.get(tenant=self.tenant).energy_score, 5)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(self.url, {"energy_score": 5}, format="json")
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(JournalEntry.objects.exists())

    def test_session_authentication(self):
        self.client.force_authenticate(user=None)
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"energy_score": 5}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(JournalEntry.objects.get().tenant_id, self.tenant.pk)
