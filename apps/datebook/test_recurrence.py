from __future__ import annotations

import json
from copy import deepcopy
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from apps.actions.models import PendingAction

from .gate import _logical_request_signature, _rehydrated_command_fields
from .models import DeviceCommand
from .runtime_views import _display_summary
from .services import ProtocolError, _validate_command_payload
from .test_b2a import DatebookB2aMixin


def _event_item(*, title="Team sync", recurrence=None) -> dict:
    item = {
        "title": title,
        "time": {
            "kind": "all_day",
            "start_date": "2099-09-01",
            "end_date_exclusive": "2099-09-02",
        },
    }
    if recurrence is not None:
        item["recurrence"] = recurrence
    return item


def _reminder_item(*, title="Water the plants", recurrence=None) -> dict:
    item = {
        "title": title,
        "due": {"kind": "all_day", "date": "2099-09-01"},
    }
    if recurrence is not None:
        item["recurrence"] = recurrence
    return item


class RecurrenceValidationTests(SimpleTestCase):
    def test_accepts_contract_matrix_without_rewriting_recurrence(self):
        cases = (
            ("daily-default-count", {"freq": "daily", "end": {"type": "count", "count": 10}}),
            (
                "daily-explicit-interval",
                {"freq": "daily", "interval": 99, "end": {"type": "count", "count": 366}},
            ),
            ("weekly-no-weekdays", {"freq": "weekly", "end": {"type": "never"}}),
            (
                "weekly-with-weekdays",
                {
                    "freq": "weekly",
                    "interval": 1,
                    "weekdays": ["mo", "we"],
                    "end": {"type": "until", "date": "2099-10-01"},
                },
            ),
            ("monthly-never", {"freq": "monthly", "end": {"type": "never"}}),
            ("yearly-until", {"freq": "yearly", "end": {"type": "until", "date": "2100-09-01"}}),
        )

        for label, recurrence in cases:
            with self.subTest(label=label):
                payload = {"items": [_event_item(recurrence=deepcopy(recurrence))]}
                original = deepcopy(payload)

                cleaned, item_count = _validate_command_payload(
                    payload,
                    command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
                )

                self.assertEqual(item_count, 1)
                self.assertEqual(payload, original)
                self.assertEqual(cleaned["items"][0]["recurrence"], recurrence)
                if "interval" not in recurrence:
                    self.assertNotIn("interval", cleaned["items"][0]["recurrence"])

    def test_accepts_recurrence_for_dated_and_undated_reminders_when_comparison_is_not_needed(self):
        cases = (
            _reminder_item(recurrence={"freq": "daily", "end": {"type": "count", "count": 2}}),
            {
                "title": "Review inbox",
                "recurrence": {"freq": "weekly", "weekdays": ["fr"], "end": {"type": "never"}},
            },
        )

        for item in cases:
            with self.subTest(title=item["title"]):
                cleaned, item_count = _validate_command_payload(
                    {"items": [deepcopy(item)]},
                    command_type=DeviceCommand.CommandType.REMINDER_CREATE,
                )

                self.assertEqual(item_count, 1)
                self.assertEqual(cleaned["items"][0]["recurrence"], item["recurrence"])

    def test_rejects_contract_matrix(self):
        valid = {"freq": "weekly", "weekdays": ["mo", "we"], "end": {"type": "count", "count": 10}}
        cases = (
            ("unknown-recurrence-key", {**valid, "extra": True}, "invalid_recurrence"),
            (
                "unknown-end-key",
                {**valid, "end": {"type": "count", "count": 10, "extra": True}},
                "invalid_recurrence",
            ),
            (
                "float-interval",
                {"freq": "daily", "interval": 1.5, "end": {"type": "never"}},
                "floats_not_allowed",
            ),
            ("interval-zero", {"freq": "daily", "interval": 0, "end": {"type": "never"}}, "invalid_recurrence"),
            (
                "interval-one-hundred",
                {"freq": "daily", "interval": 100, "end": {"type": "never"}},
                "invalid_recurrence",
            ),
            (
                "weekdays-on-daily",
                {"freq": "daily", "weekdays": ["mo"], "end": {"type": "never"}},
                "invalid_recurrence",
            ),
            (
                "duplicate-weekday",
                {"freq": "weekly", "weekdays": ["mo", "mo"], "end": {"type": "never"}},
                "invalid_recurrence",
            ),
            (
                "bad-weekday",
                {"freq": "weekly", "weekdays": ["monday"], "end": {"type": "never"}},
                "invalid_recurrence",
            ),
            ("bad-frequency", {"freq": "hourly", "end": {"type": "never"}}, "invalid_recurrence"),
            ("non-string-frequency", {"freq": ["daily"], "end": {"type": "never"}}, "invalid_recurrence"),
            ("missing-frequency", {"end": {"type": "never"}}, "invalid_recurrence"),
            ("missing-end", {"freq": "daily"}, "invalid_recurrence"),
            ("count-one", {"freq": "daily", "end": {"type": "count", "count": 1}}, "invalid_recurrence"),
            (
                "count-three-sixty-seven",
                {"freq": "daily", "end": {"type": "count", "count": 367}},
                "invalid_recurrence",
            ),
            (
                "until-before-start",
                {"freq": "daily", "end": {"type": "until", "date": "2099-08-31"}},
                "invalid_recurrence",
            ),
            (
                "until-invalid-calendar-date",
                {"freq": "daily", "end": {"type": "until", "date": "2099-02-30"}},
                "invalid_recurrence",
            ),
        )

        for label, recurrence, expected_code in cases:
            with self.subTest(label=label):
                with self.assertRaises(ProtocolError) as raised:
                    _validate_command_payload(
                        {"items": [_event_item(recurrence=recurrence)]},
                        command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
                    )
                self.assertEqual(raised.exception.code, expected_code)

    def test_rejects_until_for_an_undated_reminder(self):
        with self.assertRaises(ProtocolError) as raised:
            _validate_command_payload(
                {
                    "items": [
                        {
                            "title": "Review inbox",
                            "recurrence": {
                                "freq": "daily",
                                "end": {"type": "until", "date": "2099-10-01"},
                            },
                        }
                    ]
                },
                command_type=DeviceCommand.CommandType.REMINDER_CREATE,
            )
        self.assertEqual(raised.exception.code, "invalid_recurrence")

    def test_until_uses_each_tagged_event_start_and_reminder_due_date(self):
        cases = (
            (
                "event-zoned",
                DeviceCommand.CommandType.CALENDAR_CREATE,
                {
                    "title": "Zoned event",
                    "time": {
                        "kind": "zoned",
                        "start_at": "2099-09-01T09:00:00+09:00",
                        "end_at": "2099-09-01T10:00:00+09:00",
                        "tz_id": "Asia/Tokyo",
                    },
                },
            ),
            (
                "event-floating",
                DeviceCommand.CommandType.CALENDAR_CREATE,
                {
                    "title": "Floating event",
                    "time": {
                        "kind": "floating",
                        "start_local": "2099-09-01T09:00:00",
                        "end_local": "2099-09-01T10:00:00",
                    },
                },
            ),
            (
                "reminder-all-day",
                DeviceCommand.CommandType.REMINDER_CREATE,
                {"title": "All-day reminder", "due": {"kind": "all_day", "date": "2099-09-01"}},
            ),
            (
                "reminder-zoned",
                DeviceCommand.CommandType.REMINDER_CREATE,
                {
                    "title": "Zoned reminder",
                    "due": {
                        "kind": "zoned",
                        "due_at": "2099-09-01T09:00:00+09:00",
                        "tz_id": "Asia/Tokyo",
                    },
                },
            ),
            (
                "reminder-floating",
                DeviceCommand.CommandType.REMINDER_CREATE,
                {
                    "title": "Floating reminder",
                    "due": {"kind": "floating", "due_local": "2099-09-01T09:00:00"},
                },
            ),
        )

        for label, command_type, base_item in cases:
            with self.subTest(label=label, boundary="equal"):
                item = deepcopy(base_item)
                item["recurrence"] = {
                    "freq": "daily",
                    "end": {"type": "until", "date": "2099-09-01"},
                }
                cleaned, _item_count = _validate_command_payload(
                    {"items": [item]},
                    command_type=command_type,
                )
                self.assertEqual(cleaned["items"][0]["recurrence"], item["recurrence"])

            with self.subTest(label=label, boundary="before"):
                item = deepcopy(base_item)
                item["recurrence"] = {
                    "freq": "daily",
                    "end": {"type": "until", "date": "2099-08-31"},
                }
                with self.assertRaises(ProtocolError) as raised:
                    _validate_command_payload({"items": [item]}, command_type=command_type)
                self.assertEqual(raised.exception.code, "invalid_recurrence")

    def test_recurrence_rule_remains_prohibited(self):
        with self.assertRaises(ProtocolError) as raised:
            _validate_command_payload(
                {"items": [{**_event_item(), "recurrence_rule": "FREQ=DAILY"}]},
                command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
            )
        self.assertEqual(raised.exception.code, "unsupported_command_field")

    def test_non_recurring_request_is_byte_identical_and_keeps_existing_summary(self):
        payload = {"items": [_event_item(title="Team sync")]}
        before = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        cleaned, item_count = _validate_command_payload(
            payload,
            command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
        )

        self.assertEqual(item_count, 1)
        self.assertEqual(json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")), before)
        self.assertEqual(
            _display_summary(DeviceCommand.CommandType.CALENDAR_CREATE, cleaned), "Create calendar event: Team sync"
        )

    def test_duplicate_signature_distinguishes_recurrence_without_changing_non_recurring_shape(self):
        target_at = "2099-09-01T00:00:00+00:00"
        base = {
            "command_type": DeviceCommand.CommandType.CALENDAR_CREATE,
            "payload": {"items": [_event_item()]},
            "target_at": target_at,
        }
        self.assertEqual(
            _logical_request_signature(base),
            (DeviceCommand.CommandType.CALENDAR_CREATE, ("team sync",), target_at),
        )

        daily = deepcopy(base)
        daily["payload"]["items"][0]["recurrence"] = {"freq": "daily", "end": {"type": "never"}}
        monthly = deepcopy(base)
        monthly["payload"]["items"][0]["recurrence"] = {"freq": "monthly", "end": {"type": "never"}}
        explicit_default = deepcopy(daily)
        explicit_default["payload"]["items"][0]["recurrence"]["interval"] = 1

        self.assertNotEqual(_logical_request_signature(daily), _logical_request_signature(monthly))
        self.assertEqual(_logical_request_signature(daily), _logical_request_signature(explicit_default))
        self.assertNotIn("interval", daily["payload"]["items"][0]["recurrence"])


class RecurrenceGateTextTests(SimpleTestCase):
    def test_weekly_weekdays_until_text(self):
        item = _event_item(
            title="Planning",
            recurrence={
                "freq": "weekly",
                "weekdays": ["mo", "we"],
                "end": {"type": "until", "date": "2026-10-01"},
            },
        )
        item["time"] = {
            "kind": "all_day",
            "start_date": "2026-09-01",
            "end_date_exclusive": "2026-09-02",
        }
        payload = {"items": [item]}

        self.assertEqual(
            _display_summary(DeviceCommand.CommandType.CALENDAR_CREATE, payload),
            "Create calendar event: Planning\nRepeats weekly on Mon, Wed until 2026-10-01",
        )

    def test_daily_count_text(self):
        payload = {
            "items": [
                _reminder_item(
                    title="Take vitamins",
                    recurrence={"freq": "daily", "end": {"type": "count", "count": 10}},
                )
            ]
        }

        self.assertEqual(
            _display_summary(DeviceCommand.CommandType.REMINDER_CREATE, payload),
            "Create Apple Reminder: Take vitamins\nRepeats daily ×10",
        )

    def test_monthly_never_text(self):
        payload = {
            "items": [
                _event_item(
                    title="Finance review",
                    recurrence={"freq": "monthly", "end": {"type": "never"}},
                )
            ]
        }

        self.assertEqual(
            _display_summary(DeviceCommand.CommandType.CALENDAR_CREATE, payload),
            "Create calendar event: Finance review\nRepeats monthly (no end)",
        )

    def test_multi_item_text_keeps_every_rule_inside_gate_limit(self):
        recurrence = {
            "freq": "weekly",
            "interval": 99,
            "weekdays": ["mo", "tu", "we", "th", "fr", "sa", "su"],
            "end": {"type": "until", "date": "2099-10-01"},
        }
        payload = {
            "items": [_event_item(title=f"{'A' * 250}{index}", recurrence=deepcopy(recurrence)) for index in range(5)]
        }

        summary = _display_summary(DeviceCommand.CommandType.CALENDAR_CREATE, payload)

        self.assertLessEqual(len(summary), 500)
        self.assertEqual(summary.count("Repeats every 99 weeks"), 5)
        self.assertEqual(summary.count("until 2099-10-01"), 5)
        for index, line in enumerate(summary.splitlines()[1:]):
            self.assertTrue(line.endswith(str(index)), line)


class RecurrencePassThroughTests(DatebookB2aMixin, TestCase):
    chat_id = 929201

    def setUp(self):
        super().setUp()
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Daily"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        self.consumer = APIClient()
        self.consumer.force_authenticate(user=self.tenant.user)
        self.authoring_detector = patch("apps.pii.authoring._detect_pii", return_value=[])
        self.redactor_detector = patch("apps.pii.redactor._detect_pii", return_value=[])
        self.authoring_detector.start()
        self.redactor_detector.start()
        self.addCleanup(self.authoring_detector.stop)
        self.addCleanup(self.redactor_detector.stop)

    @patch("apps.datebook.notify.notify_device_command")
    def test_recurrence_is_identical_in_pending_action_and_approved_device_command(self, _notify):
        cases = (
            (
                "calendar_create",
                _event_item(
                    recurrence={
                        "freq": "daily",
                        "interval": 2,
                        "end": {"type": "until", "date": "2099-12-31"},
                    }
                ),
            ),
            (
                "reminder_create",
                _reminder_item(
                    recurrence={
                        "freq": "monthly",
                        "end": {"type": "count", "count": 12},
                    }
                ),
            ),
        )

        for index, (command_type, item) in enumerate(cases):
            with self.subTest(command_type=command_type):
                recurrence = deepcopy(item["recurrence"])
                created = self.client.post(
                    f"/api/v1/datebook/runtime/{self.tenant.id}/datebook/request-create",
                    {
                        "request_id": f"recurrence-pass-through-{index}",
                        "command_type": command_type,
                        "payload": {"items": [item]},
                        "direct_user_originated": True,
                        "originating_channel": "ios",
                    },
                    format="json",
                    **self.headers,
                )
                self.assertEqual(created.status_code, 202, created.data)

                action = PendingAction.objects.get(pk=created.data["action_id"])
                self.assertEqual(action.action_payload["payload"]["items"][0]["recurrence"], recurrence)
                pending_fields = _rehydrated_command_fields(action)
                self.assertEqual(pending_fields["payload"]["items"][0]["recurrence"], recurrence)

                approved = self.consumer.post(
                    f"/api/v1/datebook/gate/{action.id}/respond/",
                    {"response": "approve"},
                    format="json",
                )
                self.assertEqual(approved.status_code, 200, approved.data)
                self.assertEqual(approved.data["state"], "approved_queued")

                action.refresh_from_db()
                self.assertEqual(action.action_payload["payload"]["items"][0]["recurrence"], recurrence)
                command = DeviceCommand.objects.get(pk=action.datebook_command_id)
                self.assertEqual(command.payload["items"][0]["recurrence"], recurrence)
                claimed = self.consumer.post(
                    "/api/v1/datebook/commands/claim/",
                    {
                        "installation_id": self.gateway.installation_id,
                        "gateway_epoch": self.gateway.gateway_epoch,
                    },
                    format="json",
                )
                self.assertEqual(claimed.status_code, 200, claimed.data)
                self.assertEqual(claimed.data["command"]["id"], str(command.id))
                self.assertEqual(claimed.data["command"]["payload"]["items"][0]["recurrence"], recurrence)
