"""The owner profile exposes the two flags required by the E2E safety gate."""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant


class TenantMeSafetyFieldsTest(TestCase):
    def test_me_exposes_synthetic_and_eval_sink_independently(self):
        tenant = create_tenant(display_name="E2E gate fields", telegram_chat_id=555444110)
        tenant.is_synthetic = True
        tenant.is_eval_sink = False
        tenant.save(update_fields=["is_synthetic", "is_eval_sink"])
        client = APIClient()
        client.force_authenticate(user=tenant.user)

        response = client.get("/api/v1/tenants/me/")

        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data["is_synthetic"], True)
        self.assertIs(response.data["is_eval_sink"], False)
