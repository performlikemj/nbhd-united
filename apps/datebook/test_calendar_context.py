from __future__ import annotations

import hashlib

from django.db import IntegrityError, connection, transaction
from django.test import TestCase

from apps.orchestrator.envelope_registry import suppress_refresh

from .hashing import clean_event_item, clean_reminder_item, content_hash_v1
from .models import CalendarContext, MirrorEvent, MirrorReminder
from .tests import DatebookAPIMixin, _reminder, _zoned_event


def _fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _context(
    seed: str,
    *,
    entity_scope: str = "event",
    included: bool = True,
    container_title: str = "Family",
    source_title: str = "iCloud",
    context_note: str = "Shared family calendar",
) -> dict:
    return {
        "calendar_fingerprint": _fingerprint(seed),
        "entity_scope": entity_scope,
        "included": included,
        "container_title": container_title,
        "source_title": source_title,
        "source_type": "icloud",
        "context_note": context_note,
    }


class CalendarContextConsumerTests(DatebookAPIMixin, TestCase):
    chat_id = 927001
    path = "/api/v1/datebook/calendars/"

    def put_contexts(self, calendars, *, installation_id="install-a", gateway_epoch=None):
        return self.client.put(
            self.path,
            {
                "installation_id": installation_id,
                "gateway_epoch": self.epoch if gateway_epoch is None else gateway_epoch,
                "calendars": calendars,
            },
            format="json",
        )

    def get_contexts(self, *, installation_id="install-a", gateway_epoch=None):
        return self.client.get(
            self.path,
            {
                "installation_id": installation_id,
                "gateway_epoch": self.epoch if gateway_epoch is None else gateway_epoch,
            },
        )

    def test_replace_set_add_update_delete_and_get_restore(self):
        event = _context(
            "family",
            container_title="  Cafe\u0301\r\nFamily  ",
            context_note="  Shared\r\nwith family  ",
        )
        reminder = _context(
            "chores",
            entity_scope="reminder",
            container_title="Chores",
            context_note="Household tasks",
        )
        added = self.put_contexts([event, reminder])
        self.assertEqual(added.status_code, 200, added.data)
        self.assertEqual(added["Cache-Control"], "no-store")
        self.assertEqual(added.data["calendar_count"], 2)
        stored_event = CalendarContext.objects.get(calendar_fingerprint=event["calendar_fingerprint"])
        self.assertEqual(stored_event.container_title, "  Caf\u00e9\nFamily  ")
        self.assertEqual(stored_event.context_note, "Shared\nwith family")

        event["context_note"] = "Updated ownership"
        replaced = self.put_contexts([event])
        self.assertEqual(replaced.status_code, 200, replaced.data)
        self.assertEqual(replaced.data["calendar_count"], 1)
        self.assertEqual(replaced.data["calendars"][0]["context_note"], "Updated ownership")
        self.assertFalse(CalendarContext.objects.filter(calendar_fingerprint=reminder["calendar_fingerprint"]).exists())

        restored = self.get_contexts()
        self.assertEqual(restored.status_code, 200, restored.data)
        self.assertEqual(restored["Cache-Control"], "no-store")
        self.assertEqual(restored.data, replaced.data)

        deleted = self.put_contexts([])
        self.assertEqual(deleted.data, {"calendar_count": 0, "calendars": []})
        self.assertFalse(CalendarContext.objects.filter(tenant=self.tenant).exists())

    def test_unchanged_set_is_placeholder_space_no_op_with_stable_updated_at(self):
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])
        row = _context(
            "placeholder-noop",
            container_title="Alice calendar",
            source_title="Alice iCloud",
            context_note="Shared with Alice",
        )
        first = self.put_contexts([row])
        self.assertEqual(first.status_code, 200, first.data)
        stored = CalendarContext.objects.get(tenant=self.tenant)
        first_updated_at = stored.updated_at
        self.assertEqual(stored.context_note, "Shared with [PERSON_1]")

        row.update(
            {
                "container_title": "[PERSON_1] calendar",
                "source_title": "[PERSON_1] iCloud",
                "context_note": "Shared with [PERSON_1]",
            }
        )
        second = self.put_contexts([row])
        self.assertEqual(second.status_code, 200, second.data)
        stored.refresh_from_db()
        self.assertEqual(stored.updated_at, first_updated_at)
        self.assertEqual(second.data["calendars"][0]["context_note"], "Shared with Alice")

    def test_over_cap_fails_whole_request_without_deleting_prior_rows(self):
        prior = _context("prior")
        self.assertEqual(self.put_contexts([prior]).status_code, 200)
        over_cap = [_context(f"over-{index}") for index in range(65)]
        response = self.put_contexts(over_cap)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": "too_many_calendars", "max_calendars": 64})
        self.assertEqual(
            list(CalendarContext.objects.values_list("calendar_fingerprint", flat=True)),
            [prior["calendar_fingerprint"]],
        )

    def test_scope_consent_rejection_and_revocation_deletion(self):
        consent = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-a",
                "events_consent": True,
                "reminders_consent": False,
            },
            format="json",
        )
        self.assertEqual(consent.status_code, 200, consent.data)
        rejected = self.put_contexts([_context("no-reminders", entity_scope="reminder")])
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(
            rejected.data,
            {"error": "scope_not_consented", "entity_scope": "reminder"},
        )

        event = _context("revoked-event")
        self.assertEqual(self.put_contexts([event]).status_code, 200)
        revoked = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-a",
                "events_consent": False,
                "reminders_consent": False,
            },
            format="json",
        )
        self.assertEqual(revoked.status_code, 200, revoked.data)
        self.assertFalse(CalendarContext.objects.filter(tenant=self.tenant).exists())

    def test_stale_gateway_and_takeover_are_409_on_put_and_get(self):
        denied_takeover = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-b",
                "events_consent": True,
                "reminders_consent": True,
            },
            format="json",
        )
        self.assertEqual(denied_takeover.status_code, 409)
        takeover = self.client.post(
            "/api/v1/datebook/register/",
            {
                "installation_id": "install-b",
                "takeover": True,
                "events_consent": True,
                "reminders_consent": True,
            },
            format="json",
        )
        self.assertEqual(takeover.status_code, 200, takeover.data)
        for response in (self.put_contexts([_context("stale")]), self.get_contexts()):
            with self.subTest(method=response.request["REQUEST_METHOD"]):
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data["error"], "stale_gateway")

    def test_default_delete_excluded_privacy_note_bounds_and_duplicate_key_validation(self):
        excluded = _context(
            "private",
            included=False,
            container_title="",
            source_title="",
            context_note="",
        )
        accepted = self.put_contexts([excluded])
        self.assertEqual(accepted.status_code, 200, accepted.data)
        stored = CalendarContext.objects.get(tenant=self.tenant)
        self.assertFalse(stored.included)
        self.assertEqual(stored.container_title, "")
        self.assertEqual(stored.source_title, "")

        cases = [
            (
                [{**excluded, "container_title": "Do not upload me"}],
                "excluded_calendar_titles_not_empty",
            ),
            ([_context("long", context_note="x" * 241)], "context_note_too_long"),
            ([_context("dupe"), _context("dupe")], "duplicate_calendar"),
        ]
        for calendars, error in cases:
            with self.subTest(error=error):
                response = self.put_contexts(calendars)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["error"], error)
                self.assertEqual(CalendarContext.objects.filter(tenant=self.tenant).count(), 1)

        implicit_default = {**excluded, "included": True, "context_note": " \r\n "}
        implicit_default["container_title"] = "Inventory title"
        deleted = self.put_contexts([implicit_default])
        self.assertEqual(deleted.data["calendar_count"], 0)
        self.assertFalse(CalendarContext.objects.filter(tenant=self.tenant).exists())

    def test_database_rejects_titles_on_excluded_rows(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CalendarContext.objects.create(
                tenant=self.tenant,
                entity_scope="event",
                calendar_fingerprint=_fingerprint("db-constraint"),
                included=False,
                container_title="Leaked title",
            )


class CalendarFingerprintHashTests(DatebookAPIMixin, TestCase):
    chat_id = 927002

    def test_fingerprint_is_stage_only_for_both_hashes_and_old_clients_still_sync(self):
        event = _zoned_event("fingerprint-event")
        event_without_hash = {key: value for key, value in event.items() if key != "content_hash"}
        event_with_fingerprint = {**event_without_hash, "calendar_fingerprint": _fingerprint("event-calendar")}
        self.assertEqual(
            content_hash_v1("event", event_without_hash),
            content_hash_v1("event", event_with_fingerprint),
        )
        event_with_fingerprint["content_hash"] = event["content_hash"]
        self.assertEqual(
            clean_event_item(event_with_fingerprint)["calendar_fingerprint"],
            event_with_fingerprint["calendar_fingerprint"],
        )

        reminder = _reminder("fingerprint-reminder")
        reminder_without_hash = {key: value for key, value in reminder.items() if key != "content_hash"}
        reminder_with_fingerprint = {
            **reminder_without_hash,
            "calendar_fingerprint": _fingerprint("reminder-calendar"),
        }
        self.assertEqual(
            content_hash_v1("reminder", reminder_without_hash),
            content_hash_v1("reminder", reminder_with_fingerprint),
        )
        reminder_with_fingerprint["content_hash"] = reminder["content_hash"]
        self.assertEqual(
            clean_reminder_item(reminder_with_fingerprint)["calendar_fingerprint"],
            reminder_with_fingerprint["calendar_fingerprint"],
        )

        opened = self.open_run(
            "old-client-no-fingerprint",
            reminders_auth="full_access",
            reminders_complete=True,
        )
        page = self.stage_page(opened.data["run_id"], events=[event], reminders=[reminder])
        self.assertEqual(page.status_code, 200, page.data)
        committed = self.commit(
            opened.data["run_id"],
            events=self.manifest([event]),
            reminders=self.manifest([reminder]),
        )
        self.assertEqual(committed.status_code, 200, committed.data)
        self.assertEqual(MirrorEvent.objects.get(source_key=event["source_key"]).calendar_fingerprint, "")
        self.assertEqual(MirrorReminder.objects.get(source_key=reminder["source_key"]).calendar_fingerprint, "")

        stamped = self.open_run(
            "new-client-with-fingerprint",
            reminders_auth="full_access",
            reminders_complete=True,
        )
        stamped_page = self.stage_page(
            stamped.data["run_id"],
            events=[event_with_fingerprint],
            reminders=[reminder_with_fingerprint],
        )
        self.assertEqual(stamped_page.status_code, 200, stamped_page.data)
        stamped_commit = self.commit(
            stamped.data["run_id"],
            events=self.manifest([event_with_fingerprint]),
            reminders=self.manifest([reminder_with_fingerprint]),
        )
        self.assertEqual(stamped_commit.status_code, 200, stamped_commit.data)
        self.assertEqual(
            MirrorEvent.objects.get(source_key=event["source_key"]).calendar_fingerprint,
            event_with_fingerprint["calendar_fingerprint"],
        )
        self.assertEqual(
            MirrorReminder.objects.get(source_key=reminder["source_key"]).calendar_fingerprint,
            reminder_with_fingerprint["calendar_fingerprint"],
        )


class CalendarContextDatabaseTests(DatebookAPIMixin, TestCase):
    chat_id = 927003

    def test_calendar_context_table_is_rls_locked(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relrowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = 'datebook_calendar_contexts'
                """
            )
            self.assertEqual(cursor.fetchone(), (True,))

    def test_tenant_hard_delete_cascades_calendar_context(self):
        with suppress_refresh():
            CalendarContext.objects.create(
                tenant=self.tenant,
                entity_scope="event",
                calendar_fingerprint=_fingerprint("cascade-context"),
                context_note="Owned context",
            )
        tenant_id = self.tenant.id
        self.tenant.delete()
        self.assertFalse(CalendarContext.objects.filter(tenant_id=tenant_id).exists())
