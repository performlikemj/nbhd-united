"""Console sautai account-link view tests (Phase 0.5).

The console connect flow exchanges a one-time key server-side via sautai's
``/link/resolve/`` (mocked here) and stores the resulting ``sautai_user_id`` on
the tenant's Integration row. The raw key must never be persisted.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant

from .models import Integration


@override_settings(SAUTAI_M2M_BASE_URL="https://app.sautai.test", SAUTAI_PLATFORM_SECRET="test-secret")
class SautaiLinkViewTests(TestCase):
    URL = "/api/v1/integrations/sautai/link/"

    def setUp(self):
        self.client = APIClient()
        self.tenant = create_tenant(display_name="Sautai Link", telegram_chat_id=848492)
        self.tenant.user.email = "diner@example.com"
        self.tenant.user.save(update_fields=["email"])
        self.client.force_authenticate(user=self.tenant.user)

    def _integration(self):
        return Integration.objects.filter(tenant=self.tenant, provider=Integration.Provider.SAUTAI).first()

    def test_requires_auth(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.URL).status_code, 401)

    def test_status_unlinked(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["linked"])

    def test_connect_stores_link_and_never_persists_key(self):
        with patch("apps.integrations.sautai_client.resolve_sautai_link_key") as mock_resolve:
            mock_resolve.return_value = {"outcome": "ok", "sautai_user_id": 501, "email": "diner@example.com"}
            resp = self.client.post(self.URL, {"connect_key": "SECRET-KEY-XYZ"}, format="json")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "connected")
        self.assertTrue(body["linked"])
        self.assertEqual(body["email"], "diner@example.com")

        integration = self._integration()
        self.assertEqual(integration.sautai_user_id, 501)
        self.assertIsNotNone(integration.linked_at)
        # The raw connect key is a one-time secret — it must not land on the row.
        for value in (
            integration.key_vault_secret_name,
            integration.composio_connected_account_id,
            integration.provider_email,
        ):
            self.assertNotIn("SECRET-KEY-XYZ", value or "")

    def test_connect_invalid_key_returns_400_and_creates_nothing(self):
        with patch("apps.integrations.sautai_client.resolve_sautai_link_key", return_value={"outcome": "invalid_key"}):
            resp = self.client.post(self.URL, {"connect_key": "bad"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_key")
        self.assertIsNone(self._integration())

    def test_connect_not_configured_returns_503(self):
        with patch(
            "apps.integrations.sautai_client.resolve_sautai_link_key", return_value={"outcome": "not_configured"}
        ):
            resp = self.client.post(self.URL, {"connect_key": "x"}, format="json")
        self.assertEqual(resp.status_code, 503)

    def test_connect_requires_key(self):
        resp = self.client.post(self.URL, {}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_disconnect_clears_link(self):
        Integration.objects.create(
            tenant=self.tenant,
            provider=Integration.Provider.SAUTAI,
            status=Integration.Status.ACTIVE,
            sautai_user_id=501,
            linked_at=timezone.now(),
            provider_email="diner@example.com",
        )
        resp = self.client.delete(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["linked"])

        integration = self._integration()
        self.assertIsNone(integration.sautai_user_id)
        self.assertIsNone(integration.linked_at)
