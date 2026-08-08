"""P3 W3b real-seam coverage for journal's flat long-tail stores."""

from __future__ import annotations

from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import PersonalAccessToken, generate_pat

from .models import Document, NoteTemplate
from .serializers import NoteTemplateSerializer
from .session_models import Session
from .session_views import SessionDetailSerializer


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


class JournalLongTailPlaceholderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="w3b-journal-owner",
            email="w3b-journal-owner@example.test",
            password="x",
        )
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

    @staticmethod
    def _template_payload():
        return {
            "slug": "alice-plan",
            "name": "Alice template",
            "sections": [
                {
                    "slug": "focus",
                    "title": "Alice focus",
                    "content": "Plan the launch with Alice",
                    "source": "human",
                }
            ],
            "is_default": True,
            "source": "human",
        }

    def _pat_client(self):
        raw, prefix, token_hash = generate_pat()
        PersonalAccessToken.objects.create(
            user=self.user,
            name="W3b session PAT",
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=["sessions:write", "sessions:read"],
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
        return client

    @staticmethod
    def _session_payload():
        return {
            "source": "owner-app/1.0",
            "project": "Alice project",
            "session_start": "2026-08-08T01:00:00Z",
            "session_end": "2026-08-08T02:00:00Z",
            "summary": "Reviewed Alice's launch plan",
            "accomplishments": ["Called Alice"],
            "blockers": ["Waiting for Alice"],
            "next_steps": ["Send Alice the draft"],
        }

    def test_note_template_flag_off_preserves_bytes_on_real_create_seam(self):
        response = self.client.post("/api/v1/journal/templates/", self._template_payload(), format="json")

        self.assertEqual(response.status_code, 201)
        template = NoteTemplate.objects.get(tenant=self.tenant, slug="alice-plan")
        self.assertEqual(template.name, self._template_payload()["name"])
        self.assertEqual(template.sections, self._template_payload()["sections"])
        self.assertEqual(template.pii_receipts["name"], {"state": "bypass", "writer": "owner"})
        self.assertEqual(template.pii_receipts["sections"], {"state": "bypass", "writer": "owner"})
        self.assertEqual(response.data["name"], self._template_payload()["name"])
        self.assertEqual(response.data["sections"], self._template_payload()["sections"])

    def test_note_template_authors_sections_and_rejects_forged_receipts(self):
        self._enable_placeholder_writes()
        payload = {
            **self._template_payload(),
            "pii_receipts": {"sections": {"state": "forged", "writer": "runtime"}},
        }
        with _checked_detection():
            response = self.client.post("/api/v1/journal/templates/", payload, format="json")

        self.assertEqual(response.status_code, 201)
        template = NoteTemplate.objects.get(tenant=self.tenant, slug="alice-plan")
        self.assertEqual(template.name, "[PERSON_1] template")
        self.assertEqual(template.sections[0]["title"], "[PERSON_1] focus")
        self.assertEqual(template.sections[0]["content"], "Plan the launch with [PERSON_1]")
        self.assertEqual(template.pii_receipts["name"]["writer"], "owner")
        self.assertEqual(template.pii_receipts["sections"]["writer"], "owner")
        self.assertNotEqual(template.pii_receipts["sections"]["state"], "forged")
        self.assertEqual(response.data["name"], "Alice template")
        self.assertEqual(response.data["sections"], self._template_payload()["sections"])
        self.assertEqual(
            response.data["pii_receipts"]["sections"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )
        daily = self.client.get("/api/v1/journal/daily/2026-08-08/template/")
        self.assertEqual(daily.status_code, 200)
        self.assertEqual(daily.data["template_name"], "Alice template")

    def test_session_pat_create_flag_off_preserves_bytes(self):
        response = self._pat_client().post("/api/v1/sessions/create/", self._session_payload(), format="json")

        self.assertEqual(response.status_code, 201)
        session = Session.objects.get(id=response.data["id"])
        self.assertEqual(session.project, self._session_payload()["project"])
        self.assertEqual(session.summary, self._session_payload()["summary"])
        self.assertEqual(session.accomplishments, self._session_payload()["accomplishments"])
        self.assertEqual(session.pii_receipts["project"], {"state": "bypass", "writer": "owner"})
        self.assertEqual(session.pii_receipts["summary"], {"state": "bypass", "writer": "owner"})

    def test_session_pat_create_stores_placeholders_and_owner_reads_all_fields(self):
        self._enable_placeholder_writes()
        client = self._pat_client()
        with _checked_detection():
            response = client.post("/api/v1/sessions/create/", self._session_payload(), format="json")

        self.assertEqual(response.status_code, 201)
        session = Session.objects.get(id=response.data["id"])
        self.assertEqual(session.project, "[PERSON_1] project")
        self.assertEqual(session.summary, "Reviewed [PERSON_1]'s launch plan")
        self.assertEqual(session.accomplishments, ["Called [PERSON_1]"])
        self.assertEqual(session.pii_receipts["project"]["writer"], "owner")
        self.assertEqual(session.pii_receipts["summary"]["writer"], "owner")
        self.assertEqual(response.data["project"], "Alice project")
        self.assertEqual(response.data["summary"], self._session_payload()["summary"])
        self.assertEqual(
            response.data["pii_receipts"]["summary"]["redactions"],
            [{"placeholder": "[PERSON_1]", "value": "Alice"}],
        )

        session.processed_summary = {"note": "Filed for [PERSON_1]"}
        session.pii_receipts["processed_summary"] = {
            "state": "placeholder",
            "writer": "runtime",
            "redactions": [{"placeholder": "[PERSON_1]"}],
        }
        session.save(update_fields=["processed_summary", "pii_receipts"])
        detail = client.get(f"/api/v1/sessions/{session.id}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["processed_summary"], {"note": "Filed for Alice"})
        self.assertEqual(detail.data["pii_receipts"]["processed_summary"]["writer"], "runtime")

    def test_session_pat_project_filter_translates_literal_name_to_placeholder(self):
        self._enable_placeholder_writes()
        client = self._pat_client()
        with _checked_detection():
            created = client.post("/api/v1/sessions/create/", self._session_payload(), format="json")
        Session.objects.create(
            tenant=self.tenant,
            source="owner-app/1.0",
            project="unrelated project",
            session_start="2026-08-08T03:00:00Z",
            session_end="2026-08-08T04:00:00Z",
            summary="Unrelated session",
        )

        response = client.get("/api/v1/sessions/", {"project": "Alice project"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [created.data["id"]])

    def test_receipt_fields_are_read_only(self):
        self.assertTrue(NoteTemplateSerializer().fields["pii_receipts"].read_only)
        self.assertTrue(SessionDetailSerializer().fields["pii_receipts"].read_only)

    def test_import_assistant_memory_authors_operator_text_as_owner(self):
        self._enable_placeholder_writes()
        output = StringIO()
        with TemporaryDirectory() as src:
            Path(src, "MEMORY.md").write_text("Remember to call Alice.\n", encoding="utf-8")
            with _checked_detection():
                call_command(
                    "import_assistant_memory",
                    email=self.user.email,
                    src=src,
                    apply=True,
                    stdout=output,
                )

        document = Document.objects.get(tenant=self.tenant, kind=Document.Kind.MEMORY, slug="long-term")
        self.assertEqual(document.markdown, "Remember to call [PERSON_1].\n")
        self.assertEqual(document.pii_receipts["markdown"]["writer"], "owner")
        self.assertNotIn("Remember to call Alice", output.getvalue())

    def test_import_assistant_memory_flag_off_preserves_file_bytes(self):
        source_text = "Remember Alice exactly.\n"
        with TemporaryDirectory() as src:
            Path(src, "MEMORY.md").write_text(source_text, encoding="utf-8")
            call_command(
                "import_assistant_memory",
                email=self.user.email,
                src=src,
                apply=True,
                stdout=StringIO(),
            )

        document = Document.objects.get(tenant=self.tenant, kind=Document.Kind.MEMORY, slug="long-term")
        self.assertEqual(document.markdown, source_text)
        self.assertEqual(document.pii_receipts["markdown"], {"state": "bypass", "writer": "owner"})
