"""Tests for the constellation_enabled tenant flag + settings endpoint.

Constellation is a pure client-side visualization — the PATCH endpoint only
toggles the flag (no plugin, config bump, or restart), so these tests are
narrower than the Core/Heartbeat settings tests.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.models import User
from apps.tenants.services import create_tenant


class ConstellationSettingsAPITest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Constellation Test",
            telegram_chat_id=555444333,
        )
        self.user = self.tenant.user
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.url = "/api/v1/tenants/settings/constellation/"

    def test_patch_enables_flag(self):
        resp = self.client.patch(self.url, {"constellation_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["constellation_enabled"])
        self.assertFalse(resp.data["restart_required"])
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.constellation_enabled)

    def test_patch_disables_flag(self):
        self.tenant.constellation_enabled = True
        self.tenant.save(update_fields=["constellation_enabled"])

        resp = self.client.patch(self.url, {"constellation_enabled": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["constellation_enabled"])
        self.assertFalse(resp.data["restart_required"])
        self.tenant.refresh_from_db()
        self.assertFalse(self.tenant.constellation_enabled)

    def test_patch_missing_field_returns_400(self):
        resp = self.client.patch(self.url, {}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_rejected(self):
        client = APIClient()
        resp = client.patch(self.url, {"constellation_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_no_tenant_returns_404(self):
        user_no_tenant = User.objects.create_user(
            username="no_tenant_constellation",
            display_name="No Tenant",
        )
        client = APIClient()
        client.force_authenticate(user=user_no_tenant)
        resp = client.patch(self.url, {"constellation_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 404)


class TenantMeConstellationFieldTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Constellation Me Test",
            telegram_chat_id=555444222,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def test_me_includes_constellation_enabled_default_false(self):
        resp = self.client.get("/api/v1/tenants/me/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("constellation_enabled", resp.data)
        self.assertFalse(resp.data["constellation_enabled"])
