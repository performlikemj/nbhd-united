"""The actual nightly tool endpoint keeps markdown and also stores daily mood."""

from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.pii.testsupport import neural_ran
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

from .models import DailyNote, Document, JournalEntry
from .services import set_daily_note_section


@override_settings(NBHD_INTERNAL_API_KEY="test-key", NBHD_DISABLE_BACKGROUND_THREADS=True)
class DailyNoteMoodBridgeTests(TestCase):
    day = date(2026, 9, 18)
    markdown = (
        "# 2026-09-18\n\n## Energy & Mood\nold mood\n\n"
        "### 09:05 — Owner\nKeep this journal entry.\n\n## Later\nKeep this section.\n"
    )

    def setUp(self):
        self.tenant = Tenant.objects.create(
            user=User.objects.create_user(username="mood-bridge"), status=Tenant.Status.ACTIVE
        )
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.pk),
        }
        self.url = f"/api/v1/integrations/runtime/{self.tenant.pk}/daily-note/append/"
        self.doc = Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.DAILY,
            slug=str(self.day),
            title=str(self.day),
            markdown=self.markdown,
        )

    def write_section(self, content, section="energy-mood"):
        response = self.client.post(
            self.url,
            {"date": str(self.day), "section_slug": section, "content": content},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.doc.refresh_from_db()
        self.assertEqual(response.data["markdown"], self.doc.markdown)
        return response

    def test_parseable_section_creates_then_updates_same_days_entry(self):
        for content, score, energy, mood in (
            ("Energy: 7 | Mood: steady", 7, "medium", "steady"),
            ("Mood: bright | Energy: 10/10", 10, "high", "bright"),
            ("Energy: 1\nMood: tired", 1, "low", "tired"),
        ):
            with self.subTest(content=content):
                self.write_section(content)
                self.assertEqual(self.doc.markdown, self.markdown.replace("old mood", content))
                entry = JournalEntry.objects.get(tenant=self.tenant)
                self.assertEqual(entry.date, self.day)
                self.assertEqual(entry.energy_score, score)
                self.assertEqual(entry.energy, energy)
                self.assertEqual(entry.mood, mood)

    def test_non_numeric_and_invalid_energy_only_write_unchanged_markdown(self):
        for content in (
            "Energy: low | Mood: steady",
            "Mood: steady",
            "Energy: 0 | Mood: steady",
            "Energy: 11 | Mood: steady",
            "Energy: 7.5 | Mood: steady",
            "Energy: -1 | Mood: steady",
            "Energy: 7/100 | Mood: steady",
        ):
            with self.subTest(content=content):
                self.write_section(content)
                self.assertEqual(self.doc.markdown, self.markdown.replace("old mood", content))
                self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant).exists())

    def test_unparseable_update_preserves_existing_structured_mood(self):
        self.write_section("Energy: 7 | Mood: steady")
        self.write_section("Energy: unknown | Mood: unsure")
        entry = JournalEntry.objects.get(tenant=self.tenant)
        self.assertEqual((entry.energy_score, entry.mood), (7, "steady"))

    def test_other_sections_do_not_write_mood(self):
        self.write_section("Energy: 7 | Mood: steady", section="evening-check-in")
        self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant).exists())

    def test_existing_journal_and_omitted_feeling_are_preserved(self):
        entry = JournalEntry.objects.create(
            tenant=self.tenant,
            date=self.day,
            energy_score=3,
            mood="calm",
            raw_text="Original reflection",
            wins=["Finished a book"],
            pii_receipts={"mood": {"state": "clean"}, "raw_text": {"state": "clean"}},
        )
        self.write_section("Energy: 8")
        entry.refresh_from_db()
        self.assertEqual(entry.energy_score, 8)
        self.assertEqual(entry.energy, "high")
        self.assertEqual(entry.mood, "calm")
        self.assertEqual(entry.raw_text, "Original reflection")
        self.assertEqual(entry.wins, ["Finished a book"])
        self.assertEqual(entry.pii_receipts, {"mood": {"state": "clean"}, "raw_text": {"state": "clean"}})

    def test_bridge_and_owner_endpoint_share_the_daily_upsert(self):
        self.write_section("Energy: 7 | Mood: steady")
        entry = JournalEntry.objects.get(tenant=self.tenant)
        self.client.force_authenticate(user=self.tenant.user)
        response = self.client.post(
            "/api/v1/journal/mood/", {"date": str(self.day), "energy_score": 8, "feeling": "happy"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(entry.pk), response.data["id"])
        self.client.force_authenticate(user=None)
        self.write_section("Energy: 4 | Mood: quiet")
        entry.refresh_from_db()
        self.assertEqual((entry.energy_score, entry.mood), (4, "quiet"))
        self.assertEqual(JournalEntry.objects.filter(tenant=self.tenant).count(), 1)

    def test_runtime_feeling_uses_placeholder_store_and_receipts(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        with (
            patch("apps.pii.redactor._detect_pii", side_effect=neural_ran([])),
            patch("apps.pii.authoring._detect_pii", side_effect=neural_ran([])),
        ):
            self.write_section("Energy: 7 | Mood: Grateful for Alice")
        entry = JournalEntry.objects.get(tenant=self.tenant)
        self.assertIn("[PERSON_1]", entry.mood)
        for field in ("mood", "raw_text"):
            self.assertNotIn("Alice", getattr(entry, field))
            # Runtime authoring remasks known names immediately and records
            # deferred detection honestly, just like the existing note write.
            self.assertEqual(entry.pii_receipts[field]["state"], "unconfirmed")
            self.assertEqual(entry.pii_receipts[field]["writer"], "runtime")
        self.assertNotIn("Alice", self.doc.markdown)

    def test_bridge_is_tenant_scoped(self):
        other = Tenant.objects.create(user=User.objects.create_user(username="other-mood-bridge"))
        private = JournalEntry.objects.create(tenant=other, date=self.day, energy_score=2, mood="private")
        self.write_section("Energy: 7 | Mood: steady")
        private.refresh_from_db()
        self.assertEqual((private.energy_score, private.mood), (2, "private"))
        self.assertEqual(JournalEntry.objects.get(tenant=self.tenant).energy_score, 7)

    def test_bridge_failure_does_not_break_markdown_write(self):
        content = "Energy: 7 | Mood: steady"
        with (
            patch("apps.journal.mood.upsert_mood", side_effect=RuntimeError("store unavailable")),
            self.assertLogs("apps.journal.mood", level="WARNING"),
        ):
            self.write_section(content)
        self.assertEqual(self.doc.markdown, self.markdown.replace("old mood", content))
        self.assertFalse(JournalEntry.objects.filter(tenant=self.tenant).exists())

    def test_legacy_section_service_also_bridges_without_changing_markdown(self):
        note = DailyNote.objects.create(
            tenant=self.tenant, date=self.day, markdown="# 2026-09-18\n\n## Energy Mood\nold\n"
        )
        for content in ("Energy: low | Mood: tired", "Energy: 7 | Mood: steady"):
            note, _ = set_daily_note_section(note=note, section_slug="energy-mood", content=content, writer="owner")
            note.refresh_from_db()
            self.assertEqual(note.markdown, f"# 2026-09-18 (Default)\n\n## Energy Mood\n{content}\n")
            self.assertEqual(JournalEntry.objects.filter(tenant=self.tenant).count(), int("7" in content))
        self.assertEqual(JournalEntry.objects.get(tenant=self.tenant).energy_score, 7)
