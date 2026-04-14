"""Tests for tenant-facing workspace API (Phase 5).

JWT-authed endpoints for the subscriber console. Mirrors the runtime API tests
but uses Django's APIClient with force_authenticate instead of internal-key
headers.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.journal.models import Workspace
from apps.tenants.models import Tenant, User


def _fake_embedding(_text):
    """Stand-in for OpenAI embedding calls in tests."""
    return [0.01] * 1536


def _create_user_and_tenant(username: str, telegram_chat_id: int) -> tuple[User, Tenant]:
    user = User.objects.create_user(username=username, password="testpass123")
    tenant = Tenant.objects.create(
        user=user,
        status=Tenant.Status.ACTIVE,
        container_id="oc-test",
        container_fqdn="oc-test.internal.azurecontainerapps.io",
    )
    user.telegram_chat_id = telegram_chat_id
    user.save(update_fields=["telegram_chat_id"])
    return user, tenant


@patch("apps.lessons.services.generate_embedding", side_effect=_fake_embedding)
class WorkspaceViewsTest(TestCase):
    def setUp(self):
        self.user, self.tenant = _create_user_and_tenant("wsuser", 404040)
        self.other_user, self.other_tenant = _create_user_and_tenant("otherwsuser", 404041)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _list(self):
        return self.client.get("/api/v1/workspaces/")

    def _create(self, name="Work", description="budget meetings"):
        return self.client.post(
            "/api/v1/workspaces/",
            {"name": name, "description": description},
            format="json",
        )

    def _patch(self, slug, body):
        return self.client.patch(
            f"/api/v1/workspaces/{slug}/",
            body,
            format="json",
        )

    def _delete(self, slug):
        return self.client.delete(f"/api/v1/workspaces/{slug}/")

    def _switch(self, slug):
        return self.client.post(
            "/api/v1/workspaces/switch/",
            {"slug": slug},
            format="json",
        )

    # ── Auth ─────────────────────────────────────────────────────────────

    def test_list_requires_auth(self, _embed):
        anonymous = APIClient()
        response = anonymous.get("/api/v1/workspaces/")
        self.assertEqual(response.status_code, 401)

    def test_create_requires_auth(self, _embed):
        anonymous = APIClient()
        response = anonymous.post(
            "/api/v1/workspaces/",
            {"name": "Work"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_patch_requires_auth(self, _embed):
        anonymous = APIClient()
        response = anonymous.patch(
            "/api/v1/workspaces/work/",
            {"name": "Career"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    # ── List ─────────────────────────────────────────────────────────────

    def test_list_empty_for_new_tenant(self, _embed):
        response = self._list()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workspaces"], [])
        self.assertIsNone(body["active_workspace_id"])
        self.assertEqual(body["limit"], 4)
        self.assertEqual(body["tenant_id"], str(self.tenant.id))

    def test_list_returns_only_own_workspaces(self, _embed):
        # Create workspace for the other tenant
        Workspace.objects.create(
            tenant=self.other_tenant,
            name="Other",
            slug="other",
            description="not yours",
        )
        # Create workspace for self
        self._create(name="Mine")

        response = self._list()
        body = response.json()
        # Should only see "Mine" + auto-created General — never "Other"
        names = {ws["name"] for ws in body["workspaces"]}
        self.assertIn("Mine", names)
        self.assertIn("General", names)
        self.assertNotIn("Other", names)

    def test_list_shows_active_marker(self, _embed):
        self._create(name="Work")
        response = self._list()
        body = response.json()
        active = [ws for ws in body["workspaces"] if ws["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "Work")

    # ── Create ───────────────────────────────────────────────────────────

    def test_create_first_workspace_auto_creates_default(self, _embed):
        response = self._create(name="Translation", description="Japanese to English")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["default_workspace_created"])

        self.assertEqual(Workspace.objects.filter(tenant=self.tenant).count(), 2)
        self.assertTrue(Workspace.objects.filter(tenant=self.tenant, slug="general", is_default=True).exists())

    def test_create_at_limit_returns_409(self, _embed):
        self._create(name="Work")  # → general + work = 2
        self._create(name="Personal")  # → 3
        self._create(name="Translation")  # → 4
        response = self._create(name="Fitness")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "workspace_limit_reached")

    def test_create_with_duplicate_name_generates_unique_slug(self, _embed):
        self._create(name="Work")
        response = self._create(name="Work")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["workspace"]["slug"], "work-2")

    def test_create_requires_name(self, _embed):
        response = self.client.post(
            "/api/v1/workspaces/",
            {"description": "no name"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_long_name(self, _embed):
        response = self._create(name="x" * 61)
        self.assertEqual(response.status_code, 400)

    def test_create_marks_new_workspace_active(self, _embed):
        response = self._create(name="Work")
        self.assertTrue(response.json()["workspace"]["is_active"])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.active_workspace.slug, "work")

    def test_create_generates_description_embedding(self, embed_mock):
        self._create(name="Work", description="budget meetings")
        ws = Workspace.objects.get(tenant=self.tenant, slug="work")
        self.assertIsNotNone(ws.description_embedding)
        self.assertTrue(embed_mock.called)

    # ── Update ───────────────────────────────────────────────────────────

    def test_patch_updates_name(self, _embed):
        self._create(name="Work")
        response = self._patch("work", {"name": "Career"})
        self.assertEqual(response.status_code, 200)
        ws = Workspace.objects.get(tenant=self.tenant, slug="work")
        self.assertEqual(ws.name, "Career")

    def test_patch_updates_description_and_re_embeds(self, embed_mock):
        self._create(name="Work")
        embed_mock.reset_mock()
        response = self._patch("work", {"description": "new focus"})
        self.assertEqual(response.status_code, 200)
        embed_mock.assert_called()

    def test_patch_nonexistent_returns_404(self, _embed):
        response = self._patch("nope", {"name": "Whatever"})
        self.assertEqual(response.status_code, 404)

    def test_patch_empty_name_rejected(self, _embed):
        self._create(name="Work")
        response = self._patch("work", {"name": ""})
        self.assertEqual(response.status_code, 400)

    def test_cannot_patch_other_tenants_workspace(self, _embed):
        # Other tenant has a workspace
        Workspace.objects.create(
            tenant=self.other_tenant,
            name="Theirs",
            slug="theirs",
        )
        # Self user tries to patch it — should 404 (not visible)
        response = self._patch("theirs", {"name": "Mine Now"})
        self.assertEqual(response.status_code, 404)

    # ── Delete ───────────────────────────────────────────────────────────

    def test_delete_default_workspace_returns_409(self, _embed):
        self._create(name="Work")
        response = self._delete("general")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "cannot_delete_default")
        self.assertTrue(Workspace.objects.filter(tenant=self.tenant, slug="general").exists())

    def test_delete_nondefault_workspace_succeeds(self, _embed):
        self._create(name="Work")
        response = self._delete("work")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Workspace.objects.filter(tenant=self.tenant, slug="work").exists())

    def test_delete_active_workspace_falls_back_to_default(self, _embed):
        self._create(name="Work")
        response = self._delete("work")
        self.assertTrue(response.json()["fell_back_to_default"])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.active_workspace.slug, "general")

    def test_delete_nonexistent_returns_404(self, _embed):
        response = self._delete("nope")
        self.assertEqual(response.status_code, 404)

    # ── Switch ───────────────────────────────────────────────────────────

    def test_switch_changes_active_workspace(self, _embed):
        self._create(name="Work")
        response = self._switch("general")
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.active_workspace.slug, "general")

    def test_switch_to_nonexistent_returns_404(self, _embed):
        self._create(name="Work")
        response = self._switch("nope")
        self.assertEqual(response.status_code, 404)

    def test_switch_requires_slug(self, _embed):
        self._create(name="Work")
        response = self.client.post(
            "/api/v1/workspaces/switch/",
            {},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_cannot_switch_to_other_tenants_workspace(self, _embed):
        Workspace.objects.create(
            tenant=self.other_tenant,
            name="Theirs",
            slug="theirs",
        )
        response = self._switch("theirs")
        self.assertEqual(response.status_code, 404)
