"""P3 W3b real gate/audit seams for action payloads and summaries."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenants.services import create_tenant
from apps.tenants.test_utils import seed_internal_key

from .messaging import _send_telegram_confirmation
from .models import ActionAuditLog, PendingAction


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


@override_settings(
    NBHD_INTERNAL_API_KEY="test-internal-key",
    DEPLOY_SECRET="test-deploy-secret",
    TELEGRAM_BOT_TOKEN="test-bot-token",
)
class ActionLongTailPlaceholderTests(TestCase):
    def setUp(self):
        self.tenant = create_tenant(display_name="W3b Actions", telegram_chat_id=880314)
        self.tenant.model_tier = "pro"
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["model_tier", "pii_entity_map"])
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_INTERNAL_KEY": "test-internal-key",
            "HTTP_X_TENANT_ID": str(self.tenant.id),
        }

    def _url(self):
        return f"/api/v1/internal/runtime/{self.tenant.id}/gate/request/"

    @staticmethod
    def _payload():
        return {
            "action_type": "gmail_send",
            "payload": {"recipient": "Alice", "subject": "Meet Alice"},
            "display_summary": "Send the update to Alice",
        }

    def _enable_placeholder_writes(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])

    def test_flag_off_autoapproval_preserves_payload_bytes(self):
        self.tenant.gate_all_actions = False
        self.tenant.gate_acknowledged_risk = True
        self.tenant.save(update_fields=["gate_all_actions", "gate_acknowledged_risk"])

        response = self.client.post(self._url(), self._payload(), format="json", **self.headers)

        self.assertEqual(response.status_code, 200, response.data)
        audit = ActionAuditLog.objects.get(tenant=self.tenant)
        self.assertEqual(audit.action_payload, self._payload()["payload"])
        self.assertEqual(audit.display_summary, self._payload()["display_summary"])
        self.assertEqual(audit.pii_receipts["action_payload"], {"state": "bypass", "writer": "runtime"})
        self.assertNotIn("pii_receipts", response.data)

    def test_flag_on_pending_and_terminal_audit_stay_placeholder_space(self):
        self._enable_placeholder_writes()
        with (
            _checked_detection(),
            patch("apps.actions.messaging.send_gate_confirmation", return_value=True),
        ):
            response = self.client.post(self._url(), self._payload(), format="json", **self.headers)

        self.assertEqual(response.status_code, 202, response.data)
        action = PendingAction.objects.get(id=response.data["action_id"])
        self.assertEqual(action.action_payload, {"recipient": "[PERSON_1]", "subject": "Meet [PERSON_1]"})
        self.assertEqual(action.display_summary, "Send the update to [PERSON_1]")
        self.assertEqual(action.pii_receipts["action_payload"]["writer"], "runtime")
        self.assertNotIn("pii_receipts", response.data)

        with patch("apps.actions.messaging.update_gate_message"):
            resolved = self.client.post(
                f"/api/v1/gate/{action.id}/respond/",
                {"action": "approve"},
                format="json",
                HTTP_X_DEPLOY_SECRET="test-deploy-secret",
            )
        self.assertEqual(resolved.status_code, 200)
        audit = ActionAuditLog.objects.get(tenant=self.tenant)
        self.assertEqual(audit.action_payload, action.action_payload)
        self.assertEqual(audit.display_summary, action.display_summary)
        self.assertEqual(audit.pii_receipts, action.pii_receipts)

        polled = self.client.get(
            f"/api/v1/internal/runtime/{self.tenant.id}/gate/{action.id}/poll/",
            **self.headers,
        )
        self.assertEqual(polled.data, {"action_id": action.id, "status": "approved"})

    def test_confirmation_transport_rehydrates_summary_for_owner(self):
        self._enable_placeholder_writes()
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type="gmail_send",
            action_payload={"recipient": "[PERSON_1]"},
            display_summary="Send the update to [PERSON_1]",
            pii_receipts={
                "display_summary": {
                    "state": "placeholder",
                    "writer": "runtime",
                    "redactions": [{"placeholder": "[PERSON_1]"}],
                }
            },
        )
        sent = SimpleNamespace(status_code=200, json=lambda: {"result": {"message_id": 41}}, text="")
        with patch("httpx.post", return_value=sent) as http_post:
            message_id = _send_telegram_confirmation(self.tenant, action)

        self.assertEqual(message_id, "41")
        outbound = http_post.call_args.kwargs["json"]["text"]
        self.assertIn("Alice", outbound)
        self.assertNotIn("[PERSON_1]", outbound)
