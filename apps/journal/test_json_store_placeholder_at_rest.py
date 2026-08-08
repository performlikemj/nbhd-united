"""P3 W3a writer-seam coverage for the first JSON-carrying stores."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.journal.extraction import _create_pending_extraction
from apps.journal.models import DailyNote, PendingExtraction, Purpose
from apps.journal.serializers import JournalEntrySerializer, WeeklyReviewRuntimeSerializer
from apps.tenants.models import Tenant, User


class _JsonStoreBase(TestCase):
    flag_on = True

    def setUp(self):
        self.user = User.objects.create_user(username=f"w3a-{id(self)}", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            layer1_placeholder_writes=self.flag_on,
            pii_entity_map={"[PERSON_1]": {"name": "Alice"}},
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _journal_entry(self):
        serializer = JournalEntrySerializer(
            data={
                "date": "2026-08-08",
                "mood": "Alice-inspired",
                "energy": "medium",
                "wins": ["Called Alice"],
                "challenges": ["No blocker"],
                "reflection": "Thank Alice",
            },
            context={"tenant": self.tenant},
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save(), serializer.data

    def _weekly_review(self):
        serializer = WeeklyReviewRuntimeSerializer(
            data={
                "week_start": "2026-08-03",
                "week_end": "2026-08-09",
                "mood_summary": "Alice helped",
                "top_wins": ["Shipped with Alice"],
                "top_challenges": ["No blocker"],
                "lessons": ["Ask Alice early"],
                "week_rating": "thumbs-up",
                "intentions_next_week": ["Call Alice"],
                "raw_text": "Alice helped all week",
            },
            context={"tenant": self.tenant},
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def _pending(self):
        return _create_pending_extraction(
            tenant=self.tenant,
            text="Ask Alice to review the launch",
            seam="test.pending.background",
            kind=PendingExtraction.Kind.TASK,
            confidence="medium",
            expires_at=timezone.now() + timedelta(days=1),
        )


class JsonStoreFlagOffTests(_JsonStoreBase):
    flag_on = False

    def test_owner_runtime_and_background_writers_are_byte_identical(self):
        entry, _data = self._journal_entry()
        review = self._weekly_review()
        pending = self._pending()

        self.assertEqual(entry.wins, ["Called Alice"])
        self.assertEqual(entry.reflection, "Thank Alice")
        self.assertEqual(entry.pii_receipts["wins"], {"state": "bypass", "writer": "owner"})
        self.assertEqual(review.top_wins, ["Shipped with Alice"])
        self.assertEqual(review.raw_text, "Alice helped all week")
        self.assertEqual(review.pii_receipts["top_wins"], {"state": "bypass", "writer": "runtime"})
        self.assertEqual(pending.text, "Ask Alice to review the launch")
        self.assertEqual(pending.pii_receipts["text"], {"state": "bypass", "writer": "background"})

    def test_daily_note_entry_and_section_preserve_pre_p3_bytes(self):
        created = self.client.post(
            "/api/v1/journal/daily/2026-08-08/entries/",
            {"content": "Called Alice", "time": "09:00"},
            format="json",
        )
        section = self.client.patch(
            "/api/v1/journal/daily/2026-08-08/sections/focus/",
            {"content": "Plan with Alice"},
            format="json",
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(section.status_code, 200)
        note = DailyNote.objects.get(tenant=self.tenant, date=date(2026, 8, 8))
        self.assertIn("Plan with Alice", note.markdown)
        self.assertNotIn("[PERSON_1]", note.markdown)
        self.assertEqual(note.pii_receipts["markdown"]["state"], "bypass")

    def test_purpose_owner_fields_preserve_pre_p3_bytes(self):
        response = self.client.post(
            "/api/v1/journal/purposes/",
            {
                "statement": "Build with Alice",
                "pillars": ["core"],
                "evidence": [{"kind": "journal", "ref": "Alice-ref", "note": "Alice encouraged me"}],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        purpose = Purpose.objects.get(tenant=self.tenant)
        self.assertEqual(purpose.statement, "Build with Alice")
        self.assertEqual(purpose.evidence[0]["note"], "Alice encouraged me")
        self.assertEqual(purpose.evidence[0]["ref"], "Alice-ref")
        self.assertEqual(purpose.pii_receipts["evidence"], {"state": "bypass", "writer": "owner"})


class JsonStoreFlagOnTests(_JsonStoreBase):
    def _checked(self):
        return (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring._detect_pii", return_value=[]),
        )

    def test_owner_runtime_and_background_store_placeholders_and_receipts(self):
        redactor_detect, residual_detect = self._checked()
        with redactor_detect, residual_detect:
            entry, owner_data = self._journal_entry()
            review = self._weekly_review()
            pending = self._pending()

        self.assertEqual(entry.wins, ["Called [PERSON_1]"])
        self.assertEqual(entry.pii_receipts["wins"]["state"], "placeholder")
        self.assertEqual(entry.pii_receipts["wins"]["writer"], "owner")
        self.assertEqual(owner_data["wins"], ["Called Alice"])
        self.assertEqual(
            owner_data["pii_receipts"]["wins"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )
        self.assertEqual(review.top_wins, ["Shipped with [PERSON_1]"])
        self.assertEqual(review.pii_receipts["top_wins"]["writer"], "runtime")
        self.assertEqual(review.pii_receipts["top_wins"]["state"], "placeholder")
        self.assertEqual(pending.text, "Ask [PERSON_1] to review the launch")
        self.assertEqual(pending.pii_receipts["text"]["writer"], "background")
        self.assertEqual(pending.pii_receipts["text"]["state"], "placeholder")

    def test_daily_note_entry_stores_placeholder_but_owner_response_rehydrates(self):
        redactor_detect, residual_detect = self._checked()
        with redactor_detect, residual_detect:
            response = self.client.post(
                "/api/v1/journal/daily/2026-08-08/entries/",
                {"content": "Called Alice", "time": "09:00"},
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        note = DailyNote.objects.get(tenant=self.tenant, date=date(2026, 8, 8))
        self.assertIn("Called [PERSON_1]", note.markdown)
        self.assertEqual(note.pii_receipts["markdown"]["state"], "placeholder")
        self.assertIn("Called Alice", response.data["entries"][0]["content"])

    def test_purpose_authors_statement_and_only_registered_json_note(self):
        redactor_detect, residual_detect = self._checked()
        with redactor_detect, residual_detect:
            response = self.client.post(
                "/api/v1/journal/purposes/",
                {
                    "statement": "Build with Alice",
                    "pillars": ["core"],
                    "evidence": [{"kind": "journal", "ref": "Alice-ref", "note": "Alice encouraged me"}],
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        purpose = Purpose.objects.get(tenant=self.tenant)
        self.assertEqual(purpose.statement, "Build with [PERSON_1]")
        self.assertEqual(purpose.evidence[0]["note"], "[PERSON_1] encouraged me")
        self.assertEqual(purpose.evidence[0]["ref"], "Alice-ref")
        self.assertEqual(purpose.pii_receipts["evidence"]["state"], "placeholder")
        self.assertEqual(response.data["statement"], "Build with Alice")
        self.assertEqual(response.data["evidence"][0]["note"], "Alice encouraged me")
