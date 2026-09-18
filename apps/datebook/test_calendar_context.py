from __future__ import annotations

import hashlib
import re
import threading
from unittest.mock import patch

from django.db import IntegrityError, OperationalError, close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase

from apps.orchestrator.envelope_registry import suppress_refresh
from apps.pii.entity_registry import get_name
from apps.pii.redactor import DetectedEntity
from apps.pii.store_authoring import author_store_fields
from apps.tenants.models import Tenant

from . import services
from .hashing import clean_event_item, clean_reminder_item, content_hash_v1
from .models import CalendarContext, DatebookGateway, MirrorEvent, MirrorReminder
from .tests import DatebookAPIMixin, _ready_tenant, _reminder, _zoned_event


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


_CALENDAR_CONTEXT_AUTHORING = {
    "model_label": "datebook.CalendarContext",
    "seam": "datebook.owner.calendar_context.ingress",
    "writer": "owner",
}


def _bob_detector(events: list | None = None, *, before_call=None):
    """Stand-in for the neural detector: every ``Bob`` is a PERSON span.

    Marks the neural outcome available so authoring lands on the confirmed
    ``placeholder`` receipt path (the one production takes), records each
    call into ``events`` and runs ``before_call`` first when supplied.
    """

    def detect(text, entities, score_threshold):
        from apps.pii import redactor

        if before_call is not None:
            before_call()
        redactor._neural_detector_outcome.available = True
        if events is not None:
            events.append(("detect", text))
        return [DetectedEntity("PERSON", match.start(), match.end(), 0.99) for match in re.finditer(r"\bBob\b", text)]

    return detect


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


class CalendarContextLockScopeTests(DatebookAPIMixin, TestCase):
    """PUT calendars authors PII BEFORE the tenant row lock, with unchanged coverage."""

    chat_id = 927004
    path = "/api/v1/datebook/calendars/"

    def setUp(self):
        super().setUp()
        self.tenant.layer1_placeholder_writes = True
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Alice"}}
        self.tenant.save(update_fields=["layer1_placeholder_writes", "pii_entity_map"])

    def put_contexts(self, calendars, *, gateway_epoch=None):
        return self.client.put(
            self.path,
            {
                "installation_id": "install-a",
                "gateway_epoch": self.epoch if gateway_epoch is None else gateway_epoch,
                "calendars": calendars,
            },
            format="json",
        )

    def _bob_placeholder(self) -> str:
        self.tenant.refresh_from_db(fields=["pii_entity_map"])
        minted = [placeholder for placeholder, entry in self.tenant.pii_entity_map.items() if get_name(entry) == "Bob"]
        self.assertEqual(len(minted), 1, self.tenant.pii_entity_map)
        return minted[0]

    def test_every_detector_call_precedes_the_tenant_lock(self):
        events: list[tuple[str, str | None]] = []
        original_lock = services._locked_tenant

        def recording_lock(tenant):
            events.append(("lock", None))
            return original_lock(tenant)

        rows = [
            _context(
                "lock-scope-event",
                container_title="Alice calendar",
                source_title="Alice iCloud",
                context_note="Shared with Bob",
            ),
            _context(
                "lock-scope-reminder",
                entity_scope="reminder",
                container_title="Chores",
                source_title="Bob list",
                context_note="Bob and Alice",
            ),
        ]
        with (
            patch("apps.pii.redactor._detect_pii", side_effect=_bob_detector(events)),
            patch.object(services, "_locked_tenant", recording_lock),
        ):
            response = self.put_contexts(rows)
        self.assertEqual(response.status_code, 200, response.data)

        kinds = [kind for kind, _ in events]
        # One detector round trip per non-empty registered text field: three
        # per calendar, and the lock is taken exactly once, AFTER all of them.
        self.assertEqual(kinds.count("detect"), 6, events)
        self.assertEqual(kinds.count("lock"), 1, events)
        self.assertEqual(kinds[-1], "lock", events)

        bob = self._bob_placeholder()
        stored = {row.calendar_fingerprint: row for row in CalendarContext.objects.filter(tenant=self.tenant)}
        event = stored[rows[0]["calendar_fingerprint"]]
        reminder = stored[rows[1]["calendar_fingerprint"]]
        self.assertEqual(event.container_title, "[PERSON_1] calendar")
        self.assertEqual(event.source_title, "[PERSON_1] iCloud")
        self.assertEqual(event.context_note, f"Shared with {bob}")
        self.assertEqual(reminder.container_title, "Chores")
        self.assertEqual(reminder.source_title, f"{bob} list")
        self.assertEqual(reminder.context_note, f"{bob} and [PERSON_1]")

    def test_stored_values_and_receipts_match_direct_store_authoring(self):
        row = _context(
            "lock-scope-coverage",
            container_title="Alice and Bob",
            source_title="Bob iCloud",
            context_note="Shared with Alice, Bob",
        )
        calls: list[dict] = []
        real_author = services.author_store_fields

        def spying_author(tenant, data, **kwargs):
            calls.append(kwargs)
            return real_author(tenant, data, **kwargs)

        with (
            patch("apps.pii.redactor._detect_pii", side_effect=_bob_detector()),
            patch.object(services, "author_store_fields", spying_author),
        ):
            response = self.put_contexts([row])
        self.assertEqual(response.status_code, 200, response.data)
        # Same store, same seam, same writer class as before the lock-scope change.
        self.assertEqual(calls, [_CALENDAR_CONTEXT_AUTHORING])

        bob = self._bob_placeholder()
        stored = CalendarContext.objects.get(tenant=self.tenant)
        self.assertEqual(stored.container_title, f"[PERSON_1] and {bob}")
        self.assertEqual(stored.source_title, f"{bob} iCloud")
        self.assertEqual(stored.context_note, f"Shared with [PERSON_1], {bob}")
        for field in ("container_title", "source_title", "context_note"):
            self.assertEqual(stored.pii_receipts[field]["state"], "placeholder", stored.pii_receipts)

        # Authoring the same row straight through the registry seam yields
        # byte-identical stored text and receipts: nothing was bypassed.
        with patch("apps.pii.redactor._detect_pii", side_effect=_bob_detector()):
            expected, receipts = author_store_fields(
                Tenant.objects.get(pk=self.tenant.pk),
                row,
                **_CALENDAR_CONTEXT_AUTHORING,
            )
        for field in ("container_title", "source_title", "context_note"):
            self.assertEqual(getattr(stored, field), expected[field], field)
        self.assertEqual(stored.pii_receipts, receipts)
        # Owner-read rehydration of the known binding is intact. (A placeholder
        # minted by this very request rehydrates from the request's tenant
        # instance, which predates the mint — unchanged from before.)
        self.assertTrue(response.data["calendars"][0]["container_title"].startswith("Alice and "), response.data)

    def test_rejected_put_never_reaches_authoring(self):
        with (
            patch("apps.pii.redactor._detect_pii", side_effect=_bob_detector()) as detect,
            patch.object(services, "author_store_fields", wraps=services.author_store_fields) as author,
        ):
            stale = self.put_contexts([_context("lock-scope-stale", context_note="Bob")], gateway_epoch=self.epoch + 1)
            self.assertEqual(stale.status_code, 409, stale.data)
            self.assertEqual(stale.data["error"], "stale_gateway")

            self.tenant.datebook_reminders_consent_at = None
            self.tenant.save(update_fields=["datebook_reminders_consent_at"])
            unconsented = self.put_contexts(
                [_context("lock-scope-unconsented", entity_scope="reminder", context_note="Bob")]
            )
            self.assertEqual(unconsented.status_code, 400, unconsented.data)
            self.assertEqual(unconsented.data["error"], "scope_not_consented")

        # Pre-lock authoring must not mint for a request the locked checks reject.
        author.assert_not_called()
        detect.assert_not_called()
        self.tenant.refresh_from_db(fields=["pii_entity_map"])
        self.assertEqual(self.tenant.pii_entity_map, {"[PERSON_1]": {"name": "Alice"}})
        self.assertFalse(CalendarContext.objects.filter(tenant=self.tenant).exists())


class CalendarContextLockContentionTests(TransactionTestCase):
    """While PUT calendars is inside the detector, chat intake can lock the tenant row."""

    reset_sequences = True

    def setUp(self):
        self.tenant = _ready_tenant(927005)
        self.tenant.layer1_placeholder_writes = True
        self.tenant.save(update_fields=["layer1_placeholder_writes"])
        DatebookGateway.objects.create(tenant=self.tenant, installation_id="install-a")

    def test_detector_calls_run_with_the_tenant_row_unlocked(self):
        outcomes: list[str] = []

        def contend_for_tenant_row():
            # ``record_provisional_sightings`` (chat intake) takes exactly this
            # lock. Before the lock-scope change it waited out every detector
            # call of the PUT; now it must get the row immediately.
            close_old_connections()
            try:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '2000ms'")
                    Tenant.objects.select_for_update().get(pk=self.tenant.pk)
                outcomes.append("locked")
            except OperationalError:
                outcomes.append("blocked")
            finally:
                close_old_connections()

        def during_detection():
            worker = threading.Thread(target=contend_for_tenant_row, name="chat-intake")
            worker.start()
            worker.join(timeout=10)
            if worker.is_alive():
                outcomes.append("hung")

        with patch("apps.pii.redactor._detect_pii", side_effect=_bob_detector(before_call=during_detection)):
            rows = services.replace_calendar_contexts(
                Tenant.objects.get(pk=self.tenant.pk),
                installation_id="install-a",
                gateway_epoch=1,
                calendars=[
                    _context(
                        "lock-contention",
                        container_title="Family",
                        source_title="iCloud",
                        context_note="Shared with Bob",
                    )
                ],
            )

        # Three text fields → three detector calls, each with the row free.
        self.assertEqual(outcomes, ["locked", "locked", "locked"])
        self.assertEqual(len(rows), 1)
        self.tenant.refresh_from_db(fields=["pii_entity_map"])
        minted = [placeholder for placeholder, entry in self.tenant.pii_entity_map.items() if get_name(entry) == "Bob"]
        self.assertEqual(len(minted), 1, self.tenant.pii_entity_map)
        self.assertEqual(rows[0].context_note, f"Shared with {minted[0]}")
