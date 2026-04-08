"""Tests for workspace runtime endpoints (Phase 3 of workspace routing).

These endpoints let the agent (and the subscriber console) manage workspaces
via plugin tools. Critical behaviors:

- Auth required (X-NBHD-Internal-Key + X-NBHD-Tenant-Id)
- First create auto-generates a "General" default workspace
- Max 4 workspaces per tenant
- Slug auto-generated and de-duplicated
- Cannot delete the default workspace
- Deleting the active workspace falls back to the default
- Description embedding generated/updated whenever description changes
"""
from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.test.utils import override_settings

from apps.journal.models import Workspace
from apps.tenants.services import create_tenant


def _fake_embedding(_text):
    """Stand-in for OpenAI embedding calls in tests."""
    return [0.01] * 1536


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
@patch("apps.lessons.services.generate_embedding", side_effect=_fake_embedding)
class RuntimeWorkspaceViewsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="WS Test", telegram_chat_id=303030)
        self.other_tenant = create_tenant(display_name="Other WS", telegram_chat_id=303031)

    # ── Helpers ──────────────────────────────────────────────────────────

    def _headers(self, tenant_id: str | None = None, key: str = "shared-key") -> dict:
        return {
            "HTTP_X_NBHD_INTERNAL_KEY": key,
            "HTTP_X_NBHD_TENANT_ID": tenant_id or str(self.tenant.id),
        }

    def _list(self):
        return self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/",
            **self._headers(),
        )

    def _create(self, name="Work", description="budget meetings"):
        return self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/",
            data={"name": name, "description": description},
            content_type="application/json",
            **self._headers(),
        )

    def _patch(self, slug, body):
        return self.client.patch(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/{slug}/",
            data=body,
            content_type="application/json",
            **self._headers(),
        )

    def _delete(self, slug):
        return self.client.delete(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/{slug}/",
            **self._headers(),
        )

    def _switch(self, slug):
        return self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/switch/",
            data={"slug": slug},
            content_type="application/json",
            **self._headers(),
        )

    # ── Auth ─────────────────────────────────────────────────────────────

    def test_list_requires_auth(self, _embed_mock):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/",
        )
        self.assertEqual(response.status_code, 401)

    def test_list_rejects_tenant_mismatch(self, _embed_mock):
        response = self.client.get(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/",
            HTTP_X_NBHD_INTERNAL_KEY="shared-key",
            HTTP_X_NBHD_TENANT_ID=str(self.other_tenant.id),
        )
        self.assertEqual(response.status_code, 401)

    # ── List ─────────────────────────────────────────────────────────────

    def test_list_empty_for_new_tenant(self, _embed_mock):
        response = self._list()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workspaces"], [])
        self.assertIsNone(body["active_workspace_id"])
        self.assertEqual(body["limit"], 4)

    def test_list_returns_workspaces_with_active_marker(self, _embed_mock):
        self._create(name="Work")
        response = self._list()
        body = response.json()
        # Should have General + Work
        self.assertEqual(len(body["workspaces"]), 2)
        names = {ws["name"] for ws in body["workspaces"]}
        self.assertIn("General", names)
        self.assertIn("Work", names)
        # Work should be the active one
        active = [ws for ws in body["workspaces"] if ws["is_active"]]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["name"], "Work")

    # ── Create ───────────────────────────────────────────────────────────

    def test_create_first_workspace_auto_creates_default(self, _embed_mock):
        response = self._create(name="Translation", description="Japanese to English")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["default_workspace_created"])

        # Two workspaces now exist: General + Translation
        self.assertEqual(Workspace.objects.filter(tenant=self.tenant).count(), 2)
        self.assertTrue(
            Workspace.objects.filter(
                tenant=self.tenant, slug="general", is_default=True
            ).exists()
        )
        self.assertTrue(
            Workspace.objects.filter(
                tenant=self.tenant, slug="translation"
            ).exists()
        )

    def test_create_second_workspace_does_not_recreate_default(self, _embed_mock):
        self._create(name="Work")
        # Now create another — default should NOT be recreated
        response = self._create(name="Personal")
        body = response.json()
        self.assertFalse(body["default_workspace_created"])
        self.assertEqual(Workspace.objects.filter(tenant=self.tenant).count(), 3)

    def test_create_at_limit_returns_409(self, _embed_mock):
        # Create up to the limit (4 total)
        self._create(name="Work")  # → general + work = 2
        self._create(name="Personal")  # → 3
        self._create(name="Translation")  # → 4
        # 5th creation should fail
        response = self._create(name="Fitness")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "workspace_limit_reached")

    def test_create_with_duplicate_name_generates_unique_slug(self, _embed_mock):
        self._create(name="Work")
        response = self._create(name="Work")
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["workspace"]["slug"], "work-2")

    def test_create_requires_name(self, _embed_mock):
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/",
            data={"description": "no name"},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_long_name(self, _embed_mock):
        response = self._create(name="x" * 61)
        self.assertEqual(response.status_code, 400)

    def test_create_marks_new_workspace_as_active(self, _embed_mock):
        response = self._create(name="Work")
        body = response.json()
        self.assertTrue(body["workspace"]["is_active"])
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.active_workspace.slug, "work")

    def test_create_generates_description_embedding(self, embed_mock):
        self._create(name="Work", description="budget meetings")
        # Both default + new workspace get embeddings
        ws = Workspace.objects.get(tenant=self.tenant, slug="work")
        self.assertIsNotNone(ws.description_embedding)
        self.assertTrue(embed_mock.called)

    # ── Update ───────────────────────────────────────────────────────────

    def test_patch_updates_name(self, _embed_mock):
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
        # Embedding should be regenerated for the updated description
        embed_mock.assert_called()

    def test_patch_nonexistent_returns_404(self, _embed_mock):
        response = self._patch("nope", {"name": "Whatever"})
        self.assertEqual(response.status_code, 404)

    def test_patch_empty_name_rejected(self, _embed_mock):
        self._create(name="Work")
        response = self._patch("work", {"name": ""})
        self.assertEqual(response.status_code, 400)

    # ── Delete ───────────────────────────────────────────────────────────

    def test_delete_default_workspace_returns_409(self, _embed_mock):
        self._create(name="Work")
        # General is the default
        response = self._delete("general")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "cannot_delete_default")
        # General still exists
        self.assertTrue(
            Workspace.objects.filter(tenant=self.tenant, slug="general").exists()
        )

    def test_delete_nondefault_workspace_succeeds(self, _embed_mock):
        self._create(name="Work")
        response = self._delete("work")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Workspace.objects.filter(tenant=self.tenant, slug="work").exists()
        )

    def test_delete_active_workspace_falls_back_to_default(self, _embed_mock):
        self._create(name="Work")  # Now active
        response = self._delete("work")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["fell_back_to_default"])

        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.active_workspace.slug, "general")

    def test_delete_nonexistent_returns_404(self, _embed_mock):
        response = self._delete("nope")
        self.assertEqual(response.status_code, 404)

    # ── Switch ───────────────────────────────────────────────────────────

    def test_switch_changes_active_workspace(self, _embed_mock):
        self._create(name="Work")  # Now active
        response = self._switch("general")
        self.assertEqual(response.status_code, 200)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.active_workspace.slug, "general")

    def test_switch_returns_previous_workspace_id(self, _embed_mock):
        create_resp = self._create(name="Work")
        work_id = create_resp.json()["workspace"]["id"]
        response = self._switch("general")
        body = response.json()
        self.assertEqual(body["previous_workspace_id"], work_id)

    def test_switch_to_nonexistent_returns_404(self, _embed_mock):
        self._create(name="Work")
        response = self._switch("nope")
        self.assertEqual(response.status_code, 404)

    def test_switch_requires_slug(self, _embed_mock):
        self._create(name="Work")
        response = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/workspaces/switch/",
            data={},
            content_type="application/json",
            **self._headers(),
        )
        self.assertEqual(response.status_code, 400)

    def test_switch_updates_last_used_at(self, _embed_mock):
        self._create(name="Work")
        general = Workspace.objects.get(tenant=self.tenant, slug="general")
        prior_last_used = general.last_used_at
        self._switch("general")
        general.refresh_from_db()
        self.assertNotEqual(general.last_used_at, prior_last_used)

    # ── Tenant scoping ───────────────────────────────────────────────────

    def test_workspaces_are_tenant_scoped(self, _embed_mock):
        self._create(name="Work")
        # Other tenant should see no workspaces
        self.assertEqual(
            Workspace.objects.filter(tenant=self.other_tenant).count(), 0
        )
