"""Tests for datebook_enabled in the current-tenant payload."""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant


class TenantMeDatebookFieldTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Datebook Me Test",
            telegram_chat_id=555444111,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def test_me_includes_datebook_enabled_and_reflects_model_value(self):
        resp = self.client.get("/api/v1/tenants/me/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("datebook_enabled", resp.data)
        self.assertFalse(resp.data["datebook_enabled"])

        self.tenant.datebook_enabled = True
        self.tenant.save(update_fields=["datebook_enabled"])

        resp = self.client.get("/api/v1/tenants/me/")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data["datebook_enabled"])
