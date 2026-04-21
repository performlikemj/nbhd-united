"""Tests for Session API endpoints."""

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import generate_pat, PersonalAccessToken
from apps.tenants.services import create_tenant

from .models import Document
from .session_models import Session


class SessionCreateTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Session User", telegram_chat_id=800)
        self.user = self.tenant.user
        # Create PAT for this user
        raw, prefix, token_hash = generate_pat()
        self.pat = PersonalAccessToken.objects.create(
            user=self.user, name="Test PAT", token_prefix=prefix, token_hash=token_hash,
        )
        self.raw_token = raw
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.raw_token}")

        self.payload = {
            "source": "yardtalk-mac/1.0.0",
            "project": "acme-labs-presentation",
            "project_type": "presentation_prep",
            "session_start": "2026-04-21T14:00:00Z",
            "session_end": "2026-04-21T15:30:00Z",
            "summary": "Built first draft of slide deck.",
            "accomplishments": ["deck outline complete", "hero slide designed"],
            "blockers": ["conclusion feels flat"],
            "next_steps": ["revisit closer tomorrow"],
            "references": {"report_url": "file:///tmp/report.html", "clip_ids": ["abc123"]},
        }

    def test_create_session(self):
        response = self.client.post("/api/v1/sessions/create/", self.payload, format="json")
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["project"], "acme-labs-presentation")
        self.assertEqual(data["source"], "yardtalk-mac/1.0.0")
        self.assertEqual(len(data["accomplishments"]), 2)

    def test_create_session_auto_creates_project_document(self):
        self.client.post("/api/v1/sessions/create/", self.payload, format="json")
        self.assertTrue(
            Document.objects.filter(
                tenant=self.tenant, kind=Document.Kind.PROJECT, slug="acme-labs-presentation",
            ).exists()
        )

    def test_create_session_with_test_mode(self):
        self.payload["test_mode"] = True
        response = self.client.post("/api/v1/sessions/create/", self.payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["test_mode"])

    def test_create_session_validation_end_before_start(self):
        self.payload["session_end"] = "2026-04-21T13:00:00Z"
        response = self.client.post("/api/v1/sessions/create/", self.payload, format="json")
        self.assertEqual(response.status_code, 400)

    def test_idempotency_key_dedup(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {self.raw_token}",
            HTTP_IDEMPOTENCY_KEY="unique-key-123",
        )
        r1 = self.client.post("/api/v1/sessions/create/", self.payload, format="json")
        self.assertEqual(r1.status_code, 201)

        r2 = self.client.post("/api/v1/sessions/create/", self.payload, format="json")
        self.assertEqual(r2.status_code, 200)  # Returns existing
        self.assertEqual(r1.json()["id"], r2.json()["id"])

    def test_no_idempotency_key_creates_duplicates(self):
        r1 = self.client.post("/api/v1/sessions/create/", self.payload, format="json")
        r2 = self.client.post("/api/v1/sessions/create/", self.payload, format="json")
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r1.json()["id"], r2.json()["id"])

    def test_unauthenticated_returns_401(self):
        client = APIClient()
        response = client.post("/api/v1/sessions/create/", self.payload, format="json")
        self.assertEqual(response.status_code, 401)


class SessionListTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="List User", telegram_chat_id=801)
        self.user = self.tenant.user
        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        # Create test sessions
        Session.objects.create(
            tenant=self.tenant, source="yardtalk-mac/1.0.0",
            project="project-a", session_start="2026-04-21T10:00:00Z",
            session_end="2026-04-21T11:00:00Z", summary="Worked on A",
        )
        Session.objects.create(
            tenant=self.tenant, source="yardtalk-mac/1.0.0",
            project="project-b", session_start="2026-04-21T12:00:00Z",
            session_end="2026-04-21T13:00:00Z", summary="Worked on B",
        )
        Session.objects.create(
            tenant=self.tenant, source="yardtalk-mac/1.0.0",
            project="project-a", session_start="2026-04-21T14:00:00Z",
            session_end="2026-04-21T15:00:00Z", summary="More work on A",
            test_mode=True,
        )

    def test_list_sessions(self):
        response = self.client.get("/api/v1/sessions/")
        self.assertEqual(response.status_code, 200)
        # test_mode session excluded by default
        self.assertEqual(len(response.json()), 2)

    def test_list_filter_by_project(self):
        response = self.client.get("/api/v1/sessions/?project=project-a")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_list_include_test_mode(self):
        response = self.client.get("/api/v1/sessions/?include_test=true")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 3)

    def test_list_with_limit(self):
        response = self.client.get("/api/v1/sessions/?limit=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

    def test_list_invalid_limit_returns_400(self):
        response = self.client.get("/api/v1/sessions/?limit=abc")
        self.assertEqual(response.status_code, 400)
        self.assertIn("limit", response.json()["detail"].lower())

    def test_list_tenant_isolation(self):
        """Sessions from other tenants are never visible."""
        other_tenant = create_tenant(display_name="Other User", telegram_chat_id=802)
        Session.objects.create(
            tenant=other_tenant, source="yardtalk-mac/1.0.0",
            project="other-project", session_start="2026-04-21T10:00:00Z",
            session_end="2026-04-21T11:00:00Z", summary="Other's work",
        )

        response = self.client.get("/api/v1/sessions/")
        projects = [s["project"] for s in response.json()]
        self.assertNotIn("other-project", projects)


class SessionDetailTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Detail User", telegram_chat_id=803)
        self.user = self.tenant.user
        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        self.session = Session.objects.create(
            tenant=self.tenant, source="yardtalk-mac/1.0.0",
            project="detail-project", session_start="2026-04-21T10:00:00Z",
            session_end="2026-04-21T11:00:00Z", summary="Detail test",
            accomplishments=["thing done"], blockers=["stuck on X"],
        )

    def test_get_session_detail(self):
        response = self.client.get(f"/api/v1/sessions/{self.session.id}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["project"], "detail-project")
        self.assertEqual(data["accomplishments"], ["thing done"])

    def test_delete_session(self):
        response = self.client.delete(f"/api/v1/sessions/{self.session.id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Session.objects.filter(id=self.session.id).exists())

    def test_get_other_tenant_session_returns_404(self):
        other_tenant = create_tenant(display_name="Other Detail", telegram_chat_id=804)
        other_session = Session.objects.create(
            tenant=other_tenant, source="yardtalk-mac/1.0.0",
            project="other", session_start="2026-04-21T10:00:00Z",
            session_end="2026-04-21T11:00:00Z", summary="Other's session",
        )

        response = self.client.get(f"/api/v1/sessions/{other_session.id}/")
        self.assertEqual(response.status_code, 404)
