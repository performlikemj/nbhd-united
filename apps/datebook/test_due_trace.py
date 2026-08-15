from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from datetime import timezone as datetime_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from apps.actions.models import PendingAction

from .gate import _rehydrated_command_fields
from .models import DeviceCommand
from .services import _validate_command_payload
from .test_b2a import DatebookB2aMixin


class ReminderCommandPayloadNormalizationTests(SimpleTestCase):
    def test_absolute_alarm_without_due_derives_zoned_due_from_trigger_offset(self):
        trigger_at = "2099-08-16T08:00:00+09:00"
        payload = {
            "items": [
                {
                    "title": "Pack my bags",
                    "alarm": {"kind": "absolute", "trigger_at": trigger_at},
                }
            ]
        }
        original = deepcopy(payload)

        cleaned, item_count = _validate_command_payload(
            payload,
            command_type=DeviceCommand.CommandType.REMINDER_CREATE,
        )

        self.assertEqual(item_count, 1)
        self.assertEqual(payload, original)
        self.assertEqual(
            cleaned,
            {
                "items": [
                    {
                        "title": "Pack my bags",
                        "alarm": {"kind": "absolute", "trigger_at": trigger_at},
                        "due": {
                            "kind": "zoned",
                            "due_at": trigger_at,
                            "tz_id": "UTC+09:00",
                        },
                    }
                ]
            },
        )

    def test_due_only_and_both_present_are_unchanged(self):
        due_only = {
            "title": "Submit expenses",
            "due": {
                "kind": "zoned",
                "due_at": "2099-08-16T08:00:00+09:00",
                "tz_id": "Asia/Tokyo",
            },
        }
        conflicting_both = {
            "title": "Call the airline",
            "due": {
                "kind": "zoned",
                "due_at": "2099-08-16T08:00:00+09:00",
                "tz_id": "Asia/Tokyo",
            },
            "alarm": {
                "kind": "absolute",
                "trigger_at": "2099-08-16T09:00:00+09:00",
            },
        }
        explicit_none_with_alarm = {
            "title": "Keep this dateless",
            "due": {"kind": "none"},
            "alarm": {
                "kind": "absolute",
                "trigger_at": "2099-08-16T10:00:00+09:00",
            },
        }

        for label, item in (
            ("due-only", due_only),
            ("both-conflicting", conflicting_both),
            ("explicit-none-and-alarm", explicit_none_with_alarm),
        ):
            with self.subTest(label=label):
                payload = {"items": [deepcopy(item)]}
                original = deepcopy(payload)

                cleaned, item_count = _validate_command_payload(
                    payload,
                    command_type=DeviceCommand.CommandType.REMINDER_CREATE,
                )

                self.assertEqual(item_count, 1)
                self.assertEqual(payload, original)
                self.assertEqual(cleaned, original)


class ReminderDueEndToEndTraceTests(DatebookB2aMixin, TestCase):
    chat_id = 929101

    def setUp(self):
        super().setUp()
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        self.consumer = APIClient()
        self.consumer.force_authenticate(user=self.tenant.user)
        self.authoring_detector = patch("apps.pii.authoring._detect_pii", return_value=[])
        self.redactor_detector = patch("apps.pii.redactor._detect_pii", return_value=[])
        self.authoring_detector.start()
        self.redactor_detector.start()
        self.addCleanup(self.authoring_detector.stop)
        self.addCleanup(self.redactor_detector.stop)

    def _request_reminder(self, request_id: str, item: dict):
        return self.client.post(
            f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
            {
                "request_id": request_id,
                "command_type": DeviceCommand.CommandType.REMINDER_CREATE,
                "payload": {"items": [item]},
                "direct_user_originated": True,
                "originating_channel": "app",
            },
            format="json",
            **self.headers,
        )

    def _card_item(self, action: PendingAction) -> dict:
        response = self.consumer.get("/api/v1/datebook/gate/pending/")
        self.assertEqual(response.status_code, 200, response.data)
        card = next(item for item in response.data["actions"] if item["action_id"] == action.id)
        return card["payload"]["payload"]["items"][0]

    def _approve_and_claim(self, action: PendingAction) -> tuple[DeviceCommand, dict]:
        approved = self.consumer.post(
            f"/api/v1/datebook/gate/{action.id}/respond/",
            {"response": "approve"},
            format="json",
        )
        self.assertEqual(approved.status_code, 200, approved.data)
        self.assertEqual(approved.data["state"], "approved_queued")
        command = DeviceCommand.objects.get(pk=approved.data["command_id"])
        claimed = self.consumer.post(
            "/api/v1/datebook/commands/claim/",
            {
                "installation_id": self.gateway.installation_id,
                "gateway_epoch": self.gateway.gateway_epoch,
            },
            format="json",
        )
        self.assertEqual(claimed.status_code, 200, claimed.data)
        self.assertIsNotNone(claimed.data["command"])
        return command, claimed.data["command"]

    def test_explicit_due_and_alarm_survive_every_backend_hop_verbatim(self):
        due = {
            "kind": "zoned",
            "due_at": "2099-08-16T08:00:00+09:00",
            "tz_id": "Asia/Tokyo",
        }
        alarm = {
            "kind": "absolute",
            "trigger_at": "2099-08-16T09:00:00+09:00",
        }
        requested_item = {
            "title": "Call Alice",
            "due": due,
            "alarm": alarm,
        }

        created = self._request_reminder("explicit-due-alarm-trace", requested_item)

        self.assertEqual(created.status_code, 202, created.data)
        action = PendingAction.objects.get(datebook_request_id="explicit-due-alarm-trace")
        stored_item = action.action_payload["payload"]["items"][0]
        self.assertEqual(stored_item["title"], "Call [PERSON_1]")
        self.assertEqual(stored_item["due"], due)
        self.assertEqual(stored_item["alarm"], alarm)

        rehydrated_item = _rehydrated_command_fields(action)["payload"]["items"][0]
        self.assertEqual(rehydrated_item, requested_item)
        self.assertEqual(self._card_item(action), requested_item)

        stored_command, claimed_command = self._approve_and_claim(action)
        stored_command_item = stored_command.payload["items"][0]
        self.assertEqual(stored_command_item["title"], "Call [PERSON_1]")
        self.assertEqual(stored_command_item["due"], due)
        self.assertEqual(stored_command_item["alarm"], alarm)
        self.assertEqual(claimed_command["payload"], {"items": [requested_item]})

    def test_alarm_only_derives_due_for_storage_card_command_and_claim(self):
        trigger_at = "2099-08-16T08:00:00+09:00"
        alarm = {"kind": "absolute", "trigger_at": trigger_at}
        derived_due = {
            "kind": "zoned",
            "due_at": trigger_at,
            "tz_id": "UTC+09:00",
        }
        expected_item = {
            "title": "Pack my bags",
            "alarm": alarm,
            "due": derived_due,
        }

        created = self._request_reminder(
            "alarm-only-due-trace",
            {"title": "Pack my bags", "alarm": alarm},
        )

        self.assertEqual(created.status_code, 202, created.data)
        action = PendingAction.objects.get(datebook_request_id="alarm-only-due-trace")
        self.assertEqual(action.action_payload["payload"]["items"][0], expected_item)
        self.assertEqual(action.action_payload["target_at"], trigger_at)
        self.assertEqual(_rehydrated_command_fields(action)["payload"]["items"][0], expected_item)
        self.assertEqual(self._card_item(action), expected_item)

        stored_command, claimed_command = self._approve_and_claim(action)
        self.assertEqual(stored_command.payload, {"items": [expected_item]})
        self.assertEqual(claimed_command["payload"], {"items": [expected_item]})
        self.assertEqual(parse_datetime(claimed_command["target_at"]), parse_datetime(trigger_at))

    def test_alarm_only_due_in_two_hours_clamps_gate_expiry(self):
        trigger = (
            (timezone.now() + timedelta(hours=2))
            .astimezone(datetime_timezone(timedelta(hours=9)))
            .replace(microsecond=0)
        )
        trigger_at = trigger.isoformat()
        expected_due = {
            "kind": "zoned",
            "due_at": trigger_at,
            "tz_id": "UTC+09:00",
        }

        created = self._request_reminder(
            "alarm-only-expiry-clamp",
            {
                "title": "Leave for the airport",
                "alarm": {"kind": "absolute", "trigger_at": trigger_at},
            },
        )

        self.assertEqual(created.status_code, 202, created.data)
        action = PendingAction.objects.get(datebook_request_id="alarm-only-expiry-clamp")
        self.assertEqual(action.action_payload["payload"]["items"][0]["due"], expected_due)
        self.assertEqual(action.expires_at, trigger)
