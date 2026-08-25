"""Sub-agent runtime usage accounting tests."""

from django.test import TestCase, override_settings

from apps.billing.constants import MINIMAX_MODEL
from apps.billing.models import UsageRecord
from apps.tenants.models import Tenant
from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key


@override_settings(NBHD_INTERNAL_API_KEY="shared-key")
class SubagentUsageReportTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Subagent Usage", telegram_chat_id=424244)
        seed_internal_key(self.tenant)

    def test_metadata_and_cost_persist_without_message_quota(self):
        before = Tenant.objects.get(id=self.tenant.id)
        response = self.client.post(
            f"/api/v1/internal/runtime/{self.tenant.id}/usage/report/",
            data={
                "event_type": "subagent_message",
                "input_tokens": 100,
                "output_tokens": 50,
                "model_used": MINIMAX_MODEL,
                "metadata": {"kind": "subagent", "run": "c9bbca7c59e7"},
            },
            content_type="application/json",
            HTTP_X_NBHD_INTERNAL_KEY="shared-key",
            HTTP_X_NBHD_TENANT_ID=str(self.tenant.id),
        )

        self.assertEqual(response.status_code, 200)
        record = UsageRecord.objects.get()
        self.assertEqual(record.metadata, {"kind": "subagent", "run": "c9bbca7c59e7"})
        self.assertGreater(record.cost_estimate, 0)
        tenant = Tenant.objects.get(id=self.tenant.id)
        self.assertEqual(tenant.messages_today, before.messages_today)
        self.assertEqual(tenant.messages_this_month, before.messages_this_month)
        self.assertEqual(tenant.tokens_this_month, before.tokens_this_month + 150)
        self.assertGreater(tenant.estimated_cost_this_month, before.estimated_cost_this_month)
