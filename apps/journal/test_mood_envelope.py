"""Bounded, tenant-scoped mood context and registry refresh coverage."""

from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.orchestrator.envelope_registry import all_sections
from apps.orchestrator.workspace_envelope import TRIGGER_REGISTRY_SIGNAL, render_managed_region
from apps.tenants.models import Tenant, User

from .envelope import render_mood
from .models import JournalEntry


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class MoodEnvelopeTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(user=User.objects.create_user(username="mood-context"))

    def entry(self, **fields):
        return JournalEntry.objects.create(
            tenant=self.tenant, date=fields.pop("date", date(2026, 9, 20)), raw_text="Check-in", **fields
        )

    def test_latest_scored_entry_and_tenant_rehydration(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        self.entry(date=date(2026, 9, 19), energy_score=2, mood="tired")
        self.entry(energy_score=7, mood="Grateful for [PERSON_1]")
        self.entry(date=date(2026, 9, 21), mood="unscored")
        other = Tenant.objects.create(user=User.objects.create_user(username="other-mood-context"))
        JournalEntry.objects.create(tenant=other, date=date(2026, 9, 22), energy_score=1, mood="private")
        self.assertEqual(render_mood(self.tenant), 'Latest: 7/10 "Grateful for Alice" — 2026-09-20.')
        self.assertNotIn("private", render_mood(self.tenant))

    def test_no_mood_renders_empty_even_when_enabled(self):
        self.tenant.mood_context_enabled = True
        self.assertEqual(render_mood(self.tenant), "")
        self.entry(mood="", energy="")
        self.assertEqual(render_mood(self.tenant), "")
        self.assertNotIn("## Mood", render_managed_region(self.tenant))

    def test_legacy_energy_without_numeric_score(self):
        self.entry(energy="low", mood="tired")
        self.assertEqual(render_mood(self.tenant), 'Latest: low "tired" — 2026-09-20.')

    def test_default_off_flag_controls_assembled_section(self):
        section = next(s for s in all_sections() if s.key == "mood")
        self.assertEqual(section.order, 15)
        self.assertEqual(section.refresh_on, (JournalEntry,))
        self.entry(energy_score=7, mood="steady")
        self.assertFalse(self.tenant.mood_context_enabled)
        self.assertFalse(section.enabled(self.tenant))
        self.assertNotIn("## Mood", render_managed_region(self.tenant))
        self.tenant.mood_context_enabled = True
        self.tenant.save(update_fields=["mood_context_enabled"])
        self.tenant.refresh_from_db()
        self.assertTrue(section.enabled(self.tenant))
        self.assertIn('## Mood\nLatest: 7/10 "steady" — 2026-09-20.', render_managed_region(self.tenant))

    def test_two_lines_and_small_byte_budget_after_rehydration(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "🙂\n" * 300}}
        self.entry(energy_score=10, mood="[PERSON_1]")
        rendered = "## Mood\n" + render_mood(self.tenant) + "\n"
        self.assertEqual(len(rendered.splitlines()), 2)
        self.assertLessEqual(len(rendered.encode("utf-8")), 220)
        self.assertIn("…", rendered)

    def test_entry_save_schedules_refresh_after_commit(self):
        self.tenant.mood_context_enabled = True
        with patch("apps.orchestrator.workspace_envelope.push_user_md") as push:
            with self.captureOnCommitCallbacks(execute=True):
                entry = self.entry(energy_score=7, mood="steady")
                push.assert_not_called()
            push.assert_called_once_with(
                str(self.tenant.pk), debounce_seconds=0, trigger=TRIGGER_REGISTRY_SIGNAL, sender_model="JournalEntry"
            )
            push.reset_mock()
            with self.captureOnCommitCallbacks(execute=True):
                entry.energy_score = 8
                entry.save(update_fields=["energy_score"])
            push.assert_called_once()
