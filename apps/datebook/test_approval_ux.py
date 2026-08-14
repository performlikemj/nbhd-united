from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.actions import messaging
from apps.actions.messaging import GateSendResult, _send_line_confirmation, _send_telegram_confirmation
from apps.actions.models import ActionAuditLog, ActionStatus, ActionType, PendingAction
from apps.actions.views import GateRespondView
from apps.pii.store_registry import registered_store
from apps.router.models import DeviceToken
from apps.tenants.models import Tenant

from .gate import DATEBOOK_GATE_REVIEW_WINDOW, request_datebook_action
from .models import DatebookDestinationDefault, DeviceCommand
from .notify import notify_datebook_gate_changed
from .services import ProtocolError, disable_datebook, register_gateway
from .test_b2a import DatebookB2aMixin, _command_gate_payload, _event_payload


class DatebookApprovalUXTests(DatebookB2aMixin, TestCase):
    chat_id = 928001

    def setUp(self):
        super().setUp()
        self.consumer = APIClient()
        self.consumer.force_authenticate(user=self.tenant.user)

    def _request(self, request_id: str, **overrides) -> PendingAction:
        command_payload = _command_gate_payload(request_id)
        command_payload.update(overrides.pop("command_payload", {}))
        with patch("apps.actions.messaging.send_gate_confirmation", return_value=True):
            result = request_datebook_action(
                self.tenant,
                action_type=overrides.pop("action_type", ActionType.CALENDAR_CREATE),
                request_id=request_id,
                command_payload=command_payload,
                display_summary="Create calendar event: planning",
                direct_user_originated=overrides.pop("direct_user_originated", True),
                originating_channel=overrides.pop("originating_channel", "app"),
                **overrides,
            )
        self.assertEqual(result["state"], "approval_pending")
        return PendingAction.objects.get(pk=result["action_id"])

    def test_datebook_uses_24_hours_while_generic_actions_keep_five_minutes(self):
        before = timezone.now()
        action = self._request("window-datebook")
        generic = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_DELETE,
            action_payload={"message_id": "generic"},
            display_summary="Delete email",
        )
        after = timezone.now()

        self.assertGreaterEqual(action.expires_at, before + DATEBOOK_GATE_REVIEW_WINDOW)
        self.assertLessEqual(action.expires_at, after + DATEBOOK_GATE_REVIEW_WINDOW)
        self.assertGreaterEqual(generic.expires_at, before + timedelta(minutes=5))
        self.assertLessEqual(generic.expires_at, after + timedelta(minutes=5))

    @patch("apps.datebook.notify.dispatch_datebook_gate_changed")
    def test_idempotent_retry_after_review_window_returns_typed_stale_outcome(self, changed):
        action = self._request("retry-stale")
        PendingAction.objects.filter(pk=action.pk).update(expires_at=timezone.now() - timedelta(seconds=1))

        with self.captureOnCommitCallbacks(execute=True):
            result = request_datebook_action(
                self.tenant,
                action_type=ActionType.CALENDAR_CREATE,
                request_id="retry-stale",
                command_payload=_command_gate_payload("retry-stale"),
                display_summary="Create event",
                direct_user_originated=True,
            )

        self.assertEqual(result["state"], "stale_review")
        self.assertEqual(result["message"], "The 24-hour review window expired. Nothing was queued or created.")
        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.EXPIRED)
        changed.assert_called_once_with(self.tenant.id)

    def test_resolution_order_and_stale_default_never_falls_back_by_name(self):
        fingerprint = "a" * 64
        default = DatebookDestinationDefault.objects.create(
            tenant=self.tenant,
            entity_type=DatebookDestinationDefault.EntityType.CALENDAR,
            name="Work",
            fingerprint=fingerprint,
            target_installation_id=self.gateway.installation_id,
            gateway_epoch=self.gateway.gateway_epoch,
        )
        from_default = self._request(
            "resolution-default",
            command_payload={"destination_name": "", "destination_fingerprint": ""},
        )
        self.assertEqual(from_default.action_payload["destination_kind"], "tenant_default")
        self.assertEqual(from_default.action_payload["destination_name"], "Work")
        self.assertEqual(from_default.action_payload["destination_fingerprint"], fingerprint)

        explicit = self._request(
            "resolution-explicit",
            command_payload={
                **_command_gate_payload("resolution-explicit", title="Explicit planning"),
                "destination_name": "Personal",
                "destination_fingerprint": "",
            },
        )
        self.assertEqual(explicit.action_payload["destination_kind"], "explicit")
        self.assertEqual(explicit.action_payload["destination_name"], "Personal")

        default.gateway_epoch += 1
        default.save(update_fields=["gateway_epoch"])
        stale = self._request(
            "resolution-stale",
            command_payload={
                **_command_gate_payload("resolution-stale", title="Stale planning"),
                "destination_name": "",
                "destination_fingerprint": "",
            },
        )
        self.assertEqual(stale.action_payload["destination_kind"], "device_default")
        self.assertEqual(stale.action_payload["destination_name"], "")
        self.assertEqual(stale.action_payload["destination_fingerprint"], "")
        self.assertFalse(DatebookDestinationDefault.objects.filter(pk=default.pk).exists())

    def test_conflicting_outer_and_item_destination_is_typed_creation_error(self):
        payload = _event_payload()
        payload["items"][0]["calendar_title"] = "Personal"
        response = self.client.post(
            f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
            {
                "request_id": "conflicting-destination",
                "command_type": "calendar_create",
                "payload": payload,
                "destination_name": "Work",
                "direct_user_originated": True,
            },
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data, {"state": "conflicting_destination"})
        self.assertFalse(PendingAction.objects.filter(datebook_request_id="conflicting-destination").exists())

    def test_override_without_set_default_changes_command_only(self):
        action = self._request("override-no-default")
        fingerprint = "b" * 64
        response = self.consumer.post(
            f"/api/v1/datebook/gate/{action.id}/respond/",
            {
                "response": "approve",
                "destination_override": {"name": "Personal", "fingerprint": fingerprint},
                "set_default": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        command = DeviceCommand.objects.get(id=action.datebook_command_id)
        self.assertEqual(command.destination_name, "Personal")
        self.assertEqual(command.destination_fingerprint, fingerprint)
        self.assertFalse(DatebookDestinationDefault.objects.filter(tenant=self.tenant).exists())

    def test_override_set_default_authors_owner_pii_and_complete_audit_fingerprints(self):
        old_fingerprint = "c" * 64
        requested_fingerprint = "d" * 64
        approved_fingerprint = "e" * 64
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        DatebookDestinationDefault.objects.create(
            tenant=self.tenant,
            entity_type=DatebookDestinationDefault.EntityType.CALENDAR,
            name="Alice Old",
            fingerprint=old_fingerprint,
            target_installation_id=self.gateway.installation_id,
            gateway_epoch=self.gateway.gateway_epoch,
        )
        with (
            patch("apps.pii.redactor._detect_pii", return_value=[]),
            patch("apps.pii.authoring._detect_pii", return_value=[]),
        ):
            action = self._request(
                "override-set-default",
                command_payload={
                    "destination_name": "Alice Requested",
                    "destination_fingerprint": requested_fingerprint,
                },
            )
            self.assertEqual(action.pii_receipts["action_payload"]["writer"], "runtime")
            response = self.consumer.post(
                f"/api/v1/datebook/gate/{action.id}/respond/",
                {
                    "response": "approve",
                    "destination_override": {"name": "Alice Personal", "fingerprint": approved_fingerprint},
                    "set_default": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        default = DatebookDestinationDefault.objects.get(tenant=self.tenant)
        action.refresh_from_db()
        self.assertEqual(default.name, "[PERSON_1] Personal")
        self.assertEqual(default.fingerprint, approved_fingerprint)
        self.assertEqual(default.pii_receipts["name"]["writer"], "owner")
        self.assertEqual(action.action_payload["requested_destination"]["fingerprint"], requested_fingerprint)
        self.assertEqual(action.action_payload["approved_destination"]["fingerprint"], approved_fingerprint)
        self.assertEqual(action.pii_receipts["action_payload"]["writer"], "owner")
        audits = list(ActionAuditLog.objects.filter(datebook_command_id=action.datebook_command_id).order_by("id"))
        self.assertEqual(len(audits), 2)
        for audit in audits:
            self.assertEqual(audit.requested_destination_fingerprint, requested_fingerprint)
            self.assertEqual(audit.approved_destination_fingerprint, approved_fingerprint)
        self.assertEqual(audits[-1].default_destination_old_fingerprint, old_fingerprint)
        self.assertEqual(audits[-1].default_destination_new_fingerprint, approved_fingerprint)
        store = registered_store("datebook.DatebookDestinationDefault")
        self.assertEqual(store.flat_fields, ("name",))

    def test_protocol_error_commits_approved_failed_without_default(self):
        action = self._request("override-command-fails")
        with patch(
            "apps.datebook.services.create_device_command",
            side_effect=ProtocolError("daily_command_cap", 429),
        ):
            response = self.consumer.post(
                f"/api/v1/datebook/gate/{action.id}/respond/",
                {
                    "response": "approve",
                    "destination_override": {"name": "Work", "fingerprint": "f" * 64},
                    "set_default": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["state"], "daily_command_cap")
        action.refresh_from_db()
        self.assertEqual(action.status, ActionStatus.APPROVED)
        self.assertEqual(action.resolution_code, "daily_command_cap")
        self.assertFalse(DeviceCommand.objects.filter(id=action.datebook_command_id).exists())
        self.assertFalse(DatebookDestinationDefault.objects.filter(tenant=self.tenant).exists())
        self.assertEqual(
            list(
                ActionAuditLog.objects.filter(datebook_command_id=action.datebook_command_id)
                .order_by("created_at")
                .values_list("result", flat=True)
            ),
            ["approved", "failed"],
        )

    def test_takeover_and_disable_invalidate_defaults(self):
        def create_default(fingerprint: str):
            return DatebookDestinationDefault.objects.create(
                tenant=self.tenant,
                entity_type=DatebookDestinationDefault.EntityType.CALENDAR,
                name="Work",
                fingerprint=fingerprint,
                target_installation_id=self.gateway.installation_id,
                gateway_epoch=self.gateway.gateway_epoch,
            )

        create_default("1" * 64)
        register_gateway(
            self.tenant,
            installation_id="install-b",
            takeover=True,
            events_consent=True,
            reminders_consent=True,
        )
        self.assertFalse(DatebookDestinationDefault.objects.filter(tenant=self.tenant).exists())

        self.gateway.refresh_from_db()
        active = self.tenant.datebook_gateways.get(status="active")
        DatebookDestinationDefault.objects.create(
            tenant=self.tenant,
            entity_type=DatebookDestinationDefault.EntityType.CALENDAR,
            name="Home",
            fingerprint="2" * 64,
            target_installation_id=active.installation_id,
            gateway_epoch=active.gateway_epoch,
        )
        disable_datebook(self.tenant, purge=False)
        self.assertFalse(DatebookDestinationDefault.objects.filter(tenant=self.tenant).exists())

    def test_pending_origin_is_immutable_and_separate_from_delivery_channel(self):
        with patch.dict(
            messaging._SENDERS,
            {"telegram": (lambda _tenant, _action: GateSendResult(True, "tg-1"), None)},
        ):
            result = request_datebook_action(
                self.tenant,
                action_type=ActionType.CALENDAR_CREATE,
                request_id="immutable-origin",
                command_payload=_command_gate_payload("immutable-origin"),
                display_summary="Create event",
                direct_user_originated=True,
                originating_channel="telegram",
            )
        action = PendingAction.objects.get(pk=result["action_id"])
        PendingAction.objects.filter(pk=action.pk).update(platform_channel="app")

        response = self.consumer.get("/api/v1/datebook/gate/pending/")
        item = next(item for item in response.data["actions"] if item["action_id"] == action.id)
        self.assertEqual(item["originating_channel"], "telegram")
        self.assertIn("created_at", item)


@override_settings(TELEGRAM_BOT_TOKEN="test-token", LINE_CHANNEL_ACCESS_TOKEN="line-token")
class DatebookDeliveryTruthTests(DatebookB2aMixin, TestCase):
    chat_id = 928002

    def test_telegram_and_line_copy_are_24h_while_generic_copy_stays_five_minutes(self):
        generic = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.GMAIL_DELETE,
            action_payload={},
            display_summary="Delete email",
        )
        datebook = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            action_payload={},
            display_summary="Create event",
        )
        telegram_response = SimpleNamespace(
            status_code=200,
            json=lambda: {"result": {"message_id": 41}},
            text="",
        )
        with patch("httpx.post", return_value=telegram_response) as post:
            _send_telegram_confirmation(self.tenant, generic)
            generic_text = post.call_args.kwargs["json"]["text"]
            _send_telegram_confirmation(self.tenant, datebook)
            datebook_text = post.call_args.kwargs["json"]["text"]
        self.assertIn("Expires in 5 minutes", generic_text)
        self.assertIn("Review within 24 hours", datebook_text)

        self.tenant.user.line_user_id = "U" + "9" * 32
        self.tenant.user.save(update_fields=["line_user_id"])
        line_response = SimpleNamespace(status_code=200, json=lambda: {}, text="")
        with patch("httpx.post", return_value=line_response) as post:
            _send_line_confirmation(self.tenant, datebook)
        contents = post.call_args.kwargs["json"]["messages"][0]["contents"]["body"]["contents"]
        self.assertIn("Review within 24 hours", [item["text"] for item in contents])

    def test_no_real_platform_id_never_produces_sent_delivery_state(self):
        with patch.dict(
            messaging._SENDERS,
            {"telegram": (lambda _tenant, _action: GateSendResult(True), None)},
        ):
            accepted = request_datebook_action(
                self.tenant,
                action_type=ActionType.CALENDAR_CREATE,
                request_id="telegram-no-id",
                command_payload=_command_gate_payload("telegram-no-id"),
                display_summary="Create event",
                direct_user_originated=True,
                originating_channel="telegram",
            )
        self.assertEqual(accepted["approval_surface"], "telegram")
        self.assertEqual(accepted["delivery_state"], "accepted")
        self.assertNotIn("I sent", accepted["guidance"])
        action = PendingAction.objects.get(pk=accepted["action_id"])
        self.assertEqual(action.platform_message_id, "")

        with patch.dict(
            messaging._SENDERS,
            {"telegram": (lambda _tenant, _action: GateSendResult(True, "real-42"), None)},
        ):
            sent = request_datebook_action(
                self.tenant,
                action_type=ActionType.CALENDAR_CREATE,
                request_id="telegram-real-id",
                command_payload=_command_gate_payload("telegram-real-id", title="Real id event"),
                display_summary="Create event",
                direct_user_originated=True,
                originating_channel="telegram",
            )
        self.assertEqual(sent["delivery_state"], "sent")
        self.assertIn("I sent the approval to Telegram", sent["guidance"])

    def test_legacy_synthetic_line_marker_is_accepted_never_sent(self):
        action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.REMINDER_CREATE,
            action_payload=_command_gate_payload("legacy-line-marker"),
            display_summary="Create reminder",
            datebook_request_id="legacy-line-marker",
            datebook_command_id="00000000-0000-4000-8000-000000000042",
            originating_channel="line",
            platform_channel="line",
            platform_message_id="line-push-42",
            expires_at=timezone.now() + timedelta(hours=24),
        )

        from .gate import datebook_action_state

        state = datebook_action_state(action)
        self.assertEqual(state["delivery_state"], "accepted")
        self.assertNotIn("I sent", state["guidance"])


class DatebookGateChangedTests(DatebookB2aMixin, TestCase):
    chat_id = 928003

    @patch("apps.datebook.notify.dispatch_datebook_gate_changed")
    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    def test_creation_emits_on_commit_for_every_origin(self, _send, changed):
        for origin in ("app", "telegram", "line"):
            with self.captureOnCommitCallbacks(execute=True):
                request_datebook_action(
                    self.tenant,
                    action_type=ActionType.CALENDAR_CREATE,
                    request_id=f"create-{origin}",
                    command_payload=_command_gate_payload(f"create-{origin}", title=f"Create {origin}"),
                    display_summary="Create event",
                    direct_user_originated=True,
                    originating_channel=origin,
                )
        self.assertEqual(changed.call_count, 3)
        self.assertEqual([call.args for call in changed.call_args_list], [(self.tenant.id,)] * 3)

    @patch("apps.datebook.notify.dispatch_datebook_gate_changed")
    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    def test_every_terminal_transition_emits_on_commit(self, _send, changed):
        approve = request_datebook_action(
            self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            request_id="terminal-approve",
            command_payload=_command_gate_payload("terminal-approve", title="Approve event"),
            display_summary="Create event",
            direct_user_originated=True,
        )
        deny = request_datebook_action(
            self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            request_id="terminal-deny",
            command_payload=_command_gate_payload("terminal-deny", title="Deny event"),
            display_summary="Create event",
            direct_user_originated=True,
        )
        expired = request_datebook_action(
            self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            request_id="terminal-expire",
            command_payload=_command_gate_payload("terminal-expire", title="Expire event"),
            display_summary="Create event",
            direct_user_originated=True,
        )
        changed.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            GateRespondView.resolve_action(
                action_id=approve["action_id"], response_action="approve", tenant=self.tenant
            )
        with self.captureOnCommitCallbacks(execute=True):
            GateRespondView.resolve_action(action_id=deny["action_id"], response_action="deny", tenant=self.tenant)
        PendingAction.objects.filter(pk=expired["action_id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.captureOnCommitCallbacks(execute=True):
            _action, data, response_status = GateRespondView.resolve_action(
                action_id=expired["action_id"],
                response_action="approve",
                tenant=self.tenant,
            )
        self.assertEqual(response_status, 410)
        self.assertEqual(data["state"], "stale_review")
        self.assertEqual(changed.call_count, 3)

    @patch("apps.datebook.notify.dispatch_datebook_gate_changed")
    @patch("apps.actions.messaging.update_gate_message")
    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    def test_expiry_sweep_records_typed_stale_review_and_emits_on_commit(self, _send, _edit, changed):
        from apps.actions.tasks import expire_stale_pending_actions

        result = request_datebook_action(
            self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            request_id="sweep-expire",
            command_payload=_command_gate_payload("sweep-expire"),
            display_summary="Create event",
            direct_user_originated=True,
        )
        PendingAction.objects.filter(pk=result["action_id"]).update(expires_at=timezone.now() - timedelta(seconds=1))

        with self.captureOnCommitCallbacks(execute=True):
            summary = expire_stale_pending_actions()

        action = PendingAction.objects.get(pk=result["action_id"])
        self.assertEqual(summary, "Expired 1 actions")
        self.assertEqual(action.status, ActionStatus.EXPIRED)
        self.assertEqual(action.resolution_code, "stale_review")
        self.assertTrue(
            ActionAuditLog.objects.filter(
                tenant=self.tenant,
                result=ActionStatus.EXPIRED,
                detail_code="stale_review",
            ).exists()
        )
        changed.assert_called_once_with(self.tenant.id)

    @patch("apps.datebook.notify.apns_configured", return_value=True)
    @patch("apps.router.push_views._push_to_user_devices", return_value={"token_count": 1, "used_fallback": False})
    def test_invalidation_payload_is_pii_free_and_never_falls_back(self, push, _configured):
        DeviceToken.objects.create(
            tenant=self.tenant,
            user=self.tenant.user,
            token="9" * 64,
            installation_id=self.gateway.installation_id,
        )
        notify_datebook_gate_changed(self.tenant.id)

        kwargs = push.call_args.kwargs
        self.assertEqual(kwargs["extra"], {"type": "datebook_gate_changed"})
        self.assertEqual(kwargs["installation_id"], self.gateway.installation_id)
        self.assertFalse(kwargs["fallback_to_all"])
        self.assertNotIn("title", kwargs["extra"])
        self.assertNotIn("notes", kwargs["extra"])


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class DatebookApprovalRaceTests(DatebookB2aMixin, TransactionTestCase):
    chat_id = 928004
    reset_sequences = True

    def test_concurrent_approval_creates_one_command_one_default_and_typed_loser(self):
        from .gate import _persist_destination_default
        from .services import create_device_command

        with patch("apps.actions.messaging.send_gate_confirmation", return_value=True):
            result = request_datebook_action(
                self.tenant,
                action_type=ActionType.CALENDAR_CREATE,
                request_id="approval-race",
                command_payload=_command_gate_payload("approval-race"),
                display_summary="Create event",
                direct_user_originated=True,
            )
        barrier = threading.Barrier(2)

        def approve():
            close_old_connections()
            try:
                tenant = Tenant.objects.get(pk=self.tenant.pk)
                barrier.wait(timeout=5)
                _action, data, response_status = GateRespondView.resolve_action(
                    action_id=result["action_id"],
                    response_action="approve",
                    tenant=tenant,
                    destination_override={"name": "Work", "fingerprint": "9" * 64},
                    set_default=True,
                )
                return response_status, data
            finally:
                close_old_connections()

        with (
            patch("apps.datebook.services.create_device_command", wraps=create_device_command) as create_command,
            patch(
                "apps.datebook.gate._persist_destination_default",
                wraps=_persist_destination_default,
            ) as persist_default,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            outcomes = list(pool.map(lambda _index: approve(), range(2)))

        self.assertEqual(sorted(status_code for status_code, _data in outcomes), [200, 409])
        loser = next(data for status_code, data in outcomes if status_code == 409)
        self.assertEqual(loser["error"], "action_already_resolved")
        self.assertEqual(DeviceCommand.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(DatebookDestinationDefault.objects.filter(tenant=self.tenant).count(), 1)
        self.assertEqual(create_command.call_count, 1)
        self.assertEqual(persist_default.call_count, 1)
