from __future__ import annotations

from datetime import timedelta
from unittest import mock
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.actions import messaging
from apps.actions.messaging import send_gate_confirmation
from apps.actions.models import ActionStatus, ActionType, PendingAction
from apps.router.models import DeviceToken

from .gate import request_datebook_action
from .models import DeviceCommand
from .test_b2a import DatebookB2aMixin, _command_gate_payload
from .tests import _ready_tenant


class DatebookGateConsumerTests(DatebookB2aMixin, TestCase):
    chat_id = 927001

    def setUp(self):
        super().setUp()
        self.consumer = APIClient()
        self.consumer.force_authenticate(user=self.tenant.user)

    def _pending_action(self, *, tenant=None, request_id: str, title: str = "[PERSON_1] planning"):
        tenant = tenant or self.tenant
        return PendingAction.objects.create(
            tenant=tenant,
            action_type=ActionType.CALENDAR_CREATE,
            action_payload=_command_gate_payload(request_id, title=title),
            display_summary=f"Create calendar event: {title}",
            datebook_request_id=request_id,
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    def _requested_action(self, request_id: str):
        with patch("apps.actions.messaging.send_gate_confirmation", return_value=True):
            result = request_datebook_action(
                self.tenant,
                action_type=ActionType.CALENDAR_CREATE,
                request_id=request_id,
                command_payload=_command_gate_payload(request_id),
                display_summary="Create calendar event: [PERSON_1] planning",
                direct_user_originated=False,
            )
        return PendingAction.objects.get(id=result["action_id"])

    def test_pending_list_rehydrates_is_bounded_oldest_first_and_tenant_scoped(self):
        self.tenant.pii_entity_map = {
            "[PERSON_1]": {"name": "Alice"},
            "[ORG_1]": {"name": "Home"},
        }
        self.tenant.save(update_fields=["pii_entity_map"])
        base = timezone.now() - timedelta(minutes=2)
        actions = []
        for index in range(22):
            action = self._pending_action(
                request_id=f"list-{index:02d}",
                title=f"[PERSON_1] planning {index}",
            )
            PendingAction.objects.filter(id=action.id).update(created_at=base + timedelta(seconds=index))
            actions.append(action)

        expired = self._pending_action(request_id="expired")
        PendingAction.objects.filter(id=expired.id).update(expires_at=timezone.now() - timedelta(seconds=1))
        denied = self._pending_action(request_id="denied")
        PendingAction.objects.filter(id=denied.id).update(status=ActionStatus.DENIED)
        PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_DELETE,
            action_payload={"title": "not datebook"},
            display_summary="Not datebook",
        )
        other_tenant = _ready_tenant(927099)
        other = self._pending_action(tenant=other_tenant, request_id="other-tenant")

        response = self.consumer.get("/api/v1/datebook/gate/pending/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.data["review_window_seconds"], 300)
        self.assertEqual(
            [item["action_id"] for item in response.data["actions"]],
            [action.id for action in actions[:20]],
        )
        first = response.data["actions"][0]
        self.assertEqual(first["payload"]["payload"]["items"][0]["title"], "Alice planning 0")
        self.assertEqual(first["payload"]["destination_name"], "Home Calendar")
        self.assertIn("start_at", first["payload"]["payload"]["items"][0]["time"])
        self.assertEqual(first["display_summary"], "Create calendar event: Alice planning 0")
        self.assertNotIn(other.id, [item["action_id"] for item in response.data["actions"]])

    @patch("apps.datebook.notify.notify_device_command")
    def test_respond_approve_uses_shared_seam_and_returns_created_command_id(self, _notify):
        action = self._requested_action("consumer-approve")

        response = self.consumer.post(
            f"/api/v1/datebook/gate/{action.id}/respond/",
            {"response": "approve"},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.data["state"], "approved_queued")
        self.assertEqual(response.data["command_id"], str(action.datebook_command_id))
        self.assertTrue(DeviceCommand.objects.filter(id=response.data["command_id"]).exists())

    def test_respond_deny_expired_and_already_resolved_codes_are_shared_verbatim(self):
        denied = self._requested_action("consumer-deny")
        deny_response = self.consumer.post(
            f"/api/v1/datebook/gate/{denied.id}/respond/",
            {"response": "deny"},
            format="json",
        )
        self.assertEqual(deny_response.status_code, 200)
        self.assertEqual(
            deny_response.data,
            {
                "action_id": denied.id,
                "command_id": str(denied.datebook_command_id),
                "state": "denied",
                "status": "denied",
            },
        )

        conflict = self.consumer.post(
            f"/api/v1/datebook/gate/{denied.id}/respond/",
            {"response": "approve"},
            format="json",
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.data,
            {"error": "Action already resolved: denied", "status": "denied"},
        )

        expired = self._requested_action("consumer-expired")
        PendingAction.objects.filter(id=expired.id).update(expires_at=timezone.now() - timedelta(seconds=1))
        expired_response = self.consumer.post(
            f"/api/v1/datebook/gate/{expired.id}/respond/",
            {"response": "approve"},
            format="json",
        )
        self.assertEqual(expired_response.status_code, 410)
        self.assertEqual(
            expired_response.data,
            {
                "action_id": expired.id,
                "command_id": str(expired.datebook_command_id),
                "state": "expired",
            },
        )
        self.assertEqual(expired_response["Cache-Control"], "no-store")

    def test_respond_is_tenant_scoped(self):
        other_tenant = _ready_tenant(927098)
        other = self._pending_action(tenant=other_tenant, request_id="other-response")

        response = self.consumer.post(
            f"/api/v1/datebook/gate/{other.id}/respond/",
            {"response": "deny"},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, {"error": "Action not found"})
        other.refresh_from_db()
        self.assertEqual(other.status, ActionStatus.PENDING)

        non_datebook = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_DELETE,
            action_payload={"message_id": "same-tenant"},
            display_summary="Delete email",
        )
        response = self.consumer.post(
            f"/api/v1/datebook/gate/{non_datebook.id}/respond/",
            {"response": "approve"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        non_datebook.refresh_from_db()
        self.assertEqual(non_datebook.status, ActionStatus.PENDING)


class DatebookGateAppDeliveryTests(DatebookB2aMixin, TestCase):
    chat_id = 927002

    @patch("apps.router.push_views._push_to_user_devices", return_value={"token_count": 0, "used_fallback": False})
    def test_ios_only_gateway_gets_generic_targeted_idempotent_app_delivery(self, push):
        self.tenant.user.telegram_chat_id = None
        self.tenant.user.line_user_id = None
        self.tenant.user.save(update_fields=["telegram_chat_id", "line_user_id"])
        result = request_datebook_action(
            self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            request_id="app-gate-delivery",
            command_payload=_command_gate_payload("app-gate-delivery", title="PII MUST STAY OFF PUSH"),
            display_summary="PII MUST STAY OFF PUSH",
            direct_user_originated=False,
        )
        action = PendingAction.objects.get(id=result["action_id"])

        self.assertEqual(result["state"], "approval_pending")
        self.assertEqual(action.status, ActionStatus.PENDING)
        self.assertEqual(action.platform_channel, "app")
        self.assertTrue(send_gate_confirmation(self.tenant, action))
        push.assert_called_once()
        kwargs = push.call_args.kwargs
        self.assertEqual(kwargs["installation_id"], "install-a")
        self.assertEqual(kwargs["extra"], {"type": "datebook_gate", "action_id": str(action.id)})
        self.assertEqual(kwargs["collapse_id"], f"datebook-gate:{action.id}")
        self.assertTrue(kwargs["content_available"])
        self.assertNotIn("PII MUST", kwargs["body"])
        self.assertNotIn("approve", kwargs["body"].lower())

    @patch("apps.router.push_views._push_to_user_devices", return_value={"token_count": 1, "used_fallback": False})
    def test_registered_device_is_app_fallback_without_active_gateway(self, push):
        self.tenant.user.telegram_chat_id = None
        self.tenant.user.line_user_id = None
        self.tenant.user.save(update_fields=["telegram_chat_id", "line_user_id"])
        self.gateway.status = self.gateway.Status.RETIRED
        self.gateway.save(update_fields=["status"])
        DeviceToken.objects.create(
            tenant=self.tenant,
            user=self.tenant.user,
            token="a" * 64,
            installation_id="token-install",
        )
        action = self._action_for_priority()

        delivered = send_gate_confirmation(self.tenant, action)

        self.assertTrue(delivered)
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "app")
        push.assert_called_once()
        self.assertIsNone(push.call_args.kwargs["installation_id"])

    def test_telegram_remains_ahead_of_app_for_gateway_tenant(self):
        action = self._action_for_priority()
        telegram = mock.Mock(return_value="telegram-gate-1")
        app = mock.Mock(return_value=f"app-gate-{action.id}")
        with mock.patch.dict(
            messaging._SENDERS,
            {
                "telegram": (telegram, mock.Mock()),
                "app": (app, None),
            },
        ):
            delivered = send_gate_confirmation(self.tenant, action)

        self.assertTrue(delivered)
        telegram.assert_called_once_with(self.tenant, action)
        app.assert_not_called()
        action.refresh_from_db()
        self.assertEqual(action.platform_channel, "telegram")

    def _action_for_priority(self):
        return PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            action_payload=_command_gate_payload("priority"),
            display_summary="Create event",
        )
