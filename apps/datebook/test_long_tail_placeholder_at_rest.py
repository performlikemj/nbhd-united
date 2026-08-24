"""Real datebook ingress/create/read seams under Layer-1 placeholder writes."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from .hashing import content_hash_v1, manifest_digest_v1
from .models import CalendarContext, DeviceCommand, MirrorEvent, MirrorReminder
from .services import create_device_command
from .tests import _ready_tenant, _reminder, _zoned_event


@contextmanager
def _checked_detection():
    with (
        patch("apps.pii.redactor._detect_pii", return_value=[]),
        patch("apps.pii.authoring._detect_pii", return_value=[]),
    ):
        yield


class DatebookLongTailPlaceholderTests(TestCase):
    def setUp(self):
        self.tenant = _ready_tenant(920006)
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)
        registered = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-a",
                "events_consent": True,
                "reminders_consent": True,
            },
            format="json",
        )
        self.epoch = registered.data["gateway_epoch"]

    def test_sync_and_command_seams_store_placeholders_and_owner_claim_rehydrates(self):
        event = _zoned_event("pii-event", title="Meet Alice")
        event.update(
            {
                "location": "Alice's office",
                "notes": "Bring Alice's document",
                "calendar_title": "Alice calendar",
                "source_title": "Alice iCloud",
            }
        )
        event["content_hash"] = content_hash_v1("event", event)
        reminder = _reminder("pii-reminder", title="Call Alice")
        reminder.update(
            {
                "location": "Alice's house",
                "notes": "Ask Alice",
                "list_title": "Alice errands",
                "source_title": "Alice reminders",
            }
        )
        reminder["content_hash"] = content_hash_v1("reminder", reminder)

        with _checked_detection():
            opened = self.client.post(
                "/api/v1/datebook/sync/open/",
                {
                    "installation_id": "install-a",
                    "gateway_epoch": self.epoch,
                    "client_run_id": "pii-sync",
                    "events": {"authorization": "full_access", "coverage_complete": True},
                    "reminders": {"authorization": "full_access", "coverage_complete": True},
                },
                format="json",
            )
            page = self.client.post(
                "/api/v1/datebook/sync/page/",
                {
                    "run_id": opened.data["run_id"],
                    "page_index": 0,
                    "installation_id": "install-a",
                    "gateway_epoch": self.epoch,
                    "events": [event],
                    "reminders": [reminder],
                },
                format="json",
            )
            committed = self.client.post(
                "/api/v1/datebook/sync/commit/",
                {
                    "run_id": opened.data["run_id"],
                    "installation_id": "install-a",
                    "gateway_epoch": self.epoch,
                    "events": {
                        "item_count": 1,
                        "manifest_digest": manifest_digest_v1([(event["source_key"], event["content_hash"])]),
                        "absent_source_keys": [],
                    },
                    "reminders": {
                        "item_count": 1,
                        "manifest_digest": manifest_digest_v1([(reminder["source_key"], reminder["content_hash"])]),
                        "absent_source_keys": [],
                    },
                },
                format="json",
            )
            command, _created = create_device_command(
                self.tenant,
                request_id="pii-command",
                command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
                payload={
                    "items": [
                        {
                            "title": "Lunch with Alice",
                            "location": "Alice's cafe",
                            "notes": "Ask Alice about plans",
                            "calendar_title": "Alice calendar",
                        }
                    ]
                },
                display_text="Create lunch with Alice",
                destination_name="Alice calendar",
                destination_fingerprint="alice-destination",
            )

        self.assertEqual(page.status_code, 200, page.data)
        self.assertEqual(committed.status_code, 200, committed.data)
        stored_event = MirrorEvent.objects.get(source_key=event["source_key"])
        stored_reminder = MirrorReminder.objects.get(source_key=reminder["source_key"])
        self.assertEqual(stored_event.title, "Meet [PERSON_1]")
        self.assertEqual(stored_event.location, "[PERSON_1]'s office")
        self.assertEqual(stored_event.calendar_title, "[PERSON_1] calendar")
        self.assertEqual(stored_event.source_title, "[PERSON_1] iCloud")
        self.assertEqual(stored_event.pii_receipts["title"]["writer"], "owner")
        self.assertEqual(stored_reminder.title, "Call [PERSON_1]")
        self.assertEqual(stored_reminder.list_title, "[PERSON_1] errands")
        self.assertEqual(stored_reminder.pii_receipts["notes"]["writer"], "owner")

        command.refresh_from_db()
        self.assertEqual(command.display_text, "Create lunch with [PERSON_1]")
        self.assertEqual(command.destination_name, "[PERSON_1] calendar")
        self.assertEqual(command.payload["items"][0]["title"], "Lunch with [PERSON_1]")
        self.assertEqual(command.pii_receipts["payload"]["writer"], "runtime")

        claimed = self.client.post(
            "/api/v1/datebook/commands/claim/",
            {"installation_id": "install-a", "gateway_epoch": self.epoch},
            format="json",
        )
        self.assertEqual(claimed.status_code, 200, claimed.data)
        represented = claimed.data["command"]
        self.assertEqual(represented["display_text"], "Create lunch with Alice")
        self.assertEqual(represented["destination_name"], "Alice calendar")
        self.assertEqual(represented["payload"]["items"][0]["title"], "Lunch with Alice")

    def test_flag_off_preserves_exact_owner_and_runtime_bytes(self):
        self.tenant.layer1_placeholder_writes = False
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        command, _ = create_device_command(
            self.tenant,
            request_id="flag-off-command",
            command_type=DeviceCommand.CommandType.REMINDER_CREATE,
            payload={"items": [{"title": "Exact Alice reminder"}]},
            display_text="Exact Alice display",
        )
        self.assertEqual(command.payload["items"][0]["title"], "Exact Alice reminder")
        self.assertEqual(command.display_text, "Exact Alice display")
        self.assertEqual(command.pii_receipts["payload"], {"state": "bypass", "writer": "runtime"})

    def test_calendar_context_owner_ingress_is_placeholder_at_rest_and_restore_rehydrates(self):
        fingerprint = "d" * 64
        payload = {
            "installation_id": "install-a",
            "gateway_epoch": self.epoch,
            "calendars": [
                {
                    "calendar_fingerprint": fingerprint,
                    "entity_scope": "event",
                    "included": True,
                    "container_title": "Alice family calendar",
                    "source_title": "Alice iCloud",
                    "source_type": "icloud",
                    "context_note": "Shared with Alice — her events, not mine",
                }
            ],
        }
        with _checked_detection():
            written = self.client.put("/api/v1/datebook/calendars/", payload, format="json")
        self.assertEqual(written.status_code, 200, written.data)
        stored = CalendarContext.objects.get(tenant=self.tenant)
        self.assertEqual(stored.container_title, "[PERSON_1] family calendar")
        self.assertEqual(stored.source_title, "[PERSON_1] iCloud")
        self.assertEqual(stored.context_note, "Shared with [PERSON_1] — her events, not mine")
        self.assertEqual(stored.pii_receipts["context_note"]["writer"], "owner")

        restored = self.client.get(
            "/api/v1/datebook/calendars/",
            {"installation_id": "install-a", "gateway_epoch": self.epoch},
        )
        self.assertEqual(restored.status_code, 200, restored.data)
        self.assertEqual(restored.data["calendars"][0]["container_title"], "Alice family calendar")
        self.assertEqual(restored.data["calendars"][0]["context_note"], payload["calendars"][0]["context_note"])
