from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.actions.models import (
    ActionAuditLog,
    ActionAuditOutcome,
    ActionStatus,
    ActionType,
    GatePreference,
    PendingAction,
)
from apps.actions.views import GateRespondView
from apps.orchestrator.envelope_registry import all_sections, suppress_refresh
from apps.orchestrator.workspace_envelope import render_managed_region
from apps.router.line_webhook import LineWebhookView
from apps.router.models import DeviceToken
from apps.router.poller import TelegramPoller
from apps.router.views import telegram_webhook

from .envelope import render_datebook
from .gate import UNDELIVERABLE_MESSAGE, request_datebook_action
from .models import DatebookGateway, DeviceCommand, MirrorEvent, MirrorReminder
from .notify import notify_device_command
from .runtime_views import _DatebookResponseGuard
from .services import create_device_command
from .tests import _ready_tenant, _source_key


def _event_payload(*, title="[PERSON_1] planning"):
    start = timezone.now() + timedelta(days=1)
    return {
        "items": [
            {
                "title": title,
                "time": {
                    "kind": "zoned",
                    "start_at": start.isoformat(),
                    "end_at": (start + timedelta(hours=1)).isoformat(),
                    "tz_id": "UTC",
                },
            }
        ]
    }


def _command_gate_payload(request_id="runtime-request", *, title="[PERSON_1] planning"):
    payload = _event_payload(title=title)
    return {
        "request_id": request_id,
        "command_type": DeviceCommand.CommandType.CALENDAR_CREATE,
        "payload": payload,
        "display_text": f"Create calendar event: {title}",
        "destination_name": "[ORG_1] Calendar",
        "destination_fingerprint": "",
        "target_at": payload["items"][0]["time"]["start_at"],
    }


class DatebookB2aMixin:
    chat_id = 926000

    def setUp(self):
        super().setUp()
        self.tenant = _ready_tenant(self.chat_id)
        self.tenant.internal_api_key = "datebook-runtime-key"
        self.tenant.save(update_fields=["internal_api_key"])
        self.gateway = DatebookGateway.objects.create(
            tenant=self.tenant,
            installation_id="install-a",
            status=DatebookGateway.Status.ACTIVE,
            events_authorization="full_access",
            reminders_authorization="full_access",
            events_last_complete_sync_at=timezone.now() - timedelta(hours=2),
            reminders_last_complete_sync_at=timezone.now() - timedelta(hours=3),
        )
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "datebook-runtime-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }


class RuntimeSurfaceTests(DatebookB2aMixin, TestCase):
    chat_id = 926001

    def test_agenda_uses_standard_auth_is_bounded_and_never_cached(self):
        with suppress_refresh():
            MirrorEvent.objects.create(
                tenant=self.tenant,
                source_key=_source_key("b2a-agenda"),
                content_hash="a" * 64,
                active=True,
                first_seen_generation=1,
                last_seen_generation=1,
                time_kind="zoned",
                zoned_start_at=timezone.now() + timedelta(hours=1),
                zoned_end_at=timezone.now() + timedelta(hours=2),
                tz_id="UTC",
                title="[PERSON_1] planning",
                location="[PLACE_1]",
                notes="[PERSON_2] notes",
                calendar_title="[ORG_1] Calendar",
                source_title="[ORG_1]",
                authorization_status="full_access",
            )
        path = f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/agenda"
        denied = self.client.get(path)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied["Cache-Control"], "no-store")
        response = self.client.get(path, {"days_ahead": 61}, **self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.get(path, {"entity": "events"}, **self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertFalse(response.data["truncated"])
        self.assertEqual(response.data["items"][0]["title"], "[PERSON_1] planning")
        self.assertEqual(response.data["scopes"]["events"]["authorization"], "full_access")
        self.assertIn("T", response.data["scopes"]["events"]["last_complete_sync_at"])

    def test_response_guard_declares_every_datebook_content_field(self):
        expected = {
            "title",
            "location",
            "notes",
            "calendar_title",
            "list_title",
            "source_title",
            "display_text",
        }
        self.assertEqual(_DatebookResponseGuard.pii_egress_text_fields, expected)

    def test_runtime_and_envelope_still_require_manifest_readiness(self):
        self.tenant.datebook_manifest_ok = False
        self.tenant.save(update_fields=["datebook_manifest_ok"])
        response = self.client.get(
            f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/agenda",
            **self.headers,
        )
        section = next(section for section in all_sections() if section.key == "datebook")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data, {"state": "datebook_disabled"})
        self.assertFalse(section.enabled(self.tenant))
        self.assertNotIn("## Calendar & Reminders", render_managed_region(self.tenant))

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=False)
    def test_request_create_is_strict_and_returns_store_safe_undeliverable(self, _send):
        path = f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create"
        invalid_payload = _event_payload()
        invalid_payload["items"][0]["attendees"] = ["attacker@example.com"]
        rejected = self.client.post(
            path,
            {
                "request_id": "strict-invalid",
                "command_type": "calendar_create",
                "payload": invalid_payload,
                "direct_user_originated": True,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.data["state"], "unsupported_command_field")

        response = self.client.post(
            path,
            {
                "request_id": "ios-only-request",
                "command_type": "calendar_create",
                "payload": _event_payload(),
                "direct_user_originated": True,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.data["state"], "undeliverable")
        self.assertEqual(response.data["message"], UNDELIVERABLE_MESSAGE)
        action = PendingAction.objects.get(datebook_request_id="ios-only-request")
        self.assertEqual(action.status, ActionStatus.EXPIRED)
        self.assertNotIn("app store", response.data["message"].lower())

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    def test_status_poll_tracks_reserved_command_before_approval(self, _send):
        path = f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create"
        created = self.client.post(
            path,
            {
                "request_id": "poll-reserved",
                "command_type": "calendar_create",
                "payload": _event_payload(),
                "direct_user_originated": True,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(created.status_code, 202, created.data)
        status_response = self.client.get(
            f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/command-status/{created.data['command_id']}",
            **self.headers,
        )
        self.assertEqual(status_response.data["state"], "approval_pending")
        self.assertEqual(status_response.data["datebook_command_generation"], 0)

    @patch("apps.datebook.notify.notify_device_command")
    def test_auto_approved_claim_rehydrates_full_typed_payload_and_generation(self, _notify):
        GatePreference.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            require_confirmation=False,
        )
        payload = _event_payload(title="Alice planning")
        payload["items"][0]["alarm"] = {"kind": "relative", "offset_seconds": -900}
        with self.captureOnCommitCallbacks(execute=True):
            created = self.client.post(
                f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
                {
                    "request_id": "full-consumer-payload",
                    "command_type": "calendar_create",
                    "payload": payload,
                    "destination_name": "Alice Calendar",
                    "direct_user_originated": True,
                },
                format="json",
                **self.headers,
            )
        self.assertEqual(created.data["state"], "approved_queued")
        consumer = APIClient()
        consumer.force_authenticate(user=self.tenant.user)
        claimed = consumer.post(
            "/api/v1/datebook/commands/claim/",
            {"installation_id": "install-a", "gateway_epoch": self.gateway.gateway_epoch},
            format="json",
        )
        self.assertEqual(claimed.status_code, 200, claimed.data)
        command = claimed.data["command"]
        self.assertEqual(command["payload"]["items"][0]["title"], "Alice planning")
        self.assertEqual(command["payload"]["items"][0]["time"]["kind"], "zoned")
        self.assertEqual(command["payload"]["items"][0]["alarm"]["offset_seconds"], -900)
        self.assertEqual(command["destination_name"], "Alice Calendar")
        self.assertGreater(command["datebook_command_generation"], 0)
        self.assertEqual(
            command["datebook_command_generation"],
            claimed.data["datebook_command_generation"],
        )


class ReviewGateAndAuditTests(DatebookB2aMixin, TestCase):
    chat_id = 926002

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    @patch("apps.datebook.notify.notify_device_command")
    def test_auto_approval_requires_direct_origin_and_records_immutable_transitions(self, notify, _send):
        GatePreference.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            require_confirmation=False,
        )
        with self.captureOnCommitCallbacks(execute=True):
            result = request_datebook_action(
                self.tenant,
                action_type=ActionType.CALENDAR_CREATE,
                request_id="direct-auto",
                command_payload=_command_gate_payload("direct-auto"),
                display_summary="Create calendar event: [PERSON_1] planning",
                direct_user_originated=True,
            )
        self.assertEqual(result["state"], "approved_queued")
        command = DeviceCommand.objects.get(id=result["command_id"])
        self.assertEqual(command.state, DeviceCommand.State.PENDING)
        notify.assert_called_once()
        self.assertEqual(
            list(
                ActionAuditLog.objects.filter(datebook_command_id=command.id)
                .order_by("id")
                .values_list("result", flat=True)
            ),
            [ActionAuditOutcome.APPROVED, ActionAuditOutcome.QUEUED],
        )

        pending = request_datebook_action(
            self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            request_id="background-stays-gated",
            command_payload=_command_gate_payload("background-stays-gated"),
            display_summary="Create calendar event: [PERSON_1] planning",
            direct_user_originated=False,
        )
        self.assertEqual(pending["state"], "approval_pending")

    @patch("apps.actions.messaging.send_gate_confirmation", return_value=True)
    @patch("apps.datebook.notify.notify_device_command")
    def test_shared_resolver_approves_then_command_result_adds_terminal_audit(self, _notify, _send):
        result = request_datebook_action(
            self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            request_id="manual-approve",
            command_payload=_command_gate_payload("manual-approve"),
            display_summary="Create calendar event: [PERSON_1] planning",
            direct_user_originated=True,
        )
        action = PendingAction.objects.get(id=result["action_id"])
        resolved, data, response_status = GateRespondView.resolve_action(
            action_id=action.id,
            response_action="approve",
            tenant=self.tenant,
        )
        self.assertEqual(response_status, 200)
        self.assertEqual(data["state"], "approved_queued")
        self.assertEqual(resolved.status, ActionStatus.APPROVED)

        command = DeviceCommand.objects.get(id=action.datebook_command_id)
        from apps.actions.services import record_datebook_command_transition

        record_datebook_command_transition(command, ActionAuditOutcome.EXECUTED)
        self.assertEqual(
            list(
                ActionAuditLog.objects.filter(datebook_command_id=command.id)
                .order_by("id")
                .values_list("result", flat=True)
            ),
            [ActionAuditOutcome.APPROVED, ActionAuditOutcome.QUEUED, ActionAuditOutcome.EXECUTED],
        )


class EnvelopeAndPushTests(DatebookB2aMixin, TestCase):
    chat_id = 926003

    def test_envelope_is_metadata_only_deterministic_and_hard_bounded(self):
        today = timezone.localdate()
        with suppress_refresh():
            MirrorEvent.objects.create(
                tenant=self.tenant,
                source_key=_source_key("envelope-secret"),
                content_hash="b" * 64,
                active=True,
                first_seen_generation=1,
                last_seen_generation=1,
                time_kind="all_day",
                all_day_start_date=today,
                all_day_end_date_exclusive=today + timedelta(days=1),
                title="SECRET EVENT TITLE",
                notes="SECRET EVENT NOTES",
                location="SECRET EVENT LOCATION",
            )
            MirrorReminder.objects.create(
                tenant=self.tenant,
                source_key=_source_key("envelope-reminder"),
                content_hash="c" * 64,
                active=True,
                first_seen_generation=1,
                last_seen_generation=1,
                due_kind="all_day",
                due_date=today,
                title="SECRET REMINDER TITLE",
            )
        first = render_datebook(self.tenant)
        self.assertEqual(first, render_datebook(self.tenant))
        self.assertLessEqual(len(first), 1200)
        self.assertIn(
            "These blocks are availability metadata only — no titles, not answerable content.",
            first,
        )
        self.assertIn(
            "For ANY question about calendar, schedule, events, availability, or birthdays, "
            "you MUST call `nbhd_datebook_read` this turn and answer only from its result.",
            first,
        )
        self.assertIn("Never answer schedule questions from memory or from these blocks.", first)
        self.assertIn("1 busy", first)
        self.assertIn("1 due today", first)
        self.assertIn(self.gateway.events_last_complete_sync_at.isoformat(), first)
        self.assertNotIn("further days omitted", first)
        self.assertNotIn("SECRET", first)

    def test_envelope_overflow_drops_whole_busy_days_but_keeps_required_lines(self):
        day_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        events = []
        for day_offset in range(7):
            for event_index in range(12):
                start = day_start + timedelta(days=day_offset, hours=event_index * 2)
                events.append(
                    MirrorEvent(
                        tenant=self.tenant,
                        source_key=_source_key(f"overflow-{day_offset}-{event_index}"),
                        content_hash="d" * 64,
                        active=True,
                        first_seen_generation=1,
                        last_seen_generation=1,
                        time_kind="zoned",
                        zoned_start_at=start,
                        zoned_end_at=start + timedelta(hours=1),
                        tz_id="UTC",
                    )
                )
        with suppress_refresh():
            MirrorEvent.objects.bulk_create(events)

        rendered = render_datebook(self.tenant)

        self.assertLessEqual(len(rendered), 1200)
        self.assertIn(
            "These blocks are availability metadata only — no titles, not answerable content.",
            rendered,
        )
        self.assertIn(
            "For ANY question about calendar, schedule, events, availability, or birthdays, "
            "you MUST call `nbhd_datebook_read` this turn and answer only from its result.",
            rendered,
        )
        self.assertIn("Never answer schedule questions from memory or from these blocks.", rendered)
        self.assertIn("- Reminders: 0 overdue; 0 due today", rendered)
        self.assertIn(self.gateway.events_last_complete_sync_at.isoformat(), rendered)
        self.assertIn(self.gateway.reminders_last_complete_sync_at.isoformat(), rendered)
        self.assertIn("- (further days omitted — call nbhd_datebook_read)", rendered)
        self.assertIn("12 busy", rendered)

    @patch("apps.datebook.notify.apns_configured", return_value=True)
    @patch("apps.router.push_views._push_to_user_devices", return_value={"token_count": 1, "used_fallback": False})
    def test_command_push_is_targeted_generic_hybrid_and_idempotent(self, push, _configured):
        command, _ = create_device_command(
            self.tenant,
            request_id="push-command",
            command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
            payload={"items": [{"title": "[PERSON_1] event"}]},
            display_text="PII MUST NOT REACH PUSH",
        )
        notify_device_command(command)
        notify_device_command(command)
        push.assert_called_once()
        kwargs = push.call_args.kwargs
        self.assertEqual(kwargs["installation_id"], "install-a")
        self.assertEqual(kwargs["body"], "Your assistant has a calendar request — open NBHD")
        self.assertTrue(kwargs["content_available"])
        self.assertEqual(kwargs["extra"]["type"], "datebook_command")
        self.assertEqual(kwargs["collapse_id"], f"datebook:{command.id}")
        self.assertNotIn("PII MUST", kwargs["body"])

    @patch("apps.common.apns.send_push", return_value={"unregistered": []})
    def test_push_helper_falls_back_only_when_installation_has_no_active_token(self, send_push):
        from apps.router.push_views import _push_to_user_devices

        DeviceToken.objects.create(
            tenant=self.tenant,
            user=self.tenant.user,
            token="fallback-token",
            installation_id="other-install",
        )
        targeted = DeviceToken.objects.create(
            tenant=self.tenant,
            user=self.tenant.user,
            token="target-token",
            installation_id="install-a",
        )
        targeted_result = _push_to_user_devices(
            self.tenant.user,
            body="generic",
            thread_id=None,
            collapse_id="datebook:test",
            content_available=True,
            extra={"type": "datebook_command"},
            installation_id="install-a",
        )
        self.assertFalse(targeted_result["used_fallback"])
        self.assertEqual(send_push.call_args.args[0], ["target-token"])
        targeted.delete()
        result = _push_to_user_devices(
            self.tenant.user,
            body="generic",
            thread_id=None,
            collapse_id="datebook:test",
            content_available=True,
            extra={"type": "datebook_command"},
            installation_id="install-a",
        )
        self.assertTrue(result["used_fallback"])
        self.assertEqual(send_push.call_args.args[0], ["fallback-token"])


@override_settings(TELEGRAM_WEBHOOK_SECRET="datebook-webhook-secret")
class GateCallbackInvariantTests(DatebookB2aMixin, TestCase):
    chat_id = 926004

    def setUp(self):
        super().setUp()
        self.action = PendingAction.objects.create(
            tenant=self.tenant,
            action_type=ActionType.CALENDAR_CREATE,
            action_payload={},
            display_summary="Create event",
        )

    @patch("apps.actions.messaging.update_gate_message")
    @patch.object(GateRespondView, "resolve_action")
    def test_poller_and_line_use_the_same_resolver(self, resolve, _update):
        resolve.return_value = (self.action, {"status": "approved"}, 200)
        poller = TelegramPoller()
        with patch.object(poller, "_answer_callback_query"):
            poller._handle_gate_callback(
                {},
                self.tenant,
                f"gate_approve:{self.action.id}",
                "callback-1",
                self.chat_id,
            )
        resolve.assert_called_with(
            action_id=self.action.id,
            response_action="approve",
            tenant=self.tenant,
        )
        resolve.reset_mock()
        LineWebhookView._handle_gate_postback(self.tenant, f"gate_deny:{self.action.id}")
        resolve.assert_called_with(
            action_id=self.action.id,
            response_action="deny",
            tenant=self.tenant,
        )

    @patch("apps.actions.messaging.update_gate_message")
    @patch.object(GateRespondView, "resolve_action")
    @patch("apps.router.views.resolve_tenant_by_chat_id")
    @patch("apps.router.views.is_rate_limited", return_value=False)
    @patch("apps.router.inbound_dedup.claim_inbound_event", return_value=True)
    def test_telegram_webhook_uses_the_same_resolver(self, _claim, _limited, resolve_tenant, resolve, _update):
        resolve_tenant.return_value = self.tenant
        resolve.return_value = (self.action, {"status": "approved"}, 200)
        request = RequestFactory().post(
            "/api/v1/telegram/webhook/",
            data=json.dumps(
                {
                    "update_id": 998877,
                    "callback_query": {
                        "id": "callback-webhook",
                        "data": f"gate_approve:{self.action.id}",
                        "message": {"chat": {"id": self.chat_id}},
                    },
                }
            ),
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="datebook-webhook-secret",
        )
        response = telegram_webhook(request)
        self.assertEqual(response.status_code, 200)
        resolve.assert_called_with(
            action_id=self.action.id,
            response_action="approve",
            tenant=self.tenant,
        )
