"""Tests for content-free eval flags in the current-tenant payload."""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant


class TenantMeEvalFlagsTest(TestCase):
    def setUp(self):
        self.tenant = create_tenant(
            display_name="Eval Flags Me Test",
            telegram_chat_id=555444333,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def test_me_serializes_false_eval_flags(self):
        response = self.client.get("/api/v1/tenants/me/")

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data["is_synthetic"], False)
        self.assertIs(response.data["is_eval_sink"], False)

    def test_me_serializes_true_eval_flags(self):
        self.tenant.is_synthetic = True
        self.tenant.is_eval_sink = True
        self.tenant.save(update_fields=["is_synthetic", "is_eval_sink"])

        response = self.client.get("/api/v1/tenants/me/")

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data["is_synthetic"], True)
        self.assertIs(response.data["is_eval_sink"], True)
