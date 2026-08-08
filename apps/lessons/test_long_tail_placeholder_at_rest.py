"""P3 W3b real writer/read seams for lessons and the constellation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.journal.models import PendingExtraction
from apps.router.extraction_callbacks import _approve_lesson
from apps.tenants.models import Tenant, User

from .agent_context import constellation_notes_payload
from .models import Lesson, StarJournalEntry, TutoringSession
from .serializers import (
    ConstellationNodeSerializer,
    GalaxyStarSerializer,
    LessonCreateSerializer,
    LessonSerializer,
    StarDetailSerializer,
    StarJournalEntrySerializer,
)
from .tasks import reseed_lessons_single_tenant_task


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


def _text_receipt(*, writer="background"):
    return {
        "text": {
            "state": "placeholder",
            "writer": writer,
            "redactions": [{"placeholder": "[PERSON_1]"}],
        }
    }


class LessonLongTailPlaceholderTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="w3b-lessons-owner", password="x")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            pii_entity_map={"[PERSON_1]": {"name": "Alice"}},
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _enable_placeholder_writes(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

    def _star(self, **overrides):
        defaults = {
            "tenant": self.tenant,
            "text": "Learn with [PERSON_1]",
            "context": "Advice from [PERSON_1]",
            "source_type": "experience",
            "source_ref": "w3b",
            "tags": ["test"],
            "status": "approved",
            "approved_at": timezone.now(),
            "pii_receipts": {
                **_text_receipt(),
                "context": {
                    "state": "placeholder",
                    "writer": "background",
                    "redactions": [{"placeholder": "[PERSON_1]"}],
                },
            },
        }
        defaults.update(overrides)
        return Lesson.objects.create(**defaults)

    def test_owner_create_flag_off_is_byte_identical(self):
        payload = {
            "text": "Learn with Alice",
            "context": "Advice from Alice",
            "source_type": "experience",
            "source_ref": "w3b",
            "tags": ["test"],
        }

        response = self.client.post("/api/v1/lessons/", payload, format="json")

        self.assertEqual(response.status_code, 201)
        lesson = Lesson.objects.get(id=response.data["id"])
        self.assertEqual(lesson.text, payload["text"])
        self.assertEqual(lesson.context, payload["context"])
        self.assertEqual(lesson.pii_receipts["text"], {"state": "bypass", "writer": "owner"})
        self.assertEqual(response.data["text"], payload["text"])

    def test_owner_crud_and_star_writes_store_placeholders_but_echo_real_values(self):
        self._enable_placeholder_writes()
        payload = {
            "text": "Learn with Alice",
            "context": "Advice from Alice",
            "source_type": "experience",
            "source_ref": "w3b",
            "tags": ["test"],
        }
        with _checked_detection():
            created = self.client.post("/api/v1/lessons/", payload, format="json")

        self.assertEqual(created.status_code, 201)
        lesson = Lesson.objects.get(id=created.data["id"])
        self.assertEqual(lesson.text, "Learn with [PERSON_1]")
        self.assertEqual(lesson.pii_receipts["text"]["writer"], "owner")
        self.assertEqual(created.data["text"], payload["text"])
        self.assertEqual(
            created.data["pii_receipts"]["text"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

        with _checked_detection():
            updated = self.client.patch(
                f"/api/v1/lessons/{lesson.id}/",
                {
                    "text": "Ask Alice earlier",
                    "pii_receipts": {"text": {"state": "forged", "writer": "runtime"}},
                },
                format="json",
            )
        self.assertEqual(updated.status_code, 200)
        lesson.refresh_from_db()
        self.assertEqual(lesson.text, "Ask [PERSON_1] earlier")
        self.assertEqual(lesson.pii_receipts["text"]["writer"], "owner")
        self.assertEqual(updated.data["text"], "Ask Alice earlier")

        lesson.status = "approved"
        lesson.save(update_fields=["status"])
        with _checked_detection():
            journal = self.client.post(
                f"/api/v1/lessons/{lesson.id}/journal/create/",
                {"text": "Alice helped this click", "entry_type": "free"},
                format="json",
            )
            pinned = self.client.patch(
                f"/api/v1/lessons/{lesson.id}/pin-note/",
                {"note": "Ask Alice about this"},
                format="json",
            )

        entry = StarJournalEntry.objects.get(id=journal.data["id"])
        lesson.refresh_from_db()
        self.assertEqual(entry.text, "[PERSON_1] helped this click")
        self.assertEqual(entry.pii_receipts["text"]["writer"], "owner")
        self.assertEqual(journal.data["text"], "Alice helped this click")
        self.assertEqual(lesson.galaxy_note, "Ask [PERSON_1] about this")
        self.assertEqual(pinned.data["galaxy_note"], "Ask Alice about this")

    def test_owner_galaxy_tutor_and_journal_projections_rehydrate_with_receipts(self):
        self._enable_placeholder_writes()
        star = self._star(last_visited_at=timezone.now())
        entry = StarJournalEntry.objects.create(
            tenant=self.tenant,
            star=star,
            text=("x" * 195) + "[PERSON_1] suffix",
            entry_type="free",
            pii_receipts=_text_receipt(writer="owner"),
        )
        TutoringSession.objects.create(star=star, phases_completed=["restate"])

        summary = self.client.get("/api/v1/lessons/galaxy/summary/")
        insights = self.client.get("/api/v1/lessons/galaxy/insights/")
        landed = self.client.post(f"/api/v1/lessons/{star.id}/land/")

        self.assertEqual(summary.data["recent_activity"][0]["text"], "Learn with Alice")
        self.assertEqual(
            summary.data["recent_activity"][0]["pii_receipts"]["text"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )
        self.assertEqual(insights.data[0]["star_text"], "Learn with Alice")
        self.assertEqual(insights.data[0]["pii_receipts"]["text"]["writer"], "background")
        journal_preview = next(item for item in landed.data["journal_entries"] if item["id"] == str(entry.id))
        self.assertIn("Alice", journal_preview["text"])
        self.assertNotIn("[PERS", journal_preview["text"])
        self.assertEqual(journal_preview["pii_receipts"]["text"]["writer"], "owner")

        # The stored placeholder crosses the raw 120-character projection
        # boundary. Owner reads must rehydrate the full value before capping it.
        star.text = ("y" * 115) + "[PERSON_1] suffix"
        star.save(update_fields=["text"])

        with patch("apps.lessons.tutoring._tutor_request") as tutor_request:
            tutor_request.return_value = {
                "text": "What did [PERSON_1] teach you?",
                "current_phase": "restate",
                "phase_complete": False,
            }
            started = self.client.post(f"/api/v1/lessons/{star.id}/tutor/start/")
        self.assertEqual(started.data["message"], "What did Alice teach you?")
        cached = cache.get(f"tutoring:{started.data['session_id']}")
        self.assertEqual(cached["messages"][-1]["content"], "What did [PERSON_1] teach you?")

        state = self.client.get(f"/api/v1/lessons/{star.id}/tutor/state/?session_id={started.data['session_id']}")
        self.assertEqual(state.data["star_text"], ("y" * 115) + "Alice")
        self.assertNotIn("[PERS", state.data["star_text"])
        self.assertEqual(state.data["pii_receipts"]["text"]["writer"], "background")

    def test_runtime_constellation_context_stays_placeholder_space_without_receipts(self):
        star = self._star(galaxy_note="Ask [PERSON_1] next")
        StarJournalEntry.objects.create(
            tenant=self.tenant,
            star=star,
            text="Reflected with [PERSON_1]",
            pii_receipts=_text_receipt(writer="owner"),
        )
        StarJournalEntry.objects.create(
            tenant=self.tenant,
            star=star,
            text=("z" * 395) + "[PERSON_1] suffix",
            pii_receipts=_text_receipt(writer="owner"),
        )

        payload = constellation_notes_payload(self.tenant, star_id=star.id)

        node = payload["stars"][0]
        self.assertEqual(node["text"], "Learn with [PERSON_1]")
        self.assertEqual(node["galaxy_note"], "Ask [PERSON_1] next")
        self.assertEqual(node["journal_entries"][0]["text"], ("z" * 395) + "…")
        self.assertNotIn("[PERS", node["journal_entries"][0]["text"])
        self.assertEqual(node["journal_entries"][1]["text"], "Reflected with [PERSON_1]")
        self.assertNotIn("pii_receipts", str(payload))

    @patch("apps.router.extraction_callbacks.process_approved_lesson")
    def test_extraction_approval_authors_lesson_as_background(self, _process):
        self._enable_placeholder_writes()
        pending = PendingExtraction.objects.create(
            tenant=self.tenant,
            kind=PendingExtraction.Kind.LESSON,
            text="Ask Alice for the exact constraints",
            tags=["work"],
            source_date=date(2026, 8, 8),
            expires_at=timezone.now() + timedelta(days=1),
        )

        with _checked_detection():
            _message, lesson_id = _approve_lesson(pending)

        lesson = Lesson.objects.get(id=lesson_id)
        self.assertEqual(lesson.text, "Ask [PERSON_1] for the exact constraints")
        self.assertEqual(lesson.pii_receipts["text"]["writer"], "background")

    @patch("apps.lessons.clustering.refresh_constellation")
    @patch("apps.lessons.services.process_approved_lesson")
    @patch("apps.journal.extraction._embedding_duplicate", return_value=False)
    @patch("apps.journal.extraction._call_extraction_llm")
    @patch("apps.lessons.tasks._gather_notes")
    def test_daily_note_reseed_authors_lesson_as_background(
        self,
        gather_notes,
        extraction_llm,
        _duplicate,
        _process,
        _cluster,
    ):
        self._enable_placeholder_writes()
        gather_notes.return_value = [(date(2026, 8, 8), "A sufficiently long daily note for extraction.")]
        extraction_llm.return_value = (
            {"lessons": [{"text": "Always ask Alice for constraints early", "tags": ["work"]}]},
            {},
        )

        with _checked_detection():
            result = reseed_lessons_single_tenant_task(str(self.tenant.id))

        self.assertEqual(result["added"], 1)
        lesson = Lesson.objects.get(tenant=self.tenant, source_ref="reseed")
        self.assertEqual(lesson.text, "Always ask [PERSON_1] for constraints early")
        self.assertEqual(lesson.pii_receipts["text"]["writer"], "background")

    @override_settings(OPENROUTER_API_KEY="test-key")
    @patch("apps.lessons.clustering.refresh_constellation")
    @patch("apps.lessons.management.commands.rewrite_lessons_actionable.process_approved_lesson")
    @patch("apps.lessons.management.commands.rewrite_lessons_actionable.Command._rewrite")
    def test_actionable_rewrite_merges_background_receipt(self, rewrite, _process, _cluster):
        self._enable_placeholder_writes()
        lesson = self._star(text="Old [PERSON_1] lesson", pii_receipts=_text_receipt(writer="owner"))
        rewrite.return_value = "Ask Alice for constraints before starting"

        with _checked_detection():
            call_command(
                "rewrite_lessons_actionable",
                tenant=str(self.tenant.id),
                stdout=StringIO(),
            )

        lesson.refresh_from_db()
        self.assertEqual(lesson.text, "Ask [PERSON_1] for constraints before starting")
        self.assertEqual(lesson.pii_receipts["text"]["writer"], "background")

    def test_all_exposed_receipt_fields_are_read_only(self):
        serializers = (
            LessonSerializer(),
            LessonCreateSerializer(),
            ConstellationNodeSerializer(),
            GalaxyStarSerializer(),
            StarDetailSerializer(),
            StarJournalEntrySerializer(),
        )
        for serializer in serializers:
            self.assertTrue(serializer.fields["pii_receipts"].read_only)
