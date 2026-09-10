"""Recovery kwargs survive publishing, QStash dispatch, and the task wrapper."""

import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.cron.publish import publish_task
from apps.orchestrator.cron_reconcile import _schedule_remove_recovery
from apps.tenants.services import create_tenant


@override_settings(QSTASH_TOKEN="test-token", API_BASE_URL="https://example.test")
class RegenerateCronTaskTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="Recovery Dispatch", telegram_chat_id=864209755)
        self.tenant.container_fqdn = "oc-recovery.internal"
        self.tenant.save()
        self.qstash = self.enterContext(patch("apps.cron.publish._get_qstash_client")).return_value
        self.regenerate = self.enterContext(patch("apps.orchestrator.cron_reconcile.regenerate_tenant_crons"))
        self.regenerate.return_value = {"errors": 1}
        self.enterContext(patch("apps.cron.views.verify_qstash_signature", return_value=True))

    def _deliver(self, body):
        response = self.client.post(
            "/api/v1/cron/trigger/regenerate_tenant_crons/",
            data=json.dumps(body),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_published_recovery_reaches_reconciler_through_dispatch_and_wrapper(self):
        _schedule_remove_recovery(self.tenant)
        self.qstash.message.publish_json.assert_called_once_with(
            url="https://example.test/api/cron/trigger/regenerate_tenant_crons/",
            body={"args": [str(self.tenant.id)], "kwargs": {"recovery": True}},
            retries=0,
            delay="30s",
            deduplication_id=f"regen-cron-recovery-{self.tenant.id}",
        )
        self._deliver(self.qstash.message.publish_json.call_args.kwargs["body"])
        self.regenerate.assert_called_once_with(self.tenant, recovery=True)

    def test_markerless_published_payload_defaults_to_normal_pass_and_three_retries(self):
        publish_task("regenerate_tenant_crons", str(self.tenant.id))
        message = self.qstash.message.publish_json.call_args.kwargs
        self.assertEqual(message["body"], {"args": [str(self.tenant.id)], "kwargs": {}})
        self.assertEqual(message["retries"], 3)
        self._deliver(message["body"])
        self.regenerate.assert_called_once_with(self.tenant, recovery=False)

    def test_queued_payload_without_kwargs_defaults_to_normal_pass(self):
        self._deliver({"args": [str(self.tenant.id)]})
        self.regenerate.assert_called_once_with(self.tenant, recovery=False)
